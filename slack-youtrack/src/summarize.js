/**
 * Writing a ticket title from a support conversation.
 *
 * The deterministic builder in format.js can only quote the customer, and a
 * quote of the opening message is usually "hi" or the middle of a sentence:
 *
 *   SLACK: or just the number of accounts that are actually connected? As you…
 *
 * Turning that into "Confirm real connected-account count for Gonorth billing"
 * is summarising, not string handling. This module does that one job.
 *
 * TWO RULES, both because YouTrack will not let a helpdesk ticket be retitled
 * after creation — whatever we write is permanent:
 *
 *   1. The model writes the TITLE ONLY. The description stays the customer's
 *      verbatim words. Nobody should ever act on a sentence a model invented.
 *   2. The account name must appear literally in the conversation. The model
 *      proposes, the caller verifies (see accountInThread). Without a list of
 *      real seller names to choose from, this is what stops an invented shop
 *      reaching a permanent field.
 *
 * Any failure at all — no key, timeout, HTTP error, unparseable answer, empty
 * title — returns null, and the caller keeps today's deterministic title. A
 * ticket is never lost to this feature.
 */

import Anthropic from '@anthropic-ai/sdk';

export const DEFAULT_MODEL = 'claude-opus-5';
export const DEFAULT_EFFORT = 'low';
export const DEFAULT_TIMEOUT_MS = 12000;
const MAX_TRANSCRIPT_CHARS = 12000;

const SYSTEM = `You write titles for customer-support tickets.

You are given a support conversation. Return:

- problem: what the customer needs, as a short statement a support agent can
  act on. Aim for 4-9 words. Start with the subject, not a verb phrase about
  the customer: "Dashboard will not connect to credentials", not "Customer is
  asking about connecting". No trailing punctuation. Never quote the customer
  verbatim — state the problem.
- account: the customer's shop, seller or company name IF one is named in the
  conversation. Copy it EXACTLY as it is written there, with no additions.
  If no such name appears, return an empty string. Do not guess, do not infer
  a name from an email domain, and do not use the channel name.

Write in plain English. Ignore greetings, thanks and pleasantries. Where the
customer describes several things, title the one they most need resolved.`;

const SCHEMA = {
  type: 'object',
  properties: {
    problem: { type: 'string' },
    account: { type: 'string' },
  },
  required: ['problem', 'account'],
  additionalProperties: false,
};

/**
 * Ask the model for a problem statement and an account name.
 *
 * Returns { problem, account, usage } or null. Never throws: the caller is on
 * the path that creates the ticket, and a title is not worth a ticket.
 */
export async function summarizeThread({
  apiKey,
  transcript,
  model = DEFAULT_MODEL,
  effort = DEFAULT_EFFORT,
  timeoutMs = DEFAULT_TIMEOUT_MS,
  log = console,
}) {
  if (!apiKey) return null;
  const text = String(transcript || '').trim();
  if (!text) return null;

  const client = new Anthropic({ apiKey, timeout: timeoutMs, maxRetries: 1 });

  try {
    const response = await client.messages.create({
      model,
      max_tokens: 200,
      system: SYSTEM,
      // A title is a small extraction, and this call sits between the customer
      // writing and the ticket appearing — so no thinking, and the cheapest
      // effort that does the job. Opus 5 accepts `disabled` at effort `high`
      // or lower; pairing it with xhigh/max is a 400.
      thinking: { type: 'disabled' },
      output_config: {
        effort,
        format: { type: 'json_schema', schema: SCHEMA },
      },
      messages: [{ role: 'user', content: `<conversation>\n${clipTranscript(text)}\n</conversation>` }],
    });

    const body = response.content.find((block) => block.type === 'text')?.text;
    if (!body) return null;

    const parsed = JSON.parse(body);
    const problem = String(parsed.problem || '').trim();
    if (!problem) return null;

    return {
      problem,
      account: String(parsed.account || '').trim(),
      usage: response.usage,
    };
  } catch (e) {
    log.error(`title model call failed: ${e.message}`);
    return null;
  }
}

/**
 * Render a thread as the plain transcript the model reads.
 *
 * Deliberately not the YouTrack description: no markdown, no links to follow,
 * no ticket metadata — just who said what, so the model has the same
 * information a human skimming the thread would have.
 */
export function transcriptOf(messages, renderText) {
  return messages
    .map((m) => `${m.speaker}: ${renderText ? renderText(m.text) : m.text}`.trim())
    .filter((line) => !/:$/.test(line))
    .join('\n\n');
}

/**
 * Accept an account name only if the customer actually wrote it.
 *
 * With no list of real seller names to match against, this is the guard that
 * keeps a hallucinated shop out of a field nobody can edit later. Matching
 * ignores case and surrounding punctuation, so "the Gonorth account" yields
 * "Gonorth", but a name that appears nowhere in the thread yields nothing.
 */
export function accountInThread(account, transcript) {
  const name = String(account || '').trim();
  if (name.length < 2) return '';
  if (/^(the|a|an|this|that|none|unknown|n\/a)$/i.test(name)) return '';
  const haystack = String(transcript || '').toLowerCase();
  return haystack.includes(name.toLowerCase()) ? name : '';
}

/**
 * Keep the tail, not the head.
 *
 * A long thread ends with the problem as the customer finally framed it, and
 * begins with the version they abandoned.
 */
function clipTranscript(text) {
  return text.length <= MAX_TRANSCRIPT_CHARS ? text : `…\n\n${text.slice(-MAX_TRANSCRIPT_CHARS)}`;
}
