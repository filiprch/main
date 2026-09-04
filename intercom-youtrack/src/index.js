/**
 * Intercom → YouTrack connector (Cloudflare Worker)
 *
 * WHAT MAKES A TICKET
 *
 * Fin hands a conversation to humans. Intercom fires no event for this — it
 * sets "Fin AI Agent resolution state" and leaves the conversation unassigned
 * — so we watch the topics that DO fire (Fin's own replies come through
 * `conversation.operator.replied`, because Fin is an Operator and not a
 * teammate) and read that state.
 *
 * That state is the ONLY trigger. Assignment is not one: a conversation
 * assigned to a teammate is one a person is already handling, and counting it
 * filed a ticket every time an agent replied to a client.
 *
 * WHY THE TICKET IS NOT CREATED IMMEDIATELY
 *
 * Customers open with "hello" and go hunting for a screenshot only once Fin
 * has already given up. A ticket cut at the moment of handoff is therefore
 * almost empty. Instead the handoff arms a timer: DEBOUNCE_SECONDS after the
 * customer's LAST message, a scheduled pass builds the ticket from everything
 * they said. Each new message pushes the timer back.
 *
 * Cloudflare Workers cannot sleep for minutes, so the timer lives in KV and a
 * cron pass sweeps it once a minute. Real delay lands between DEBOUNCE_SECONDS
 * and DEBOUNCE_SECONDS + 60.
 *
 * WHAT THE TICKET CONTAINS
 *
 * Only the customer's words. Fin's replies are excluded — they turned the
 * description into a wall of text nobody would read — and are represented by
 * the titles of the articles Fin tried, which Intercom hands us for free in
 * ai_agent.content_sources. Attachments are re-uploaded into YouTrack rather
 * than linked, so they outlive Intercom's URLs. Anything the customer says
 * after the ticket exists arrives as a comment.
 *
 * Nothing here is ever visible to the customer: the only thing written back to
 * Intercom is an internal note.
 *
 * Secrets / vars:
 *   INTERCOM_CLIENT_SECRET     (secret) verifies X-Hub-Signature
 *   INTERCOM_TOKEN             (secret) Intercom access token
 *   YOUTRACK_TOKEN             (secret) YouTrack permanent token
 *   YOUTRACK_BASE_URL          (var)    https://myrealprofit.youtrack.cloud
 *   YOUTRACK_PROJECT_ID        (var)    internal project id, e.g. 0-18
 *   INTERCOM_APP_ID            (var)    for conversation deep-links
 *   INTERCOM_HANDOFF_STATES    (var)    states meaning "handed to humans"
 *   INTERCOM_DEBOUNCE_SECONDS  (var)    quiet period before creating
 *   INTERCOM_NOTE_ADMIN_ID     (var)    admin the internal note posts as
 *   INTERCOM_NOTE_PREFIX       (var)    bold first line of the note
 *   DEDUPE                     (KV)     REQUIRED — timers and ticket ids
 */

import {
  createYouTrackIssue,
  addYouTrackComment,
  attachToYouTrackIssue,
} from './youtrack.js';

/**
 * Only Fin's replies and the customer's own messages.
 *
 * conversation.admin.assigned and .replied are deliberately NOT here. A
 * teammate picking up or answering a conversation is not a handoff — it is a
 * human already doing the work — and treating it as one filed a ticket every
 * time Lisa replied to a client.
 */
const HANDLED_TOPICS = ['conversation.operator.replied', 'conversation.user.replied'];

/** A customer message can land before Fin has finished deciding. */
const TOPICS_WORTH_RECHECKING = ['conversation.user.replied'];

const CONTACT_AUTHOR_TYPES = ['user', 'lead', 'contact'];
const DEFAULT_HANDOFF_STATES = 'escalated,routed_to_team';
const DEFAULT_DEBOUNCE_SECONDS = 180;
const RECHECK_MS = 8000;
const MAX_CREATE_ATTEMPTS = 5;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// KV key shapes, all in the DEDUPE namespace.
const kTicket = (id) => `intercom:${id}`; // conversation -> ticket id
const kPending = (id) => `pending:${id}`; // conversation -> { dueAt, attempts }
const kSeen = (id) => `seen:${id}`; // conversation -> last part id in the ticket

export default {
  async fetch(request, env, ctx) {
    if (request.method !== 'POST') {
      return new Response('Method Not Allowed', { status: 405 });
    }

    const rawBody = await request.text();
    if (!(await verifyIntercomSignature(request, rawBody, env.INTERCOM_CLIENT_SECRET))) {
      return new Response('Invalid signature', { status: 401 });
    }

    let payload;
    try {
      payload = JSON.parse(rawBody);
    } catch {
      return new Response('Bad JSON', { status: 400 });
    }

    if (payload.topic === 'ping') return new Response('pong', { status: 200 });

    // Kill switch. Checked after the ping so Intercom's "Send a test request"
    // still succeeds while the integration is off, and returns 200 so Intercom
    // does not retry or mark the endpoint unhealthy.
    if (!isEnabled(env)) {
      console.log('disabled by ENABLED=false — ignoring event');
      return new Response('', { status: 200 });
    }

    ctx.waitUntil(
      Promise.all([
        handleEvent(payload, env).catch((e) => console.error('handleEvent error:', e)),
        // Check THIS conversation's timer only — never the whole queue.
        // Listing on every webhook raced with itself (several deliveries
        // arriving together each filed a ticket for the same conversation)
        // and burned the KV free tier's 1,000 daily list operations.
        createIfDue(env, payload.data?.item?.id).catch((e) =>
          console.error('createIfDue error:', e)
        ),
      ])
    );
    return new Response('', { status: 200 });
  },

  /** Cron sweep: create the tickets whose quiet period has elapsed. */
  async scheduled(event, env, ctx) {
    if (!isEnabled(env)) {
      console.log('disabled by ENABLED=false — skipping sweep');
      return;
    }
    ctx.waitUntil(sweepPending(env).catch((e) => console.error('sweep error:', e)));
  },
};

// --------------------------------------------------------------------------
// Webhook
// --------------------------------------------------------------------------

/** Off unless ENABLED is exactly "false", so a missing value stays on. */
function isEnabled(env) {
  return String(env.ENABLED ?? 'true').toLowerCase() !== 'false';
}

async function handleEvent(payload, env) {
  if (!HANDLED_TOPICS.includes(payload.topic)) return;

  const item = payload.data?.item;
  if (!item?.id) return;
  if (!env.DEDUPE) {
    console.error('DEDUPE KV is not bound — cannot schedule or dedupe');
    return;
  }

  // A ticket already exists. Everything the customer says from here is a
  // comment on it; anything else is ignored.
  const ticketId = await env.DEDUPE.get(kTicket(item.id));
  if (ticketId) {
    if (payload.topic === 'conversation.user.replied') {
      try {
        await commentNewMessages(env, item.id, ticketId);
      } catch (e) {
        console.error(`comment on ${ticketId} failed:`, e);
      }
    }
    return;
  }

  let conversation =
    (env.INTERCOM_TOKEN && (await fetchConversation(env.INTERCOM_TOKEN, item.id))) || item;
  let handoff = handoffState(conversation, env);

  // Fin sets the state a beat after the message that causes it, and its own
  // replies arrive on operator.replied where the state is already settled —
  // so only customer-side topics are worth a second look.
  if (!handoff.create && TOPICS_WORTH_RECHECKING.includes(payload.topic)) {
    await sleep(RECHECK_MS);
    const fresh =
      env.INTERCOM_TOKEN && (await fetchConversation(env.INTERCOM_TOKEN, item.id));
    if (fresh) {
      conversation = fresh;
      handoff = handoffState(conversation, env);
    }
  }

  if (!handoff.create) {
    console.log(
      `skip ${item.id}: not escalated ` +
        `(state=${conversation.ai_agent?.resolution_state ?? 'unknown'})`
    );
    return;
  }

  // Handed over. Arm (or push back) the quiet period. Every customer message
  // lands here again and moves the deadline, which is the reset.
  const seconds = Number(env.INTERCOM_DEBOUNCE_SECONDS) || DEFAULT_DEBOUNCE_SECONDS;
  const dueAt = Date.now() + seconds * 1000;
  await env.DEDUPE.put(
    kPending(item.id),
    JSON.stringify({ dueAt, attempts: 0 }),
    { expirationTtl: 60 * 60 * 24 }
  );
  console.log(`armed ${item.id}: ticket due in ${seconds}s`);
}

// --------------------------------------------------------------------------
// Cron sweep
// --------------------------------------------------------------------------

async function sweepPending(env) {
  if (!env.DEDUPE) return;

  const { keys } = await env.DEDUPE.list({ prefix: 'pending:' });
  const now = Date.now();
  if (!keys.length) return;

  const due = [];
  for (const key of keys) {
    const raw = await env.DEDUPE.get(key.name);
    if (!raw) continue;

    let state;
    try {
      state = JSON.parse(raw);
    } catch {
      await env.DEDUPE.delete(key.name);
      continue;
    }

    if (state.dueAt > now) continue; // customer is still typing

    const conversationId = key.name.slice('pending:'.length);
    due.push(conversationId);
    await claimAndCreate(env, conversationId, state);
  }

  console.log(`sweep: ${keys.length} pending, ${due.length} due${due.length ? ` (${due.join(', ')})` : ''}`);
}

/** Is this one conversation's quiet period up? Cheap: one KV read. */
async function createIfDue(env, conversationId) {
  if (!conversationId || !env.DEDUPE) return;
  const raw = await env.DEDUPE.get(kPending(conversationId));
  if (!raw) return;

  let state;
  try {
    state = JSON.parse(raw);
  } catch {
    await env.DEDUPE.delete(kPending(conversationId));
    return;
  }
  if (state.dueAt > Date.now()) return;

  await claimAndCreate(env, conversationId, state);
}

/**
 * Claim the timer, then create.
 *
 * The delete comes FIRST and is the claim. Two passes running at once would
 * otherwise both read the timer, both find no ticket yet, and both file one —
 * which is exactly what produced three tickets for a single conversation. The
 * has-a-ticket check inside createTicketFor is the second line of defence.
 */
async function claimAndCreate(env, conversationId, state) {
  await env.DEDUPE.delete(kPending(conversationId));
  try {
    await createTicketFor(env, conversationId);
  } catch (e) {
    const attempts = (state.attempts || 0) + 1;
    console.error(`create for ${conversationId} failed (attempt ${attempts}):`, e);
    if (attempts < MAX_CREATE_ATTEMPTS) {
      // Put the timer back so a later pass retries, rather than hammering a
      // dependency that is already unhappy.
      await env.DEDUPE.put(
        kPending(conversationId),
        JSON.stringify({ dueAt: Date.now() + 60_000, attempts }),
        { expirationTtl: 60 * 60 * 24 }
      );
    } else {
      console.error(`giving up on ${conversationId} after ${attempts} attempts`);
    }
  }
}

async function createTicketFor(env, conversationId) {
  // Another pass may have got there first.
  if (await env.DEDUPE.get(kTicket(conversationId))) return;

  const conversation = await fetchConversation(env.INTERCOM_TOKEN, conversationId);
  if (!conversation) throw new Error('could not fetch conversation');

  const contact = extractContact(conversation);
  let email = contact.email;
  if (!email && contact.id) {
    email = await fetchContactEmail(env.INTERCOM_TOKEN, contact.id);
  }

  const said = customerMessages(conversation);
  const attachments = said.flatMap((m) => m.attachments);

  const issue = await createYouTrackIssue({
    baseUrl: env.YOUTRACK_BASE_URL,
    token: env.YOUTRACK_TOKEN,
    projectId: env.YOUTRACK_PROJECT_ID || 'CS',
    summary: buildTitle({
      source: 'INT',
      problem: pickSummary(said, conversation),
      sender: contact.name || email,
      // Populated only when the contact is linked to a company; website
      // visitors are usually anonymous, so this is often absent.
      account: conversation.company?.name || '',
    }),
    description: buildDescription({
      sender: contact.name || email || 'Intercom contact',
      email,
      link: conversationLink(env.INTERCOM_APP_ID, conversationId),
      escalatedReason:
        conversation.custom_attributes?.['Fin AI Agent escalated reason'] || '',
      createdAt: conversation.created_at,
      said,
      attachments,
      finTried: finArticles(conversation),
    }),
    channel: 'Intercom',
    type: 'Task',
    replied: 'Not Replied',
    customerEmail: email || undefined,
  });

  // Claim the conversation before anything that can fail, so a later retry
  // cannot produce a second ticket.
  await env.DEDUPE.put(kTicket(conversationId), issue.idReadable, {
    expirationTtl: 60 * 60 * 24 * 30,
  });
  const lastPart = said.length ? said[said.length - 1].id : '';
  await env.DEDUPE.put(kSeen(conversationId), String(lastPart), {
    expirationTtl: 60 * 60 * 24 * 30,
  });

  console.log(
    `created ${issue.idReadable} for ${conversationId}` +
      ` (${said.length} customer messages, ${attachments.length} attachments)`
  );

  for (const a of attachments) {
    try {
      await attachToYouTrackIssue({
        baseUrl: env.YOUTRACK_BASE_URL,
        token: env.YOUTRACK_TOKEN,
        issueId: issue.idReadable,
        name: a.name,
        url: a.url,
        authHeader: `Bearer ${env.INTERCOM_TOKEN}`,
      });
    } catch (e) {
      console.error(`attachment "${a.name}" failed:`, e);
    }
  }

  try {
    await postInternalNote(env, conversationId, issue.idReadable);
  } catch (e) {
    console.error('postInternalNote failed:', e);
  }
}

/** Everything the customer has said since the ticket was written. */
async function commentNewMessages(env, conversationId, ticketId) {
  const conversation = await fetchConversation(env.INTERCOM_TOKEN, conversationId);
  if (!conversation) return;

  const seen = await env.DEDUPE.get(kSeen(conversationId));
  const said = customerMessages(conversation);

  const cut = said.findIndex((m) => String(m.id) === String(seen));
  const fresh = cut === -1 ? said : said.slice(cut + 1);
  if (!fresh.length) return;

  const lines = ['**New from the customer in Intercom**', ''];
  for (const m of fresh) lines.push(m.text || '_(attachment only)_', '');

  await addYouTrackComment({
    baseUrl: env.YOUTRACK_BASE_URL,
    token: env.YOUTRACK_TOKEN,
    issueId: ticketId,
    text: lines.join('\n').trim(),
  });

  for (const a of fresh.flatMap((m) => m.attachments)) {
    try {
      await attachToYouTrackIssue({
        baseUrl: env.YOUTRACK_BASE_URL,
        token: env.YOUTRACK_TOKEN,
        issueId: ticketId,
        name: a.name,
        url: a.url,
        authHeader: `Bearer ${env.INTERCOM_TOKEN}`,
      });
    } catch (e) {
      console.error(`late attachment "${a.name}" failed:`, e);
    }
  }

  await env.DEDUPE.put(kSeen(conversationId), String(fresh[fresh.length - 1].id), {
    expirationTtl: 60 * 60 * 24 * 30,
  });
  console.log(`commented ${fresh.length} new message(s) on ${ticketId}`);
}

// --------------------------------------------------------------------------
// Reading the conversation
// --------------------------------------------------------------------------

/**
 * Only what the customer wrote.
 *
 * `source` is the conversation's first message and on website chat that is
 * Fin's greeting, so it counts only when its author is genuinely a contact.
 */
function customerMessages(conversation) {
  const out = [];

  const src = conversation.source || {};
  if (CONTACT_AUTHOR_TYPES.includes(src.author?.type) && (src.body || src.attachments?.length)) {
    out.push({
      id: `source-${conversation.id}`,
      text: htmlToText(src.body || ''),
      at: conversation.created_at,
      attachments: src.attachments || [],
    });
  }

  for (const part of conversation.conversation_parts?.conversation_parts || []) {
    if (part.part_type !== 'comment') continue;
    if (!CONTACT_AUTHOR_TYPES.includes(part.author?.type)) continue;
    const text = htmlToText(part.body || '');
    const attachments = part.attachments || [];
    if (!text && !attachments.length) continue;
    out.push({ id: part.id, text, at: part.created_at, attachments });
  }

  return out;
}

/** The customer's longest message — "hello" makes a poor ticket title. */
function pickSummary(said, conversation) {
  const subject = (conversation.source?.subject || '').trim();
  if (subject) return summarize(subject);

  const withText = said.filter((m) => m.text);
  if (!withText.length) return summarize(`Conversation ${conversation.id}`);

  const longest = withText.reduce((a, b) => (b.text.length > a.text.length ? b : a));
  return summarize(longest.text);
}

/** What Fin tried, as the titles of the articles it drew on. */
function finArticles(conversation) {
  const sources = conversation.ai_agent?.content_sources?.content_sources || [];
  return [...new Set(sources.map((s) => s.title).filter(Boolean))];
}

function isEscalated(conversation, env) {
  const states = (env.INTERCOM_HANDOFF_STATES || DEFAULT_HANDOFF_STATES)
    .split(',')
    .map((x) => x.trim().toLowerCase())
    .filter(Boolean);

  const a = String(conversation.ai_agent?.resolution_state || '').toLowerCase();
  const b = String(
    conversation.custom_attributes?.['Fin AI Agent resolution state'] || ''
  ).toLowerCase();
  return states.includes(a) || states.includes(b);
}

/**
 * Handed to humans?
 *
 * Fin's own resolution state is the ONLY signal. Assignment is not: a
 * conversation assigned to a teammate is one a person is already handling,
 * and treating that as a handoff meant every reply Lisa sent to a client
 * created a ticket.
 */
function handoffState(conversation, env) {
  const escalated = isEscalated(conversation, env);
  return { create: escalated, escalated };
}

function extractContact(conversation) {
  const author = conversation.source?.author || {};
  if (CONTACT_AUTHOR_TYPES.includes(author.type) && (author.email || author.name)) {
    return { id: author.id, email: author.email || '', name: author.name || '' };
  }
  const first = conversation.contacts?.contacts?.[0] || {};
  return { id: first.id, email: first.email || '', name: first.name || '' };
}

async function fetchConversation(token, id) {
  if (!token) return null;
  try {
    const res = await intercomGet(token, `/conversations/${id}`);
    if (!res.ok) {
      console.error(`conversation fetch failed (${res.status})`);
      return null;
    }
    return await res.json();
  } catch (e) {
    console.error('conversation fetch threw:', e);
    return null;
  }
}

async function fetchContactEmail(token, contactId) {
  if (!token) return '';
  try {
    const res = await intercomGet(token, `/contacts/${contactId}`);
    if (!res.ok) return '';
    return (await res.json()).email || '';
  } catch (e) {
    console.error('contact lookup failed:', e);
    return '';
  }
}

async function fetchTokenOwnerAdminId(token) {
  try {
    const res = await intercomGet(token, '/me');
    if (!res.ok) return '';
    const data = await res.json();
    return data.id ? String(data.id) : '';
  } catch (e) {
    console.error('admin lookup failed:', e);
    return '';
  }
}

function intercomGet(token, path) {
  return fetch(`https://api.intercom.io${path}`, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/json',
      'Intercom-Version': '2.11',
    },
  });
}

function conversationLink(appId, conversationId) {
  if (!appId) return '';
  return `https://app.intercom.com/a/inbox/${appId}/inbox/conversation/${conversationId}`;
}

// --------------------------------------------------------------------------
// The ticket body
// --------------------------------------------------------------------------

function buildDescription({
  sender, email, link, escalatedReason, createdAt, said, attachments, finTried,
}) {
  const who = email && email !== sender ? `${sender} <${email}>` : sender;
  const lines = [
    `**Customer:** ${who}`,
    `**Started:** ${formatUtc(createdAt)} UTC`,
  ];
  if (escalatedReason) lines.push(`**Escalated:** ${escalatedReason}`);
  if (link) lines.push(`**Intercom:** [open the conversation](${link})`);

  lines.push('', '### What the customer said', '');
  if (said.length) {
    for (const m of said) lines.push(m.text || '_(attachment only)_', '');
  } else {
    lines.push('_(nothing — the customer asked for a person without explaining)_', '');
  }

  if (attachments.length) {
    lines.push('### Attachments', '');
    for (const a of attachments) lines.push(`- ${a.name || 'attachment'}`);
    lines.push('');
  }

  if (finTried.length) {
    lines.push('### Fin already tried', '');
    for (const t of finTried) lines.push(`- ${t}`);
    lines.push('', '_The customer still asked for a person._');
  }

  return lines.join('\n').trim();
}

// --------------------------------------------------------------------------
// Internal note back to Intercom
// --------------------------------------------------------------------------

/**
 * message_type is hardcoded to 'note' and must stay that way. The same
 * endpoint sends a customer-visible reply when it is 'comment', so this one
 * field is the whole difference between annotating a conversation and
 * messaging a client. Do not make it configurable.
 */
async function postInternalNote(env, conversationId, ticketId) {
  if (!env.INTERCOM_TOKEN) return;

  const adminId =
    env.INTERCOM_NOTE_ADMIN_ID || (await fetchTokenOwnerAdminId(env.INTERCOM_TOKEN));
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
    '<i>Internal note — the customer cannot see this, and no reply was sent to them.</i>';

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
        message_type: 'note', // never 'comment' — see above
        type: 'admin',
        admin_id: String(adminId),
        body,
      }),
    }
  );

  if (!res.ok) throw new Error(`note rejected (${res.status}): ${await res.text()}`);
  console.log(`note added to ${conversationId} for ${ticketId}`);
}

// --------------------------------------------------------------------------
// Formatting and crypto
// --------------------------------------------------------------------------

function formatUtc(seconds) {
  const d = new Date(seconds ? Number(seconds) * 1000 : Date.now());
  const p = (n) => String(n).padStart(2, '0');
  return (
    `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ` +
    `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`
  );
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
    .replace(/&#39;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

// --------------------------------------------------------------------------
// Ticket titles
// --------------------------------------------------------------------------

/**
 * Strip everything that makes a title unreadable on a board.
 *
 * A pasted link used to swallow the whole title — CS-172 was named after a
 * Google Sheets URL — so links go first, keeping their label where one exists.
 * Opening pleasantries go too: "Well, I've shared the list" is one word of
 * throat-clearing in a field where every character counts.
 */
function cleanForTitle(text) {
  return (text || '')
    .replace(/<(?:https?:\/\/|mailto:)[^|>]+\|([^>]+)>/g, '$1') // <url|label> -> label
    .replace(/<(?:https?:\/\/|mailto:)[^>]+>/g, '') // bare <url> -> gone
    .replace(/https?:\/\/\S+/gi, '')
    .replace(/\S+@\S+\.\S+/g, '')
    .replace(
      /^\s*(?:well|so|ok|okay|hi|hey|hello|good\s+(?:morning|afternoon|evening))\b[\s,.!—-]*/i,
      ''
    )
    .replace(/\s+/g, ' ')
    .trim();
}

function clip(text, max) {
  const t = (text || '').trim();
  if (t.length <= max) return t;
  const cut = t.slice(0, max);
  const space = cut.lastIndexOf(' ');
  return `${(space > max * 0.6 ? cut.slice(0, space) : cut).trim()}…`;
}

/**
 * SOURCE: what they want - who asked - which account
 *
 * The prefix makes the channel readable at a glance on a mixed board, and the
 * trailing name and account mean a ticket can be placed without opening it.
 */
function buildTitle({ source, problem, sender, account }) {
  const parts = [clip(cleanForTitle(problem), 70) || 'No message'];
  if (sender) parts.push(clip(sender, 40));
  if (account) parts.push(clip(account, 30));
  return `${source}: ${parts.join(' - ')}`;
}

function summarize(text) {
  const first = text.split('\n')[0].trim() || text.trim();
  return first.length > 140 ? `${first.slice(0, 137)}…` : first;
}

async function verifyIntercomSignature(request, rawBody, clientSecret) {
  const header = request.headers.get('x-hub-signature');
  if (!header || !clientSecret) return false;
  const expected = `sha1=${await hmacHex('SHA-1', clientSecret, rawBody)}`;
  return timingSafeEqual(expected, header);
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
