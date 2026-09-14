/**
 * Slack → YouTrack connector (Cloudflare Worker)
 *
 * Flow:
 *   1. Receive a Slack Events API POST.
 *   2. Verify the Slack signing secret (rejects forged requests).
 *   3. Answer the url_verification challenge during setup.
 *   4. Ack with 200 immediately (Slack's 3s rule), do work in waitUntil().
 *   5. Decide whether the event is a trigger (see SLACK_TRIGGERS below).
 *   6. Create a YouTrack CS ticket and confirm back in Slack.
 *
 * Secrets / vars (set via `wrangler secret put` or wrangler.toml [vars]):
 *   SLACK_SIGNING_SECRET   (secret)  Slack app signing secret
 *   SLACK_BOT_TOKEN        (secret)  xoxb-… bot token (needs users:read.email)
 *   YOUTRACK_TOKEN         (secret)  YouTrack permanent token
 *   YOUTRACK_BASE_URL      (var)     https://myrealprofit.youtrack.cloud
 *   YOUTRACK_PROJECT_ID    (var)     0-18 (internal id — NOT the shortName)
 *   CUSTOMER_CHANNEL_IDS   (var)     comma-separated allowlist of channel IDs
 *   SLACK_TRIGGERS         (var)     comma list: all | emoji | mention | keyword
 *   SLACK_TRIGGER_EMOJI    (var)     emoji names for `emoji` mode (no colons)
 *   SLACK_TRIGGER_KEYWORD  (var)     phrase for `keyword` mode
 *   SLACK_CONFIRM          (var)     thread | ephemeral | off
 *   ENABLED                (var)     "false" switches the whole worker off
 *   DEDUPE                 (KV, opt) namespace to dedupe events and messages
 */

import { createYouTrackIssue } from './youtrack.js';

const DEFAULT_TRIGGERS = 'all';
const DEFAULT_EMOJI = 'ticket';
const DEFAULT_KEYWORD = '!ticket';
const TICKETED_TTL_SECONDS = 60 * 60 * 24 * 30;

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

    // --- Kill switch ------------------------------------------------------
    // Checked AFTER the handshake above, so Slack can still verify the
    // Request URL while the integration is switched off. Returns 200 so Slack
    // treats the event as delivered rather than retrying it three times and
    // eventually marking the endpoint unhealthy.
    if (!isEnabled(env)) {
      console.log('disabled by ENABLED=false — ignoring event');
      return new Response('', { status: 200 });
    }

    // --- Everything else: ack fast, process in the background -------------
    if (payload.type === 'event_callback') {
      ctx.waitUntil(handleEvent(payload, env).catch((e) => console.error('handleEvent error:', e)));
    }
    return new Response('', { status: 200 });
  },
};

/** Off unless ENABLED is exactly "false", so a missing value stays on. */
function isEnabled(env) {
  return String(env.ENABLED ?? 'true').toLowerCase() !== 'false';
}

/**
 * Which events count as "file a ticket". Several can run at once — that is the
 * point of a comma list: you can leave `all` on in a test channel while trying
 * `emoji` in a real one.
 *
 *   all      every new thread (a top-level human message) — today's behaviour
 *   emoji    someone reacts to a message with SLACK_TRIGGER_EMOJI
 *   mention  a message @-mentions the bot
 *   keyword  a message contains SLACK_TRIGGER_KEYWORD
 *
 * An empty list means nothing is ever filed, which is a legitimate way to
 * watch the logs without touching YouTrack.
 */
function triggers(env) {
  return String(env.SLACK_TRIGGERS ?? DEFAULT_TRIGGERS)
    .split(',')
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
}

function channelAllowed(channelId, env) {
  const allowed = (env.CUSTOMER_CHANNEL_IDS || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
  return !allowed.length || allowed.includes(channelId);
}

// --------------------------------------------------------------------------
// Event routing
// --------------------------------------------------------------------------

async function handleEvent(payload, env) {
  const event = payload.event || {};

  // Work out whether this is a trigger BEFORE touching KV. Dedupe writes are
  // capped at 1,000/day on the free tier, so only events that are about to
  // become tickets are allowed to spend one.
  let trigger = null;
  if (event.type === 'message') trigger = await messageTrigger(event, env);
  else if (event.type === 'reaction_added') trigger = await reactionTrigger(event, env);
  if (!trigger) return;

  // Slack retries a delivery up to three times; event_id is stable across them.
  if (await alreadyProcessed(env, payload.event_id)) return;

  console.log(`trigger: ${trigger.why} (${trigger.channel}/${trigger.ts})`);
  await createTicket(env, trigger);
}

/**
 * A plain channel message. Returns a trigger or null.
 */
async function messageTrigger(event, env) {
  // Ignore message subtypes (edits, deletes, joins, bot_message, etc.)
  // and anything posted by a bot, including ourselves, to avoid loops.
  if (event.subtype || event.bot_id) return null;
  if (!channelAllowed(event.channel, env)) return null;

  const text = (event.text || '').trim();
  if (!text) return null;

  const modes = triggers(env);
  const threadTs = event.thread_ts || event.ts;
  const isThreadParent = !event.thread_ts || event.thread_ts === event.ts;
  const base = (problem, why) => ({
    channel: event.channel,
    ts: event.ts,
    threadTs,
    userId: event.user,
    text,
    problem,
    why,
  });

  // `all` only ever fires on a thread parent: a reply is a continuation of a
  // conversation that already has (or deliberately has not) a ticket.
  if (modes.includes('all') && isThreadParent) {
    return base(text, 'new thread (mode: all)');
  }

  // The explicit modes below work on replies too — someone may only realise
  // halfway down a thread that it needs a ticket.
  if (modes.includes('mention')) {
    const botId = await getBotUserId(env);
    if (botId && text.includes(`<@${botId}>`)) {
      const problem = text.replace(new RegExp(`<@${botId}>`, 'g'), ' ').trim();
      return base(problem || text, 'bot mentioned (mode: mention)');
    }
  }

  if (modes.includes('keyword')) {
    const keyword = (env.SLACK_TRIGGER_KEYWORD || DEFAULT_KEYWORD).trim();
    if (keyword && text.toLowerCase().includes(keyword.toLowerCase())) {
      const problem = stripFirst(text, keyword);
      return base(problem || text, `keyword "${keyword}" (mode: keyword)`);
    }
  }

  return null;
}

/**
 * A reaction on a message. The reacting person is choosing which message
 * states the problem, which is worth more than any guess we could make.
 */
async function reactionTrigger(event, env) {
  if (!triggers(env).includes('emoji')) return null;
  if (event.item?.type !== 'message') return null;

  // Skin-tone variants arrive as "wave::skin-tone-3".
  const reaction = String(event.reaction || '').split('::')[0];
  const wanted = (env.SLACK_TRIGGER_EMOJI || DEFAULT_EMOJI)
    .split(',')
    .map((s) => s.trim().replace(/:/g, '').toLowerCase())
    .filter(Boolean);
  if (wanted.length && !wanted.includes(reaction.toLowerCase())) return null;

  const channel = event.item.channel;
  if (!channelAllowed(channel, env)) return null;

  const message = await fetchMessage(env.SLACK_BOT_TOKEN, channel, event.item.ts);
  if (!message) return null;
  if (message.bot_id) return null; // don't let a reaction ticket our own reply

  const text = (message.text || '').trim();
  if (!text) return null;

  return {
    channel,
    ts: event.item.ts,
    threadTs: message.thread_ts || event.item.ts,
    userId: message.user,
    requestedBy: event.user,
    text,
    problem: text,
    why: `:${reaction}: reaction (mode: emoji)`,
  };
}

// --------------------------------------------------------------------------
// Ticket creation
// --------------------------------------------------------------------------

async function createTicket(env, trigger) {
  // One message, one ticket — whichever mode fires. Without this, reacting to
  // a message that already auto-filed under `all` would file it twice, and
  // titles cannot be corrected after the fact.
  const messageKey = `slack:msg:${trigger.channel}:${trigger.ts}`;
  if (env.DEDUPE) {
    const existing = await env.DEDUPE.get(messageKey);
    if (existing) {
      console.log(`skip — message already filed as ${existing}`);
      await confirm(env, trigger, existing, true);
      return;
    }
  }

  const [user, channelName, permalink] = await Promise.all([
    getUser(env.SLACK_BOT_TOKEN, trigger.userId),
    getChannelName(env.SLACK_BOT_TOKEN, trigger.channel),
    getPermalink(env.SLACK_BOT_TOKEN, trigger.channel, trigger.ts),
  ]);

  const description = buildDescription({
    sender: user.name,
    email: user.email,
    channelName,
    permalink,
    ts: trigger.ts,
    text: trigger.text,
  });

  const issue = await createYouTrackIssue({
    baseUrl: env.YOUTRACK_BASE_URL,
    token: env.YOUTRACK_TOKEN,
    projectId: env.YOUTRACK_PROJECT_ID || 'CS',
    summary: buildTitle({
      source: 'SLACK',
      problem: trigger.problem || trigger.text,
      sender: user.name,
      // The channel IS the client here — #gigabrain, #mrp-amiz — so it is the
      // most useful "account" we have, and it costs nothing to read.
      account: channelName,
    }),
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
  if (env.DEDUPE) {
    await env.DEDUPE.put(messageKey, ticketId, { expirationTtl: TICKETED_TTL_SECONDS });
  }
  await confirm(env, trigger, ticketId, false);
}

/**
 * Tell Slack the ticket exists — or don't.
 *
 *   thread     public reply in the thread (default)
 *   ephemeral  visible only to whoever triggered it — good for testing in a
 *              live channel without the customer seeing anything
 *   off        say nothing
 */
async function confirm(env, trigger, ticketId, alreadyExisted) {
  const mode = String(env.SLACK_CONFIRM || 'thread').toLowerCase();
  if (mode === 'off') return;

  const token = env.SLACK_BOT_TOKEN;
  const text = alreadyExisted
    ? `ℹ️ This message is already filed as ${ticketId}.`
    : `✅ Ticket ${ticketId} created. Our team will respond shortly.`;

  if (mode === 'ephemeral') {
    const user = trigger.requestedBy || trigger.userId;
    if (!user) return;
    await slackPost(token, 'chat.postEphemeral', {
      channel: trigger.channel,
      user,
      thread_ts: trigger.threadTs,
      text,
    });
    return;
  }

  await slackPost(token, 'chat.postMessage', {
    channel: trigger.channel,
    thread_ts: trigger.threadTs,
    text,
  });
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

/** Cached for the life of the isolate — the bot's own id never changes. */
let botUserId;
async function getBotUserId(env) {
  if (botUserId !== undefined) return botUserId;
  if (env.SLACK_BOT_USER_ID) {
    botUserId = env.SLACK_BOT_USER_ID;
    return botUserId;
  }
  const data = await slackGet(env.SLACK_BOT_TOKEN, 'auth.test', {});
  botUserId = data?.user_id || '';
  return botUserId;
}

/**
 * Fetch one message by timestamp.
 *
 * conversations.replies takes the ts of either a thread parent or any reply
 * inside it, so it reaches threaded messages — conversations.history does not.
 * A standalone message comes back as a one-item thread.
 */
async function fetchMessage(token, channel, ts) {
  const data = await slackGet(token, 'conversations.replies', { channel, ts, limit: 1 });
  const messages = data?.messages || [];
  return messages.find((m) => m.ts === ts) || messages[0] || null;
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

async function slackGet(token, method, params) {
  const url = new URL(`https://slack.com/api/${method}`);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
  const data = await res.json();
  if (!data.ok) console.error(`${method} failed:`, data.error);
  return data;
}

async function slackPost(token, method, body) {
  const res = await fetch(`https://slack.com/api/${method}`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json; charset=utf-8',
    },
    body: JSON.stringify(body),
  });
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

/** Remove the first occurrence of `needle`, case-insensitively. */
function stripFirst(text, needle) {
  const at = text.toLowerCase().indexOf(needle.toLowerCase());
  if (at < 0) return text.trim();
  return `${text.slice(0, at)} ${text.slice(at + needle.length)}`.replace(/\s+/g, ' ').trim();
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
    .replace(/<@[A-Z0-9]+>/g, '') // unresolved @-mentions
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
