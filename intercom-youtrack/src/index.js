/**
 * Intercom → YouTrack connector (Cloudflare Worker)
 *
 * Flow:
 *   1. Receive an Intercom webhook POST.
 *   2. Verify the X-Hub-Signature (HMAC-SHA1 of the raw body with the app's
 *      client secret) — rejects forged requests.
 *   3. Trigger = HUMAN ESCALATION ONLY. We create a ticket when a conversation
 *      is assigned to a human teammate (topic `conversation.admin.assigned`),
 *      excluding assignments to Fin / operator bots.
 *   4. Create a YouTrack CS ticket with the contact's email.
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

  // Identify who it was assigned to. Skip assignments to Fin / operator bots.
  const assignee = conversation.admin_assignee_id;
  const excluded = (env.INTERCOM_EXCLUDE_ADMIN_IDS || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
  if (!assignee) return; // unassigned / assigned to a team only — not a human
  if (excluded.includes(String(assignee))) return; // assigned to Fin/operator

  // Dedupe: one ticket per conversation even if reassigned later.
  if (await alreadyProcessed(env, conversation.id)) return;

  const contact = extractContact(conversation);
  // Email may be absent in the webhook payload — look it up if we have a token.
  let email = contact.email;
  if (!email && contact.id && env.INTERCOM_TOKEN) {
    email = await fetchContactEmail(env.INTERCOM_TOKEN, contact.id);
  }

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

  await createYouTrackIssue({
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

function extractContact(conversation) {
  // Webhook payloads vary: prefer source.author, fall back to contacts list.
  const author = conversation.source?.author || {};
  if (author.email || author.name) {
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
