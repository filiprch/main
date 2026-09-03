/**
 * Slack → YouTrack connector (Cloudflare Worker)
 *
 * Flow:
 *   1. Receive a Slack Events API POST.
 *   2. Verify the Slack signing secret (rejects forged requests).
 *   3. Answer the url_verification challenge during setup.
 *   4. Ack with 200 immediately (Slack's 3s rule), do work in waitUntil().
 *   5. Trigger = first message in a thread (top-level message in a monitored,
 *      allowlisted channel). Threaded replies, edits, bot messages ignored.
 *   6. Create a YouTrack CS ticket and post a confirmation back in-thread.
 *
 * Secrets / vars (set via `wrangler secret put` or wrangler.toml [vars]):
 *   SLACK_SIGNING_SECRET   (secret)  Slack app signing secret
 *   SLACK_BOT_TOKEN        (secret)  xoxb-… bot token (needs users:read.email)
 *   YOUTRACK_TOKEN         (secret)  YouTrack permanent token
 *   YOUTRACK_BASE_URL      (var)     https://myrealprofit.youtrack.cloud
 *   YOUTRACK_PROJECT_ID    (var)     CS  (shortName or internal id)
 *   CUSTOMER_CHANNEL_IDS   (var)     comma-separated allowlist of channel IDs
 *   DEDUPE                 (KV, opt) namespace to dedupe Slack event_id retries
 */

import { createYouTrackIssue } from './youtrack.js';

export default {
  async fetch(request, env, ctx) {
    if (request.method !== 'POST') {
      return new Response('Method Not Allowed', { status: 405 });
    }

    const rawBody = await request.text();

    // --- Slack signature verification -------------------------------------
    const verified = await verifySlackSignature(request, rawBody, env.SLACK_SIGNING_SECRET);
    if (!verified) {
      return new Response('Invalid signature', { status: 401 });
    }

    let payload;
    try {
      payload = JSON.parse(rawBody);
    } catch {
      return new Response('Bad JSON', { status: 400 });
    }

    // --- URL verification handshake (one-time, during app setup) ----------
    if (payload.type === 'url_verification') {
      return new Response(payload.challenge, {
        headers: { 'Content-Type': 'text/plain' },
      });
    }

    // --- Everything else: ack fast, process in the background -------------
    if (payload.type === 'event_callback') {
      ctx.waitUntil(handleEvent(payload, env).catch((e) => console.error('handleEvent error:', e)));
    }
    return new Response('', { status: 200 });
  },
};

/**
 * Decide whether an event should become a ticket, then create it.
 */
async function handleEvent(payload, env) {
  const event = payload.event || {};

  // We only care about plain channel messages.
  if (event.type !== 'message') return;

  // Ignore message subtypes (edits, deletes, joins, bot_message, etc.).
  if (event.subtype) return;

  // Ignore anything posted by a bot (including ourselves) to avoid loops.
  if (event.bot_id) return;

  // Trigger = FIRST message in a thread. A top-level message has either no
  // thread_ts, or thread_ts === ts (it is the thread parent). Replies
  // (thread_ts !== ts) are ignored.
  if (event.thread_ts && event.thread_ts !== event.ts) return;

  // Channel allowlist.
  const allowed = (env.CUSTOMER_CHANNEL_IDS || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
  if (allowed.length && !allowed.includes(event.channel)) return;

  // Dedupe Slack retries on the unique event_id.
  if (await alreadyProcessed(env, payload.event_id)) return;

  const text = (event.text || '').trim();
  if (!text) return; // nothing meaningful to file

  // Gather sender identity, channel name, and a permalink for the header.
  const [user, channelName, permalink] = await Promise.all([
    getUser(env.SLACK_BOT_TOKEN, event.user),
    getChannelName(env.SLACK_BOT_TOKEN, event.channel),
    getPermalink(env.SLACK_BOT_TOKEN, event.channel, event.ts),
  ]);

  const description = buildDescription({
    sender: user.name,
    email: user.email,
    channelName,
    permalink,
    ts: event.ts,
    text,
  });

  const issue = await createYouTrackIssue({
    baseUrl: env.YOUTRACK_BASE_URL,
    token: env.YOUTRACK_TOKEN,
    projectId: env.YOUTRACK_PROJECT_ID || 'CS',
    summary: summarize(text),
    description,
    channel: 'Slack',
    type: 'Task',
    replied: 'Not Replied',
    // Requires the users:read.email bot scope. Left empty for guests and
    // Slack Connect users who expose no address; the auto-tag-and-route
    // workflow then falls back to the reporter.
    customerEmail: user.email || undefined,
  });

  const ticketId = issue.idReadable || issue.id;
  await postConfirmation(env.SLACK_BOT_TOKEN, event.channel, event.ts, ticketId);
}

// --------------------------------------------------------------------------
// Slack helpers
// --------------------------------------------------------------------------

async function verifySlackSignature(request, rawBody, signingSecret) {
  const timestamp = request.headers.get('x-slack-request-timestamp');
  const signature = request.headers.get('x-slack-signature');
  if (!timestamp || !signature || !signingSecret) return false;

  // Reject requests older than 5 minutes (replay protection).
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - Number(timestamp)) > 60 * 5) return false;

  const base = `v0:${timestamp}:${rawBody}`;
  const expected = `v0=${await hmacHex('SHA-256', signingSecret, base)}`;
  return timingSafeEqual(expected, signature);
}

async function getUser(token, userId) {
  if (!userId) return { name: 'Unknown user', email: '' };
  const data = await slackGet(token, 'users.info', { user: userId });
  const p = data?.user?.profile || {};
  return {
    name: p.display_name || p.real_name || data?.user?.name || userId,
    email: p.email || '',
  };
}

async function getChannelName(token, channelId) {
  const data = await slackGet(token, 'conversations.info', { channel: channelId });
  const name = data?.channel?.name;
  return name ? `#${name}` : channelId;
}

async function getPermalink(token, channelId, ts) {
  const data = await slackGet(token, 'chat.getPermalink', {
    channel: channelId,
    message_ts: ts,
  });
  return data?.permalink || '';
}

async function postConfirmation(token, channel, threadTs, ticketId) {
  const res = await fetch('https://slack.com/api/chat.postMessage', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json; charset=utf-8',
    },
    body: JSON.stringify({
      channel,
      thread_ts: threadTs,
      text: `✅ Ticket ${ticketId} created. Our team will respond shortly.`,
    }),
  });
  const data = await res.json();
  if (!data.ok) console.error('chat.postMessage failed:', data.error);
}

async function slackGet(token, method, params) {
  const url = new URL(`https://slack.com/api/${method}`);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
  const data = await res.json();
  if (!data.ok) console.error(`${method} failed:`, data.error);
  return data;
}

// --------------------------------------------------------------------------
// Formatting
// --------------------------------------------------------------------------

function buildDescription({ sender, email, channelName, permalink, ts, text }) {
  const lines = [
    '**Source:** Slack',
    `**Sender:** ${sender}${email ? ` <${email}>` : ''} (${channelName})`,
    `**Received:** ${formatUtc(ts)} UTC`,
  ];
  if (permalink) lines.push(`**Thread:** ${permalink}`);
  lines.push('', '**Full message:**', '', text);
  return lines.join('\n');
}

/** Slack ts is "<unix-seconds>.<micros>". Render as YYYY-MM-DD HH:MM. */
function formatUtc(ts) {
  const ms = ts ? Math.floor(Number(ts) * 1000) : Date.now();
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(
    d.getUTCHours()
  )}:${p(d.getUTCMinutes())}`;
}

/** First line, trimmed to a sensible title length. */
function summarize(text) {
  const firstLine = text.split('\n')[0].trim() || text.trim();
  return firstLine.length > 140 ? `${firstLine.slice(0, 137)}…` : firstLine;
}

// --------------------------------------------------------------------------
// Dedupe + crypto utilities
// --------------------------------------------------------------------------

/** Best-effort dedupe: KV if bound, else an in-isolate Set fallback. */
const seenEvents = new Set();
async function alreadyProcessed(env, eventId) {
  if (!eventId) return false;
  if (env.DEDUPE) {
    const key = `slack:${eventId}`;
    if (await env.DEDUPE.get(key)) return true;
    await env.DEDUPE.put(key, '1', { expirationTtl: 60 * 60 });
    return false;
  }
  if (seenEvents.has(eventId)) return true;
  seenEvents.add(eventId);
  if (seenEvents.size > 1000) seenEvents.clear();
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
