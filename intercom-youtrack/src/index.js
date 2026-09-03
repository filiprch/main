/**
 * Intercom → YouTrack connector (Cloudflare Worker)
 *
 * Flow:
 *   1. Receive an Intercom webhook POST.
 *   2. Verify the X-Hub-Signature (HMAC-SHA1 of the raw body with the app's
 *      client secret) — rejects forged requests.
 *   3. Trigger = HANDOFF TO HUMANS, which happens in two different ways:
 *        a) Fin escalates. Intercom fires NO assignment event for this — it
 *           only sets "Fin AI Agent resolution state" to Escalated and leaves
 *           the conversation unassigned. So we listen to topics that do fire
 *           during a conversation and read that state.
 *        b) Someone is assigned — a named teammate, or a team inbox.
 *      Either counts. Assignment to Fin or another operator bot does not.
 *   4. Create a YouTrack CS ticket from the CUSTOMER's first message. The
 *      conversation `source` is Fin's greeting on website chat, so it is not
 *      the customer and must not be used for the summary or the sender.
 *   5. Add an INTERNAL NOTE to the conversation naming the ticket. Never a
 *      reply — the customer is never messaged by this worker.
 *
 * Secrets / vars:
 *   INTERCOM_CLIENT_SECRET    (secret)  used to verify X-Hub-Signature
 *   INTERCOM_TOKEN            (secret)  Intercom access token (for contact lookup)
 *   YOUTRACK_TOKEN           (secret)  YouTrack permanent token
 *   YOUTRACK_BASE_URL        (var)     https://myrealprofit.youtrack.cloud
 *   YOUTRACK_PROJECT_ID      (var)     CS
 *   INTERCOM_APP_ID          (var)     used to build the conversation deep-link
 *   INTERCOM_EXCLUDE_ADMIN_IDS (var)   comma-separated admin IDs to treat as
 *                                      bots (e.g. Fin) — assignments to these
 *                                      do NOT create a ticket
 *   INTERCOM_TEST_CONTACT_EMAILS (var) test mode: only these contacts create
 *                                      tickets. Empty = every escalation.
 *   INTERCOM_NOTE_ADMIN_ID     (var)   admin the internal note is posted as.
 *                                      Empty = the INTERCOM_TOKEN owner.
 *   INTERCOM_NOTE_PREFIX       (var)   bold first line of the note, e.g.
 *                                      "TESTING HELPDESK". Empty = no prefix.
 *   DEDUPE                   (KV, opt) dedupe by conversation id
 */

import { createYouTrackIssue, updateYouTrackIssue } from './youtrack.js';

export default {
  async fetch(request, env, ctx) {
    if (request.method !== 'POST') {
      return new Response('Method Not Allowed', { status: 405 });
    }

    const rawBody = await request.text();

    const verified = await verifyIntercomSignature(
      request,
      rawBody,
      env.INTERCOM_CLIENT_SECRET
    );
    if (!verified) {
      return new Response('Invalid signature', { status: 401 });
    }

    let payload;
    try {
      payload = JSON.parse(rawBody);
    } catch {
      return new Response('Bad JSON', { status: 400 });
    }

    // Intercom sends a `ping` topic when you test the webhook in the dashboard.
    if (payload.topic === 'ping') {
      return new Response('pong', { status: 200 });
    }

    ctx.waitUntil(
      handleEvent(payload, env).catch((e) => console.error('handleEvent error:', e))
    );
    return new Response('', { status: 200 });
  },
};

/** How long to wait before re-reading a conversation that is not yet flagged. */
const ESCALATION_RECHECK_MS = 8000;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Should this conversation produce a ticket, and why?
 *
 * Two routes to the same answer: Fin flagged it Escalated, or a human owns it
 * — a named teammate, or a team inbox. Assignment to Fin or another operator
 * bot does not count.
 */
function handoffState(conversation, env) {
  const assignee = conversation.admin_assignee_id;
  const teamAssignee = conversation.team_assignee_id;
  const excluded = (env.INTERCOM_EXCLUDE_ADMIN_IDS || '')
    .split(',')
    .map((x) => x.trim())
    .filter(Boolean);

  const toHuman = Boolean(assignee) && !excluded.includes(String(assignee));
  const toTeam = Boolean(teamAssignee);
  const escalated = isEscalated(conversation, env);

  return { create: toHuman || toTeam || escalated, assignee, teamAssignee, escalated };
}

/**
 * Topics that fire while a conversation is in progress.
 *
 * `conversation.operator.replied` is the important one. Fin is the Operator,
 * not a teammate, so its messages do NOT fire conversation.admin.replied
 * ("Reply from your teammates") — that omission is why escalations were
 * invisible. Fin sets the escalation flag and then speaks, so this event
 * arrives with the state already in place.
 */
const HANDLED_TOPICS = [
  'conversation.operator.replied',
  'conversation.admin.assigned',
  'conversation.admin.replied',
  'conversation.user.replied',
];

/**
 * Topics that can land BEFORE Fin has finished deciding. A customer message
 * arrives, then Fin flags the conversation a beat later, so on these it is
 * worth looking again. On operator.replied the state is already settled and
 * re-reading would only burn an API call and eight seconds.
 */
const TOPICS_WORTH_RECHECKING = [
  'conversation.user.replied',
  'conversation.admin.replied',
];

async function handleEvent(payload, env) {
  if (!HANDLED_TOPICS.includes(payload.topic)) return;

  const item = payload.data?.item;
  if (!item?.id) return;

  // A ticket already exists for this conversation. The customer is still
  // talking, though, and the first thing they say is often just "hello" — so
  // rather than ignore the rest, keep the ticket's body in step with the
  // conversation. Anything other than a customer message is ignored outright.
  const existingTicket = await processedTicket(env, item.id);
  if (existingTicket) {
    if (payload.topic === 'conversation.user.replied') {
      try {
        await refreshTicket(env, item.id, existingTicket);
      } catch (e) {
        console.error(`refresh of ${existingTicket} failed:`, e);
      }
    }
    return;
  }

  // Webhook payloads are trimmed and often omit the parts we need — the
  // customer's own first message above all. Fetch the full conversation.
  let conversation =
    (env.INTERCOM_TOKEN && (await fetchConversation(env.INTERCOM_TOKEN, item.id))) ||
    item;

  let handoff = handoffState(conversation, env);
  let rechecked = false;

  // Fin sets the escalation flag a beat AFTER the message that causes it, and
  // its own replies are bot messages that fire no webhook of their own. So the
  // event we are handling routinely arrives a few seconds before the state we
  // are looking for, with nothing further coming to tell us. Look once more
  // before giving up.
  if (!handoff.create && TOPICS_WORTH_RECHECKING.includes(payload.topic)) {
    console.log(`${item.id}: not flagged on first read, waiting to re-check`);
    await sleep(ESCALATION_RECHECK_MS);
    const fresh =
      env.INTERCOM_TOKEN && (await fetchConversation(env.INTERCOM_TOKEN, item.id));
    if (fresh) {
      conversation = fresh;
      handoff = handoffState(conversation, env);
      rechecked = true;
    }
  }

  if (!handoff.create) {
    console.log(
      `skip ${conversation.id}${rechecked ? ' (after re-check)' : ' (no re-check)'}: ` +
        `admin=${handoff.assignee ?? 'none'} team=${handoff.teamAssignee ?? 'none'} ` +
        `state=${conversation.ai_agent?.resolution_state ?? 'unknown'}`
    );
    return;
  }

  console.log(
    `create for ${conversation.id}: escalated=${handoff.escalated} ` +
      `rechecked=${rechecked}`
  );

  // Another delivery for this conversation may have won the race while we
  // slept. Check again now that we know we want to create.
  if (await processedTicket(env, conversation.id)) return;

  const contact = extractContact(conversation);
  // Email may be absent in the webhook payload — look it up if we have a token.
  let email = contact.email;
  if (!email && contact.id && env.INTERCOM_TOKEN) {
    email = await fetchContactEmail(env.INTERCOM_TOKEN, contact.id);
  }

  // Test mode. While the integration is being trialled, only escalations from
  // these contacts create tickets. Empty = every escalation counts, which is
  // the production behaviour.
  const testContacts = (env.INTERCOM_TEST_CONTACT_EMAILS || '')
    .split(',')
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
  if (testContacts.length && !testContacts.includes((email || '').toLowerCase())) {
    console.log(
      `skip ${conversation.id}: ${email || 'unknown contact'} is not on the test list`
    );
    return;
  }


  const subject = (conversation.source?.subject || '').trim();
  const summary = summarize(
    subject || pickSummary(conversation) || `Conversation ${conversation.id}`
  );

  const description = buildDescription({
    sender: contact.name || email || 'Intercom contact',
    email,
    createdAt: conversation.created_at,
    link: conversationLink(env.INTERCOM_APP_ID, conversation.id),
    subject,
    transcript: buildTranscript(conversation),
    attachments: allAttachments(conversation),
    escalatedReason:
      conversation.custom_attributes?.['Fin AI Agent escalated reason'] || '',
  });

  const issue = await createYouTrackIssue({
    baseUrl: env.YOUTRACK_BASE_URL,
    token: env.YOUTRACK_TOKEN,
    projectId: env.YOUTRACK_PROJECT_ID || 'CS',
    summary,
    description,
    channel: 'Intercom',
    type: 'Task',
    replied: 'Not Replied',
    customerEmail: email || undefined,
  });

  await markProcessed(env, conversation.id, issue.idReadable);

  // Best effort: a failed note must never lose the ticket we just created.
  // handoff.assignee, not a bare `assignee` — the local went away when the
  // guard moved into handoffState(). Wrapped in try/catch rather than .catch()
  // so a synchronous throw while building the arguments cannot escape either.
  try {
    await postInternalNote(
      env, conversation.id, issue.idReadable, handoff.assignee
    );
  } catch (e) {
    console.error('postInternalNote failed:', e);
  }
}

/**
 * Rewrite an existing ticket from the current state of the conversation.
 *
 * Customers routinely open with "hello" and explain themselves afterwards, so
 * a ticket built only from what existed at handoff is thin. Rather than delay
 * creation — which would delay the SLA clock too — the ticket is created at
 * handoff and its body kept current as the customer keeps typing.
 */
async function refreshTicket(env, conversationId, ticketId) {
  // Older entries stored a bare marker rather than a ticket id.
  if (!/^[A-Za-z]+-\d+$/.test(ticketId)) return;

  const conversation = await fetchConversation(env.INTERCOM_TOKEN, conversationId);
  if (!conversation) return;

  const contact = extractContact(conversation);
  let email = contact.email;
  if (!email && contact.id && env.INTERCOM_TOKEN) {
    email = await fetchContactEmail(env.INTERCOM_TOKEN, contact.id);
  }

  const description = buildDescription({
    sender: contact.name || email || 'Intercom contact',
    email,
    createdAt: conversation.created_at,
    link: conversationLink(env.INTERCOM_APP_ID, conversationId),
    subject: (conversation.source?.subject || '').trim(),
    transcript: buildTranscript(conversation),
    attachments: allAttachments(conversation),
    escalatedReason:
      conversation.custom_attributes?.['Fin AI Agent escalated reason'] || '',
  });

  await updateYouTrackIssue({
    baseUrl: env.YOUTRACK_BASE_URL,
    token: env.YOUTRACK_TOKEN,
    issueId: ticketId,
    description,
  });

  console.log(`refreshed ${ticketId} from conversation ${conversationId}`);
}

// --------------------------------------------------------------------------
// Internal note
// --------------------------------------------------------------------------

/**
 * Add an INTERNAL NOTE to the Intercom conversation naming the ticket.
 *
 * message_type is hardcoded to 'note' and must stay that way. The same
 * endpoint sends a customer-visible reply when message_type is 'comment', so
 * this one field is the whole difference between an internal annotation and
 * messaging the customer. Nothing this worker writes should ever be visible to
 * a customer — do not make this configurable.
 */
async function postInternalNote(env, conversationId, ticketId, assigneeId) {
  if (!env.INTERCOM_TOKEN) {
    console.error('no INTERCOM_TOKEN — cannot post the note');
    return;
  }

  const adminId =
    env.INTERCOM_NOTE_ADMIN_ID ||
    (await fetchTokenOwnerAdminId(env.INTERCOM_TOKEN)) ||
    String(assigneeId || '');

  if (!adminId) {
    console.error('no admin id available — skipping the note');
    return;
  }

  const base = (env.YOUTRACK_BASE_URL || '').replace(/\/$/, '');
  const prefix = env.INTERCOM_NOTE_PREFIX || '';
  const body =
    (prefix ? `<b>${prefix}</b><br><br>` : '') +
    `YouTrack ticket <a href="${base}/tickets/${ticketId}">${ticketId}</a>` +
    ' has been created for this conversation.<br><br>' +
    '<i>Internal note — the customer cannot see this, and no reply was sent' +
    ' to them.</i>';

  const res = await fetch(
    `https://api.intercom.io/conversations/${conversationId}/reply`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${env.INTERCOM_TOKEN}`,
        'Content-Type': 'application/json',
        Accept: 'application/json',
        'Intercom-Version': '2.11',
      },
      body: JSON.stringify({
        message_type: 'note', // never 'comment' — see the note above
        type: 'admin',
        admin_id: String(adminId),
        body,
      }),
    }
  );

  if (!res.ok) {
    throw new Error(`note rejected (${res.status}): ${await res.text()}`);
  }

  console.log(`note added to conversation ${conversationId} for ${ticketId}`);
}

/** The admin who owns INTERCOM_TOKEN — the note is attributed to them. */
async function fetchTokenOwnerAdminId(token) {
  try {
    const res = await fetch('https://api.intercom.io/me', {
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/json',
        'Intercom-Version': '2.11',
      },
    });
    if (!res.ok) return '';
    const data = await res.json();
    return data.id ? String(data.id) : '';
  } catch (e) {
    console.error('admin lookup failed:', e);
    return '';
  }
}

// --------------------------------------------------------------------------
// Intercom helpers
// --------------------------------------------------------------------------

/**
 * Intercom signs the raw request body with HMAC-SHA1 using the app's client
 * secret and sends it as `X-Hub-Signature: sha1=<hex>`.
 */
async function verifyIntercomSignature(request, rawBody, clientSecret) {
  const header = request.headers.get('x-hub-signature');
  if (!header || !clientSecret) return false;
  const expected = `sha1=${await hmacHex('SHA-1', clientSecret, rawBody)}`;
  return timingSafeEqual(expected, header);
}

const CONTACT_AUTHOR_TYPES = ['user', 'lead', 'contact'];

/**
 * Find the customer on the conversation.
 *
 * `source` is the conversation's FIRST message, and on website chat that is
 * Fin's greeting — so source.author is the operator, not the customer. Trust
 * the author only when it is actually a contact; otherwise take the contacts
 * list, which is the customer either way.
 */
function extractContact(conversation) {
  const author = conversation.source?.author || {};
  if (CONTACT_AUTHOR_TYPES.includes(author.type) && (author.email || author.name)) {
    return { id: author.id, email: author.email || '', name: author.name || '' };
  }
  const first = conversation.contacts?.contacts?.[0] || {};
  return { id: first.id, email: first.email || '', name: first.name || '' };
}

/**
 * Fin records a handoff as state, not as an assignment or an event — and it
 * uses more than one word for it depending on how the handoff came about:
 *
 *   escalated       the customer asked for a human
 *   routed_to_team  Fin decided by itself to pass the conversation on
 *
 * Both mean the same thing to us. The list is configurable because this
 * vocabulary is Intercom's to change, and a state we do not recognise is
 * silently treated as "still with Fin" — so when the skip log shows an
 * unfamiliar state, add it here rather than rewriting the check.
 */
const DEFAULT_HANDOFF_STATES = 'escalated,routed_to_team';

function isEscalated(conversation, env) {
  const states = (env.INTERCOM_HANDOFF_STATES || DEFAULT_HANDOFF_STATES)
    .split(',')
    .map((x) => x.trim().toLowerCase())
    .filter(Boolean);

  const fromAgent = String(
    conversation.ai_agent?.resolution_state || ''
  ).toLowerCase();
  const fromAttrs = String(
    conversation.custom_attributes?.['Fin AI Agent resolution state'] || ''
  ).toLowerCase();

  return states.includes(fromAgent) || states.includes(fromAttrs);
}

/** Every message in the conversation, oldest first, with who said it. */
function messages(conversation) {
  const out = [];

  const src = conversation.source || {};
  if (src.body) {
    out.push({
      who: CONTACT_AUTHOR_TYPES.includes(src.author?.type) ? 'customer' : 'fin',
      text: htmlToText(src.body),
      at: conversation.created_at,
      attachments: src.attachments || [],
    });
  }

  for (const part of conversation.conversation_parts?.conversation_parts || []) {
    if (part.part_type !== 'comment') continue;
    const text = htmlToText(part.body || '');
    const attachments = part.attachments || [];
    if (!text && !attachments.length) continue;
    out.push({
      who: CONTACT_AUTHOR_TYPES.includes(part.author?.type) ? 'customer' : 'fin',
      text,
      at: part.created_at,
      attachments,
    });
  }

  return out;
}

/**
 * What the ticket should be called.
 *
 * The customer's FIRST message is often just "hello" or "human", so using it
 * made the summary useless. The longest message they sent is nearly always
 * the one that actually states the problem.
 */
function pickSummary(conversation) {
  const mine = messages(conversation).filter((m) => m.who === 'customer' && m.text);
  if (!mine.length) return '';
  return mine.reduce((best, m) => (m.text.length > best.text.length ? m : best)).text;
}

/** The whole exchange, so the agent can see what Fin already said. */
function buildTranscript(conversation) {
  const lines = [];
  for (const m of messages(conversation)) {
    const label = m.who === 'customer' ? 'Customer' : 'Fin';
    lines.push(`**${label}** · ${formatUtc(m.at)} UTC`, '', m.text || '_(no text)_', '');
    for (const a of m.attachments) {
      lines.push(`  📎 [${a.name || 'attachment'}](${a.url})`);
    }
    if (m.attachments.length) lines.push('');
  }
  return lines.join('\n').trim();
}

function allAttachments(conversation) {
  return messages(conversation).flatMap((m) => m.attachments);
}

async function fetchConversation(token, id) {
  try {
    const res = await fetch(`https://api.intercom.io/conversations/${id}`, {
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/json',
        'Intercom-Version': '2.11',
      },
    });
    if (!res.ok) {
      console.error(`conversation fetch failed (${res.status}) — using payload`);
      return null;
    }
    return await res.json();
  } catch (e) {
    console.error('conversation fetch threw:', e);
    return null;
  }
}

async function fetchContactEmail(token, contactId) {
  try {
    const res = await fetch(`https://api.intercom.io/contacts/${contactId}`, {
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/json',
        'Intercom-Version': '2.11',
      },
    });
    if (!res.ok) return '';
    const data = await res.json();
    return data.email || '';
  } catch (e) {
    console.error('contact lookup failed:', e);
    return '';
  }
}

function conversationLink(appId, conversationId) {
  if (!appId) return '';
  return `https://app.intercom.com/a/inbox/${appId}/inbox/conversation/${conversationId}`;
}

// --------------------------------------------------------------------------
// Formatting
// --------------------------------------------------------------------------

function buildDescription({
  sender, email, createdAt, link, subject, transcript, attachments, escalatedReason,
}) {
  // Don't print "name <email>" when the name IS the email.
  const who = email && email !== sender ? `${sender} <${email}>` : sender;
  const lines = [
    '**Source:** Intercom',
    `**Sender:** ${who}`,
    `**Received:** ${formatUtc(createdAt)} UTC`,
  ];
  if (link) lines.push(`**Conversation:** ${link}`);
  if (subject) lines.push(`**Subject:** ${subject}`);
  if (escalatedReason) lines.push(`**Escalated:** ${escalatedReason}`);

  if (attachments?.length) {
    lines.push('', '**Attachments:**', '');
    for (const a of attachments) {
      lines.push(`- [${a.name || 'attachment'}](${a.url})`);
    }
  }

  lines.push('', '---', '', '**Conversation:**', '', transcript || '_(no messages)_');
  return lines.join('\n');
}

/** Intercom timestamps are unix seconds. */
function formatUtc(seconds) {
  const ms = seconds ? Number(seconds) * 1000 : Date.now();
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(
    d.getUTCHours()
  )}:${p(d.getUTCMinutes())}`;
}

function htmlToText(html) {
  return html
    .replace(/<\s*br\s*\/?>/gi, '\n')
    .replace(/<\/\s*p\s*>/gi, '\n\n')
    .replace(/<[^>]+>/g, '')
    .replace(/&nbsp;/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function summarize(text) {
  const firstLine = text.split('\n')[0].trim() || text.trim();
  return firstLine.length > 140 ? `${firstLine.slice(0, 137)}…` : firstLine;
}

// --------------------------------------------------------------------------
// Dedupe + crypto utilities
// --------------------------------------------------------------------------

const seenConversations = new Map();

/** The ticket this conversation already produced, or null. Read-only. */
async function processedTicket(env, conversationId) {
  if (!conversationId) return null;
  if (env.DEDUPE) return await env.DEDUPE.get(`intercom:${conversationId}`);
  return seenConversations.get(conversationId) || null;
}

/** Record the ticket id. Called only after creation actually succeeded. */
async function markProcessed(env, conversationId, ticketId) {
  if (!conversationId) return;
  if (env.DEDUPE) {
    await env.DEDUPE.put(`intercom:${conversationId}`, ticketId, {
      expirationTtl: 60 * 60 * 24 * 30,
    });
    return;
  }
  seenConversations.set(conversationId, ticketId);
  if (seenConversations.size > 1000) seenConversations.clear();
}

async function hmacHex(hash, secret, message) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw',
    enc.encode(secret),
    { name: 'HMAC', hash },
    false,
    ['sign']
  );
  const sig = await crypto.subtle.sign('HMAC', key, enc.encode(message));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
