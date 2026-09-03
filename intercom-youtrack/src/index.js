/**
 * Intercom → YouTrack connector (Cloudflare Worker)
 *
 * Flow:
 *   1. Receive an Intercom webhook POST.
 *   2. Verify the X-Hub-Signature (HMAC-SHA1 of the raw body with the app's
 *      client secret) — rejects forged requests.
 *   3. Trigger = HANDOFF TO HUMANS. Topic `conversation.admin.assigned`,
 *      which fires both when a named teammate is assigned and when the
 *      conversation lands in a team inbox. Either counts; assignment to Fin
 *      or another operator bot does not.
 *   4. Create a YouTrack CS ticket with the contact's email.
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

import { createYouTrackIssue } from './youtrack.js';

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

async function handleEvent(payload, env) {
  // We only act on human escalation.
  if (payload.topic !== 'conversation.admin.assigned') return;

  const conversation = payload.data?.item;
  if (!conversation) return;

  // conversation.admin.assigned fires for BOTH kinds of assignment:
  //   - to a named teammate  -> admin_assignee_id set
  //   - to a team inbox      -> admin_assignee_id null, team_assignee_id set
  //
  // The team case is the moment Fin stops handling the conversation and hands
  // it to humans. That is when the ticket should exist and the SLA clock
  // should start — not whenever someone eventually opens it, which could be
  // hours later and would make First Reply measure the wrong thing.
  const assignee = conversation.admin_assignee_id;
  const teamAssignee = conversation.team_assignee_id;
  const excluded = (env.INTERCOM_EXCLUDE_ADMIN_IDS || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);

  const toHuman = Boolean(assignee) && !excluded.includes(String(assignee));
  const toTeam = Boolean(teamAssignee);
  if (!toHuman && !toTeam) {
    console.log(
      `skip ${conversation.id}: admin=${assignee ?? 'none'} team=none` +
        ' (still with Fin, or unassigned)'
    );
    return;
  }

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

  // Dedupe: one ticket per conversation even if reassigned later. Checked after
  // the filters so a skipped conversation is not recorded as handled.
  if (await alreadyProcessed(env, conversation.id)) return;

  const firstMessage = htmlToText(conversation.source?.body || '');
  const subject = (conversation.source?.subject || '').trim();
  const summary = summarize(subject || firstMessage || `Conversation ${conversation.id}`);

  const description = buildDescription({
    sender: contact.name || email || 'Intercom contact',
    email,
    createdAt: conversation.created_at,
    link: conversationLink(env.INTERCOM_APP_ID, conversation.id),
    subject,
    message: firstMessage,
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

  // Best effort: a failed note must never lose the ticket we just created.
  await postInternalNote(env, conversation.id, issue.idReadable, assignee).catch(
    (e) => console.error('postInternalNote failed:', e)
  );
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

function buildDescription({ sender, email, createdAt, link, subject, message }) {
  const lines = [
    'Source: Intercom',
    `Sender: ${sender}${email ? ` <${email}>` : ''}`,
    `Received: ${formatUtc(createdAt)} UTC`,
  ];
  if (link) lines.push(`Conversation: ${link}`);
  if (subject) lines.push(`Subject: ${subject}`);
  lines.push('', 'Full message:', '', message || '(no message body)');
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

const seenConversations = new Set();
async function alreadyProcessed(env, conversationId) {
  if (!conversationId) return false;
  if (env.DEDUPE) {
    const key = `intercom:${conversationId}`;
    if (await env.DEDUPE.get(key)) return true;
    await env.DEDUPE.put(key, '1', { expirationTtl: 60 * 60 * 24 });
    return false;
  }
  if (seenConversations.has(conversationId)) return true;
  seenConversations.add(conversationId);
  if (seenConversations.size > 1000) seenConversations.clear();
  return false;
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
