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
 *   SLACK_MODE             (var)     always | emoji | night | night+emoji | off
 *   SLACK_TRIGGERS         (var)     custom mode: all | night | emoji | mention | keyword
 *   SLACK_NIGHT_HOURS      (var)     window for `night`, default "20:00-08:00"
 *   SLACK_HOURS_TZ         (var)     "+02:00" or an IANA zone (Europe/Warsaw)
 *   SLACK_TRIGGER_EMOJI    (var)     emoji names for `emoji` mode (no colons)
 *   SLACK_TRIGGER_KEYWORD  (var)     phrase for `keyword` mode
 *   SLACK_CONFIRM          (var)     thread | ephemeral | off
 *   ENABLED                (var)     "false" switches the whole worker off
 *   DEDUPE                 (KV, opt) namespace to dedupe events and messages
 */

import {
  addYouTrackComment,
  attachToYouTrackIssue,
  createYouTrackIssue,
  findYouTrackUserByEmail,
  listYouTrackComments,
  isPublicComment,
  setYouTrackEnumField,
} from './youtrack.js';
import {
  buildComment,
  buildDescription,
  buildTitle,
  downloadable,
  fileName,
  renderSlack,
  stripFirst,
  slackifyComment,
  stripSignature,
} from './format.js';
import {
  DEFAULT_EFFORT,
  DEFAULT_MODEL,
  DEFAULT_TIMEOUT_MS,
  accountInThread,
  summarizeThread,
  transcriptOf,
} from './summarize.js';

const DEFAULT_TRIGGERS = 'all';
const DEFAULT_EMOJI = 'ticket';
const DEFAULT_KEYWORD = '!ticket';
const DEFAULT_TZ = '+02:00';
const DEFAULT_NIGHT_HOURS = '20:00-08:00';
const TICKETED_TTL_SECONDS = 60 * 60 * 24 * 30;
const MAX_THREAD_MESSAGES = 200;

/**
 * channel+thread -> ticket id.
 *
 * ONE THREAD IS ONE TICKET. Keying this per message instead let a second
 * :ticket: reaction elsewhere in the same thread open a second ticket for the
 * same conversation — and replies afterwards could only ever land on one of
 * them, so the other silently went stale.
 */
const kThread = (channel, ts) => `slack:thread:${channel}:${ts}`;

/**
 * Written while a ticket is being created, so two reactions landing together
 * cannot both get past the check above. Short-lived: if the isolate dies
 * mid-create, the thread must not be wedged forever.
 */
const CLAIM = 'creating';
const CLAIM_TTL_SECONDS = 300;

/** ticket id -> the Slack thread it came from, so a reply knows where to go. */
const kTicket = (id) => `slack:ticket:${id}`;
/** comment id -> already relayed, so a workflow retry cannot double-post. */
const kRelayed = (id) => `slack:relayed:${id}`;
/**
 * email -> YouTrack user id, or "-" when that address has no account.
 *
 * Cached because most commenters repeat, and because the negative answer is
 * the common one — customers in a shared channel have no YouTrack account and
 * never will. A shorter TTL on the negative so somebody who joins next week is
 * picked up without anyone having to clear anything.
 */
const kYtUser = (email) => `yt:user:${email.toLowerCase()}`;
const YT_USER_TTL_SECONDS = 60 * 60 * 24 * 7;
const YT_NO_USER_TTL_SECONDS = 60 * 60 * 24;
/**
 * attachment id -> already uploaded.
 *
 * A second claim, narrower than the comment's. KV is eventually consistent, so
 * two firings arriving together can both read the comment as unclaimed; this
 * gives the upload its own check closer to the act, which is what is worth
 * protecting — a duplicated sentence is untidy, a duplicated photo looks
 * broken.
 */
const kFileSent = (id) => `slack:file:${id}`;

/** Path YouTrack's workflow posts a new comment to. */
const REPLY_PATH = '/youtrack/comment';
/**
 * How far back a relay pass will look.
 *
 * The pass is self-healing, which without a bound would mean the first firing
 * after a fix floods a customer with every public comment ever written on the
 * ticket. An hour is long enough to recover from an outage, short enough that
 * nothing surprising arrives.
 */
const RELAY_MAX_AGE_MS = 60 * 60 * 1000;

/**
 * Comments already reported as internal, so a chatty ticket does not reprint
 * the same verdict on every firing for an hour. Per-isolate and lossy on
 * purpose: this is log hygiene, not correctness, and must never cost a KV
 * write.
 */
const loggedInternal = new Set();

/**
 * Largest attachment worth moving through the worker.
 *
 * Every byte passes through memory on its way from YouTrack to Slack, and a
 * worker has far less of it than either service. A screenshot is the case that
 * matters; a video is the case that would take the whole relay down with it.
 */
const MAX_RELAY_FILE_BYTES = 20 * 1024 * 1024;

/**
 * Named presets, so switching behaviour is one line in wrangler.toml.
 *
 *   always       Every new thread, round the clock. The original behaviour.
 *   emoji        Only when someone reacts :ticket:, round the clock.
 *   night        Every new thread posted between 20:00 and 08:00 — the hours
 *                when nobody is watching Slack, so nothing can be handled
 *                live and everything needs a ticket waiting in the morning.
 *   night+emoji  Both of the above. A message posted overnight files itself;
 *                anything else files when someone reacts :ticket: to it.
 *   off          File nothing. The worker still logs every decision.
 *
 * `custom` (or any unrecognised value) falls through to SLACK_TRIGGERS.
 */
const MODES = {
  always: 'all',
  emoji: 'emoji',
  night: 'night',
  'night+emoji': 'night,emoji',
  off: '',
};

export default {
  async fetch(request, env, ctx) {
    if (request.method !== 'POST') {
      return new Response('Method Not Allowed', { status: 405 });
    }

    const rawBody = await request.text();

    // --- YouTrack's workflow, on its own path -----------------------------
    // Separated before the Slack checks below: this call carries a shared
    // secret, not a Slack signature, and running it through Slack's
    // verification would reject every one of them.
    if (new URL(request.url).pathname === REPLY_PATH) {
      if (!verifySharedSecret(request, env)) {
        return new Response('Invalid secret', { status: 401 });
      }
      if (!isEnabled(env)) return new Response('disabled', { status: 200 });
      ctx.waitUntil(
        relayReply(rawBody, env).catch((e) => console.error('relayReply error:', e))
      );
      return new Response('', { status: 200 });
    }

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
function settings(env) {
  const name = String(env.SLACK_MODE || 'custom').trim().toLowerCase();
  const preset = MODES[name];
  if (preset !== undefined) return { name, triggers: preset };
  if (name !== 'custom') {
    console.error(`unknown SLACK_MODE "${name}" — falling back to SLACK_TRIGGERS`);
  }
  return { name: 'custom', triggers: env.SLACK_TRIGGERS ?? DEFAULT_TRIGGERS };
}

function triggers(env) {
  return String(settings(env).triggers)
    .split(',')
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
}

function nightWindow(env) {
  return {
    hours: env.SLACK_NIGHT_HOURS || DEFAULT_NIGHT_HOURS,
    tz: env.SLACK_HOURS_TZ || DEFAULT_TZ,
  };
}

/**
 * Was `tsSeconds` inside the window?
 *
 * A malformed window or timezone opens the gate rather than closing it: a
 * missed ticket is worse than an extra one, and the error is in the log.
 */
function withinHours(tsSeconds, window, tz) {
  if (!window) return { ok: true };

  const m = /^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$/.exec(String(window).trim());
  if (!m) {
    console.error(`bad night window "${window}" — treating every hour as in-window`);
    return { ok: true };
  }
  const start = Number(m[1]) * 60 + Number(m[2]);
  const end = Number(m[3]) * 60 + Number(m[4]);

  const at = localMinutes(new Date(Number(tsSeconds) * 1000), tz);
  if (at === null) return { ok: true };

  // start > end means the window crosses midnight, which 20:00-08:00 does.
  const ok = start === end ? true : start < end ? at >= start && at < end : at >= start || at < end;
  return { ok, at: hhmm(at), window, tz };
}

/** Minutes past local midnight, for a fixed offset or an IANA zone name. */
function localMinutes(date, tz) {
  const offset = /^([+-])(\d{2}):?(\d{2})$/.exec(String(tz).trim());
  if (offset) {
    const sign = offset[1] === '-' ? -1 : 1;
    const minutes = Number(offset[2]) * 60 + Number(offset[3]);
    const shifted = new Date(date.getTime() + sign * minutes * 60000);
    return shifted.getUTCHours() * 60 + shifted.getUTCMinutes();
  }
  try {
    const parts = new Intl.DateTimeFormat('en-GB', {
      timeZone: tz,
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    }).formatToParts(date);
    const hour = Number(parts.find((x) => x.type === 'hour').value);
    const minute = Number(parts.find((x) => x.type === 'minute').value);
    return hour * 60 + minute;
  } catch (e) {
    console.error(`bad SLACK_HOURS_TZ "${tz}": ${e.message} — treating every hour as in-window`);
    return null;
  }
}

function hhmm(minutes) {
  const p = (n) => String(n).padStart(2, '0');
  return `${p(Math.floor(minutes / 60))}:${p(minutes % 60)}`;
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

  // A reply in a thread that already has a ticket updates that ticket. Checked
  // first, so "!ticket" written halfway down a filed thread adds a comment
  // rather than opening a second ticket for the same conversation.
  if (await commentOnExistingTicket(env, event, payload.event_id)) return;

  // Work out whether this is a trigger BEFORE touching KV. Dedupe writes are
  // capped at 1,000/day on the free tier, so only events that are about to
  // become tickets are allowed to spend one.
  let trigger = null;
  if (event.type === 'message') trigger = await messageTrigger(event, env);
  else if (event.type === 'reaction_added') trigger = await reactionTrigger(event, env);
  if (!trigger) return;

  // Slack retries a delivery up to three times; event_id is stable across them.
  if (await alreadyProcessed(env, payload.event_id)) return;

  console.log(
    `trigger: ${trigger.why} (${trigger.channel}/${trigger.ts}) mode=${settings(env).name}`
  );

  try {
    await createTicket(env, trigger);
  } catch (e) {
    // Until now this threw into the void: the event was acknowledged, the log
    // carried a stack nobody reads, and the customer's request simply did not
    // become a ticket. For a helpdesk that is the worst failure there is,
    // precisely because it is invisible.
    console.error(`create failed for ${trigger.channel}/${trigger.ts}: ${e.stack || e.message}`);
    await reportFailure(env, trigger, e);
  }
}

/**
 * A reply to an already-filed thread, added to its ticket as a comment.
 *
 * Without this a ticket is a snapshot of the thread at the moment it was
 * filed: "actually it is also affecting Gonorth", sent two minutes later,
 * never reaches the agent working it.
 *
 * Returns true when it has handled the event. Messages from bots are excluded,
 * which also keeps an agent reply relayed into Slack from coming straight back
 * as a comment on the ticket it came from.
 */
async function commentOnExistingTicket(env, event, eventId) {
  if (event.type !== 'message' || event.bot_id) return false;
  if (event.subtype && event.subtype !== 'file_share') return false;
  if (!event.thread_ts || event.thread_ts === event.ts) return false; // not a reply
  if (!channelAllowed(event.channel, env)) return false;
  if (!env.DEDUPE) return false;

  const ticketId = await env.DEDUPE.get(kThread(event.channel, event.thread_ts));
  if (!ticketId) return false;

  // Claimed before the work, so a Slack redelivery cannot double-comment.
  if (await alreadyProcessed(env, eventId)) return true;

  const token = env.SLACK_BOT_TOKEN;
  const names = await resolveNames(token, [event]);
  const files = (event.files || []).filter(downloadable);
  const author = await getUser(token, event.user);

  try {
    const created = await commentAsAuthor(env, {
      issueId: ticketId,
      email: author.email,
      text: buildComment({
        speaker: names.get(event.user) || event.username || 'Unknown',
        ts: event.ts,
        text: event.text,
        files,
        names,
      }),
    });

    // Claim it as already relayed. This comment IS the customer's own message,
    // arriving from Slack — and it is public, because nothing sets otherwise.
    // Without this claim the relay rule would fire on it, see a public comment
    // and post the customer's words back at them, once per message, forever.
    if (created?.id) {
      await env.DEDUPE.put(kRelayed(created.id), 'from-slack', {
        expirationTtl: TICKETED_TTL_SECONDS,
      });
    }
    console.log(`comment: ${event.channel}/${event.ts} -> ${ticketId}`);
  } catch (e) {
    console.error(`comment failed for ${ticketId}: ${e.message}`);
    return true;
  }

  await uploadFiles(env, ticketId, files);
  return true;
}

/**
 * Send an agent's answer from YouTrack back into the Slack thread.
 *
 * WHAT TRAVELS IS DECIDED BY THE COMMENT'S VISIBILITY, nothing else. A public
 * comment reaches the customer; an internal one never leaves YouTrack. That is
 * the same control an agent already uses on a Gmail ticket — the "Internal,
 * visible to Customer Support - Helpdesk Team" toggle — so there is one habit
 * across every channel rather than a per-channel convention to remember. A
 * convention that has to be remembered is one that will eventually be
 * forgotten, and the thing forgetting it leaks is an internal note about a
 * customer, to that customer.
 *
 * The workflow reports only THAT something changed, by issue id. This pass
 * then reads the issue's recent comments from YouTrack and sends every public
 * one it has not sent before. That makes it idempotent and self-healing — a
 * firing that arrives late, twice, or batched still lands each comment exactly
 * once — and it avoids depending on a workflow comment id matching a REST
 * comment id, which is documented nowhere. Anything not positively confirmed
 * public stays put; see isPublicComment.
 */
async function relayReply(rawBody, env) {
  let payload;
  try {
    payload = JSON.parse(rawBody);
  } catch {
    console.error('reply: body was not JSON');
    return;
  }

  const issueId = payload.issueId;
  if (!issueId) {
    console.error('reply: needs issueId');
    return;
  }
  if (!env.DEDUPE) {
    console.error('reply: no KV bound, cannot find the thread');
    return;
  }

  const target = await env.DEDUPE.get(kTicket(issueId));
  if (!target) {
    console.error(`reply: no Slack thread recorded for ${issueId}`);
    return;
  }
  const { channel, threadTs } = JSON.parse(target);

  let comments;
  try {
    comments = await listYouTrackComments({
      baseUrl: env.YOUTRACK_BASE_URL,
      token: env.YOUTRACK_TOKEN,
      issueId,
    });
  } catch (e) {
    // Could not establish what is public, so nothing travels.
    console.error(`reply: could not read comments on ${issueId}: ${e.message}`);
    return;
  }

  const cutoff = Date.now() - RELAY_MAX_AGE_MS;
  const recent = comments
    .filter((c) => c?.id && Number(c.created || 0) >= cutoff)
    .sort((a, b) => Number(a.created) - Number(b.created));

  for (const comment of recent) {
    // A relayed message cannot be unsent, so the claim comes before the send.
    if (await env.DEDUPE.get(kRelayed(comment.id))) continue;

    if (!isPublicComment(comment)) {
      // The shape is logged, not just the verdict. What YouTrack actually puts
      // on a public helpdesk comment is the one thing this decision rests on.
      if (!loggedInternal.has(comment.id)) {
        loggedInternal.add(comment.id);
        if (loggedInternal.size > 500) loggedInternal.clear();
        console.log(
          `reply: ${issueId} comment ${comment.id} treated as internal — ` +
            `visibility=${JSON.stringify(comment.visibility ?? null)}`
        );
      }
      continue;
    }

    const message = slackifyComment(stripSignature(comment.text));
    const attachments = comment.attachments || [];
    if (!message && !attachments.length) continue;

    await env.DEDUPE.put(kRelayed(comment.id), '1', { expirationTtl: TICKETED_TTL_SECONDS });

    const who = String(comment.author?.fullName || comment.author?.login || '').trim();

    if (message) {
      const posted = await slackPost(env.SLACK_BOT_TOKEN, 'chat.postMessage', {
        channel,
        thread_ts: threadTs,
        text: who ? `*${who}:* ${message}` : message,
      });
      if (!posted.ok) {
        console.error(`reply: could not post ${issueId}/${comment.id} to Slack: ${posted.error}`);
        continue;
      }
    }

    // After the text, so a file that will not move cannot cost the answer.
    let sentFiles = 0;
    for (const file of attachments) {
      const ok = await relayAttachment(env, {
        channel,
        threadTs,
        file,
        issueId,
        who: message ? '' : who,
      });
      if (ok) sentFiles++;
    }

    // Counts what arrived, not what was attempted — a line claiming a file was
    // sent when it failed is worse than no line at all.
    const files =
      attachments.length === 0
        ? ''
        : ` (${sentFiles}/${attachments.length} file${attachments.length === 1 ? '' : 's'})`;
    console.log(`reply: ${issueId} comment ${comment.id} -> ${channel}/${threadTs}${files}`);

    // The board should show who is still waiting. Failing to flip the field is
    // not worth undoing a message the customer has already seen.
    try {
      await setYouTrackEnumField({
        baseUrl: env.YOUTRACK_BASE_URL,
        token: env.YOUTRACK_TOKEN,
        issueId,
        field: 'Replied',
        value: 'Replied',
      });
    } catch (e) {
      console.error(`reply: sent, but could not mark ${issueId} replied: ${e.message}`);
    }
  }
}

/**
 * Move one YouTrack attachment into the Slack thread.
 *
 * files.upload was retired, so this is Slack's three-step external flow: ask
 * for an upload URL, PUT the bytes at it, then tell Slack where to share the
 * result. Needs the files:write bot scope.
 *
 * Failures are logged and swallowed: the agent's words have already reached
 * the customer by this point, and a screenshot that would not move is not a
 * reason to abandon the rest of the thread. Returns whether it arrived.
 */
async function relayAttachment(env, { channel, threadTs, file, issueId, who }) {
  const name = file.name || 'attachment';
  const claim = file.id ? kFileSent(file.id) : null;
  try {
    if (claim) {
      if (await env.DEDUPE.get(claim)) {
        console.log(`reply: ${name} was already sent for ${issueId}`);
        return false;
      }
      // Claimed before the bytes move, not after: the upload is the slow part
      // and therefore the widest window for a second pass to slip through.
      await env.DEDUPE.put(claim, '1', { expirationTtl: TICKETED_TTL_SECONDS });
    }

    const src = await fetch(absoluteUrl(env.YOUTRACK_BASE_URL, file.url), {
      headers: { Authorization: `Bearer ${env.YOUTRACK_TOKEN}` },
    });
    if (!src.ok) throw new Error(`download failed (HTTP ${src.status})`);

    const blob = await src.blob();
    if (!blob.size) throw new Error('downloaded as 0 bytes');
    if (blob.size > MAX_RELAY_FILE_BYTES) {
      throw new Error(`${Math.round(blob.size / 1048576)}MB exceeds the relay limit`);
    }

    const ticket = await slackForm(env.SLACK_BOT_TOKEN, 'files.getUploadURLExternal', {
      filename: name,
      length: String(blob.size),
    });
    if (!ticket.ok) throw new Error(`getUploadURLExternal: ${ticket.error}`);

    const form = new FormData();
    form.append('file', blob, name);
    const put = await fetch(ticket.upload_url, { method: 'POST', body: form });
    if (!put.ok) throw new Error(`upload failed (HTTP ${put.status})`);

    const done = await slackPost(env.SLACK_BOT_TOKEN, 'files.completeUploadExternal', {
      files: [{ id: ticket.file_id, title: name }],
      channel_id: channel,
      // Must be the thread PARENT, which is what we stored — Slack rejects a
      // reply's ts here.
      thread_ts: threadTs,
      ...(who ? { initial_comment: `*${who}:*` } : {}),
    });
    if (!done.ok) throw new Error(`completeUploadExternal: ${done.error}`);

    console.log(`reply: ${issueId} attached ${name} to ${channel}/${threadTs}`);
    return true;
  } catch (e) {
    console.error(`reply: could not relay ${name} from ${issueId}: ${e.message}`);
    return false;
  }
}

/** YouTrack gives attachment urls relative in some responses, absolute in others. */
function absoluteUrl(baseUrl, url) {
  const href = String(url || '');
  if (/^https?:\/\//i.test(href)) return href;
  return `${String(baseUrl).replace(/\/$/, '')}${href.startsWith('/') ? '' : '/'}${href}`;
}

/** Slack methods that want form encoding rather than JSON. */
async function slackForm(token, method, params) {
  const res = await fetch(`https://slack.com/api/${method}`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    body: new URLSearchParams(params),
  });
  const data = await res.json();
  if (!data.ok) console.error(`${method} failed:`, data.error);
  return data;
}

/**
 * Is this really our YouTrack?
 *
 * The endpoint takes a plain shared secret rather than a signature: YouTrack's
 * workflow HTTP client cannot compute an HMAC over the body. Compared in
 * constant time all the same, so the comparison itself leaks nothing.
 */
function verifySharedSecret(request, env) {
  const expected = env.YOUTRACK_WEBHOOK_SECRET;
  const given = request.headers.get('x-helpdesk-secret') || '';
  if (!expected) {
    console.error('reply: YOUTRACK_WEBHOOK_SECRET is not set — refusing every call');
    return false;
  }
  return timingSafeEqual(expected, given);
}

/**
 * Say out loud that a ticket could not be filed.
 *
 * Goes to SLACK_ALERT_CHANNEL when one is set, so the warning reaches the
 * team rather than the customer; otherwise into the thread, on the grounds
 * that somebody seeing it beats nobody seeing it. Deliberately ignores
 * SLACK_CONFIRM: that setting governs routine confirmations, and a request
 * that silently failed to become a ticket is not routine.
 */
async function reportFailure(env, trigger, error) {
  const reason = String(error?.message || error).slice(0, 300);
  const alertChannel = (env.SLACK_ALERT_CHANNEL || '').trim();
  const link = await getPermalink(env.SLACK_BOT_TOKEN, trigger.channel, trigger.ts).catch(() => '');

  const body = alertChannel
    ? `⚠️ Could not file a ticket from <#${trigger.channel}>${link ? ` (<${link}|the message>)` : ''} — please raise it manually.\n\n\`${reason}\``
    : `⚠️ Could not file this in YouTrack — please raise it manually.\n\n\`${reason}\``;

  await slackPost(env.SLACK_BOT_TOKEN, 'chat.postMessage', {
    channel: alertChannel || trigger.channel,
    ...(alertChannel ? {} : { thread_ts: trigger.threadTs }),
    text: body,
  }).catch((e) => console.error(`could not report the failure to Slack: ${e.message}`));
}

/**
 * A plain channel message. Returns a trigger or null.
 */
async function messageTrigger(event, env) {
  // Ignore message subtypes (edits, deletes, joins, bot_message, etc.) and
  // anything posted by a bot, including ourselves, to avoid loops. file_share
  // is the exception: a screenshot posted with a caption carries that subtype,
  // and refusing it meant night mode quietly ignored exactly the messages
  // most worth filing.
  if (event.bot_id) return null;
  if (event.subtype && event.subtype !== 'file_share') return null;
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

  // Out of hours nobody is watching Slack, so a new thread cannot be handled
  // live and files itself. Like `all`, it only fires on a thread parent — a
  // reply continues a conversation that has already been dealt with one way
  // or the other. If it does not match we fall through: emoji, mention and
  // keyword can still pick the message up.
  if (modes.includes('night') && isThreadParent) {
    const { hours, tz } = nightWindow(env);
    const when = withinHours(event.ts, hours, tz);
    if (when.ok) {
      return base(text, `out of hours — posted ${when.at} ${when.tz} (mode: night)`);
    }
    console.log(
      `night: ${event.channel}/${event.ts} posted ${when.at} ${when.tz}, ` +
        `outside ${when.window} — not filing`
    );
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
  const threadKey = kThread(trigger.channel, trigger.threadTs);

  if (env.DEDUPE) {
    const existing = await env.DEDUPE.get(threadKey);
    if (existing === CLAIM) {
      console.log(`skip ${trigger.channel}/${trigger.ts} — this thread is mid-creation`);
      return;
    }
    if (existing) {
      console.log(`skip ${trigger.channel}/${trigger.ts} — thread already filed as ${existing}`);
      await confirm(env, trigger, existing, true);
      return;
    }
    await env.DEDUPE.put(threadKey, CLAIM, { expirationTtl: CLAIM_TTL_SECONDS });
  }

  try {
    const ticketId = await fileTicket(env, trigger);
    if (env.DEDUPE) {
      await env.DEDUPE.put(threadKey, ticketId, { expirationTtl: TICKETED_TTL_SECONDS });
      // The reverse lookup, for an agent's reply travelling the other way.
      await env.DEDUPE.put(
        kTicket(ticketId),
        JSON.stringify({ channel: trigger.channel, threadTs: trigger.threadTs }),
        { expirationTtl: TICKETED_TTL_SECONDS }
      );
    }
    return ticketId;
  } catch (e) {
    // Release the claim, or one failed attempt would block this thread from
    // ever being filed until the TTL expired.
    await env.DEDUPE?.delete(threadKey);
    throw e;
  }
}

async function fileTicket(env, trigger) {
  const token = env.SLACK_BOT_TOKEN;

  // The whole thread, not just the message that tripped the trigger. Reacting
  // to the fifth message used to throw away the four above it — which is where
  // the customer actually explained the problem.
  const thread = await fetchThread(token, trigger.channel, trigger.threadTs);
  const messages = thread.length ? thread : [{ ts: trigger.ts, user: trigger.userId, text: trigger.text }];

  const names = await resolveNames(token, messages);
  const [reporter, channelName, permalink] = await Promise.all([
    getUser(token, trigger.userId),
    getChannelName(token, trigger.channel),
    getPermalink(token, trigger.channel, trigger.ts),
  ]);

  const files = messages.flatMap((m) => m.files || []).filter(downloadable);

  const issue = await createYouTrackIssue({
    baseUrl: env.YOUTRACK_BASE_URL,
    token: env.YOUTRACK_TOKEN,
    projectId: env.YOUTRACK_PROJECT_ID || 'CS',
    summary: await buildSummary(env, {
      trigger,
      messages,
      names,
      sender: reporter.name,
      channelName,
    }),
    description: buildDescription({
      sender: reporter.name,
      email: reporter.email,
      channelName,
      permalink,
      why: trigger.why,
      messages,
      names,
      files,
      triggerTs: trigger.ts,
    }),
    channel: 'Slack',
    type: 'Task',
    replied: 'Not Replied',
    // Requires the users:read.email bot scope. Left empty for guests and
    // Slack Connect users who expose no address; the auto-tag-and-route
    // workflow then falls back to the reporter.
    customerEmail: reporter.email || undefined,
  });

  const ticketId = issue.idReadable || issue.id;

  // After the ticket exists, so a failing upload cannot cost us the ticket.
  await uploadFiles(env, ticketId, files);
  await confirm(env, trigger, ticketId, false);
  return ticketId;
}

/**
 * The ticket title: written by the model when that is switched on and working,
 * quoted from the customer when it is not.
 *
 * The fallback is not a degraded mode to be embarrassed about — it is what
 * every ticket got until now. What it must never do is fail to produce a
 * title, because YouTrack will not let a helpdesk ticket be retitled after
 * creation, so a missing title is permanent in a way a mediocre one is not.
 */
async function buildSummary(env, { trigger, messages, names, sender, channelName }) {
  const fallback = () =>
    buildTitle({
      source: 'SLACK',
      problem: renderSlack(trigger.problem || trigger.text, names),
      sender,
      // The channel IS the client here — #gigabrain, #mrp-amiz — so it is the
      // most useful "account" we have, and it costs nothing to read.
      account: channelName,
    });

  if (String(env.TITLE_AI ?? 'false').toLowerCase() !== 'true') return fallback();

  const transcript = transcriptOf(
    messages.map((m) => ({ speaker: names?.get(m.user) || m.username || 'Unknown', text: m.text })),
    (text) => renderSlack(text, names)
  );

  const started = Date.now();
  const result = await summarizeThread({
    apiKey: env.ANTHROPIC_API_KEY,
    transcript,
    model: env.TITLE_AI_MODEL || DEFAULT_MODEL,
    effort: env.TITLE_AI_EFFORT || DEFAULT_EFFORT,
    timeoutMs: Number(env.TITLE_AI_TIMEOUT_MS) || DEFAULT_TIMEOUT_MS,
  });

  if (!result) {
    console.log('title: model gave nothing usable — using the quoted fallback');
    return fallback();
  }

  // Only a name the customer actually typed may reach a field nobody can edit.
  const account = accountInThread(result.account, transcript);
  if (result.account && !account) {
    console.log(`title: dropped account "${result.account}" — not present in the thread`);
  }

  const usage = result.usage || {};
  console.log(
    `title: ${Date.now() - started}ms, ${usage.input_tokens ?? '?'} in / ` +
      `${usage.output_tokens ?? '?'} out — "${result.problem}"`
  );

  return buildTitle({
    source: 'SLACK',
    problem: result.problem,
    sender,
    account: account || channelName,
  });
}

/**
 * Add a comment attributed to the person who actually wrote it.
 *
 * A message carried out of Slack is somebody's words, and a ticket crediting
 * them to the integration account is wrong about who said what — which matters
 * when the history is read back months later to work out what was agreed.
 *
 * Only people with a YouTrack account can be credited. Customers in a shared
 * channel have none, and inventing an author for them would be worse than the
 * honest fallback, so they keep the integration as author with their name in
 * the comment body.
 *
 * Falls back on ANY failure. YouTrack documents the author field but also says
 * it is unsupported for reporter-type accounts, so a refusal is expected
 * rather than exceptional — and a comment under the wrong name still beats a
 * comment lost.
 */
async function commentAsAuthor(env, { issueId, email, text }) {
  const post = (authorId) =>
    addYouTrackComment({
      baseUrl: env.YOUTRACK_BASE_URL,
      token: env.YOUTRACK_TOKEN,
      issueId,
      text,
      authorId,
    });

  const authorId = await youTrackUserId(env, email);
  if (!authorId) return post();

  try {
    return await post(authorId);
  } catch (e) {
    console.log(
      `comment: could not attribute to ${email} (${e.message}) — posting as the integration`
    );
    return post();
  }
}

/** The YouTrack user id for an email, or '' — cached both ways. */
async function youTrackUserId(env, email) {
  const address = String(email || '').trim().toLowerCase();
  if (!address) return '';

  const key = kYtUser(address);
  if (env.DEDUPE) {
    const cached = await env.DEDUPE.get(key);
    if (cached) return cached === '-' ? '' : cached;
  }

  let id = '';
  try {
    const user = await findYouTrackUserByEmail({
      baseUrl: env.YOUTRACK_BASE_URL,
      token: env.YOUTRACK_TOKEN,
      email: address,
    });
    id = user?.id || '';
  } catch (e) {
    // Not cached: a lookup that failed for infrastructure reasons leaves the
    // answer unknown, not negative.
    console.error(`comment: user lookup failed for ${address}: ${e.message}`);
    return '';
  }

  if (env.DEDUPE) {
    await env.DEDUPE.put(key, id || '-', {
      expirationTtl: id ? YT_USER_TTL_SECONDS : YT_NO_USER_TTL_SECONDS,
    });
  }
  return id;
}

/**
 * Copy Slack files into YouTrack as real attachments.
 *
 * url_private needs the bot token, and the link is useless to an agent without
 * a Slack seat — so the bytes are re-uploaded rather than linked. Each file is
 * attempted independently: one oversized video should not cost us the
 * screenshot that explains the problem.
 */
async function uploadFiles(env, issueId, files) {
  for (const file of files) {
    const name = fileName(file);
    // url_private_download is the byte-serving URL; url_private can answer
    // with a preview page for some file types. Prefer it, fall back.
    const url = file.url_private_download || file.url_private;
    try {
      await attachToYouTrackIssue({
        baseUrl: env.YOUTRACK_BASE_URL,
        token: env.YOUTRACK_TOKEN,
        issueId,
        name,
        url,
        authHeader: `Bearer ${env.SLACK_BOT_TOKEN}`,
      });
      console.log(`attached ${name} to ${issueId}`);
    } catch (e) {
      console.error(`attach failed for ${name} (${url}): ${e.message}`);
      if (/HTTP 403/.test(e.message)) await logTokenScopes(env.SLACK_BOT_TOKEN);
    }
  }
}

/**
 * Print the scopes the deployed token actually carries.
 *
 * A 403 on a file download means the token lacks files:read — and a token
 * issued before a scope was added never gains it, however many times the app
 * is reinstalled. This answers "is the secret in Cloudflare the current
 * token?" directly, instead of by inference.
 */
async function logTokenScopes(token) {
  try {
    const res = await fetch('https://slack.com/api/auth.test', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
    });
    const scopes = res.headers.get('x-oauth-scopes') || '(none reported)';
    console.log(`token scopes: ${scopes}`);
    console.log(
      /files:read/.test(scopes)
        ? 'files:read IS present — the 403 is not a missing scope'
        : 'files:read is MISSING — re-run: npx wrangler secret put SLACK_BOT_TOKEN'
    );
  } catch (e) {
    console.error(`could not read token scopes: ${e.message}`);
  }
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
    ? `ℹ️ This thread is already filed as ${ticketId} — reply here and it lands on that ticket.`
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
async function fetchThread(token, channel, threadTs) {
  const data = await slackGet(token, 'conversations.replies', {
    channel,
    ts: threadTs,
    limit: MAX_THREAD_MESSAGES,
  });
  return (data?.messages || []).filter((m) => !m.subtype || m.subtype === 'file_share');
}

/**
 * Display names for everyone in the thread, plus anyone @-mentioned in it.
 *
 * Resolved once per ticket and passed down, so a ten-message thread costs one
 * users.info call per distinct person rather than one per mention.
 */
async function resolveNames(token, messages) {
  const ids = new Set();
  for (const m of messages) {
    if (m.user) ids.add(m.user);
    for (const [, id] of String(m.text || '').matchAll(/<@([A-Z0-9]+)(?:\|[^>]*)?>/g)) ids.add(id);
  }
  const names = new Map();
  await Promise.all(
    [...ids].map(async (id) => {
      const user = await getUser(token, id);
      names.set(id, user.name);
    })
  );
  return names;
}

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
