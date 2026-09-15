#!/usr/bin/env node
/**
 * Show the titles the model WOULD have written. Creates nothing.
 *
 * YouTrack will not let a helpdesk ticket be retitled after creation, so the
 * first real run is already permanent. This replays actual threads from a
 * Slack channel through the same builders the worker uses — imported, not
 * copied — and prints the old title beside the new one, with what the call
 * cost.
 *
 *   export SLACK_BOT_TOKEN=xoxb-…
 *   export ANTHROPIC_API_KEY=sk-ant-…
 *   node scripts/title-dryrun.mjs C0BDDQWPZB4            # last 10 threads
 *   node scripts/title-dryrun.mjs C0BDDQWPZB4 --limit 20
 *   node scripts/title-dryrun.mjs --file threads.json    # no Slack needed
 *
 * --file takes [{ "channel": "#name", "messages": [{ "speaker": …, "text": … }] }]
 * so a thread can be pasted in by hand when pulling from Slack is awkward.
 */

import { buildTitle, renderSlack } from '../src/format.js';
import { accountInThread, summarizeThread, transcriptOf } from '../src/summarize.js';

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : fallback;
};
const channelId = args.find((a) => !a.startsWith('--') && args[args.indexOf(a) - 1] !== '--limit');
const limit = Number(flag('--limit', 10));
const file = flag('--file');

const { ANTHROPIC_API_KEY, SLACK_BOT_TOKEN } = process.env;
if (!ANTHROPIC_API_KEY) die('set ANTHROPIC_API_KEY first');
if (!file && !SLACK_BOT_TOKEN) die('set SLACK_BOT_TOKEN, or pass --file threads.json');
if (!file && !channelId) die('pass a channel id, or --file threads.json');

const threads = file ? await fromFile(file) : await fromSlack(channelId, limit);
if (!threads.length) die('no threads found');

console.log(`\nReplaying ${threads.length} thread${threads.length === 1 ? '' : 's'}. Nothing is created.\n`);

let inTokens = 0;
let outTokens = 0;
let failures = 0;
let accountsFound = 0;
let accountsDropped = 0;

for (const [i, thread] of threads.entries()) {
  const transcript = transcriptOf(thread.messages);
  const sender = thread.messages[0]?.speaker || 'Unknown';

  const before = buildTitle({
    source: 'SLACK',
    problem: thread.messages[0]?.text || '',
    sender,
    account: thread.channel,
  });

  const result = await summarizeThread({ apiKey: ANTHROPIC_API_KEY, transcript });

  let after;
  let note = '';
  if (!result) {
    failures++;
    after = before;
    note = '  (model gave nothing — would fall back)';
  } else {
    inTokens += result.usage?.input_tokens || 0;
    outTokens += result.usage?.output_tokens || 0;
    const account = accountInThread(result.account, transcript);
    if (account) accountsFound++;
    else if (result.account) {
      accountsDropped++;
      note = `  (dropped account "${result.account}" — not in the thread)`;
    }
    after = buildTitle({ source: 'SLACK', problem: result.problem, sender, account: account || thread.channel });
  }

  console.log(`${String(i + 1).padStart(2)}. ${thread.messages.length} msg · ${thread.channel}`);
  console.log(`    now:  ${before}`);
  console.log(`    new:  ${after}${note}`);
  console.log();
}

console.log('─'.repeat(72));
console.log(`threads          ${threads.length}`);
console.log(`tokens           ${inTokens} in / ${outTokens} out`);
console.log(`account found    ${accountsFound}`);
console.log(`account dropped  ${accountsDropped}   (model named one that was not in the thread)`);
console.log(`fell back        ${failures}`);
console.log(`
Token counts are exact — multiply by the per-token price on your Anthropic
console billing page for the real cost. Nothing above was written anywhere.
`);

// --------------------------------------------------------------------------

async function fromSlack(channel, want) {
  const names = new Map();
  const history = await slack('conversations.history', { channel, limit: 100 });
  const parents = (history.messages || [])
    .filter((m) => !m.subtype && !m.bot_id && (m.text || '').trim())
    .slice(0, want);

  const channelInfo = await slack('conversations.info', { channel });
  const channelName = channelInfo.channel?.name ? `#${channelInfo.channel.name}` : channel;

  const threads = [];
  for (const parent of parents) {
    const replies = await slack('conversations.replies', { channel, ts: parent.thread_ts || parent.ts, limit: 200 });
    const messages = (replies.messages || []).filter((m) => !m.subtype || m.subtype === 'file_share');
    for (const m of messages) {
      if (m.user && !names.has(m.user)) {
        const info = await slack('users.info', { user: m.user });
        const p = info.user?.profile || {};
        names.set(m.user, p.display_name || p.real_name || info.user?.name || m.user);
      }
    }
    threads.push({
      channel: channelName,
      messages: messages.map((m) => ({
        speaker: names.get(m.user) || m.username || 'Unknown',
        text: renderSlack(m.text, names),
      })),
    });
  }
  return threads;
}

async function fromFile(path) {
  const { readFile } = await import('node:fs/promises');
  return JSON.parse(await readFile(path, 'utf8'));
}

async function slack(method, params) {
  const url = new URL(`https://slack.com/api/${method}`);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  const res = await fetch(url, { headers: { Authorization: `Bearer ${SLACK_BOT_TOKEN}` } });
  const data = await res.json();
  if (!data.ok) die(`${method} failed: ${data.error}`);
  return data;
}

function die(message) {
  console.error(`\n${message}\n`);
  process.exit(1);
}
