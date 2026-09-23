/**
 * Turning Slack's wire format into a readable YouTrack ticket.
 *
 * Kept apart from the worker so the dry-run script scores the very same
 * builders that production uses. A title preview that came from a copy of this
 * logic would prove nothing about the titles you actually get.
 */

export function fileName(file) {
  return file.name || file.title || 'attachment';
}

/**
 * Slack sends entries here that are not files we can fetch — external links
 * added via "Add a file from Google Drive", and posts still being processed.
 */
export function downloadable(file) {
  if (!file) return false;
  if (file.mode === 'external' || file.is_external) return false;
  return Boolean(file.url_private_download || file.url_private);
}

/**
 * Turn Slack's wire format into something readable in YouTrack.
 *
 * Agents without a Slack seat were reading raw <@U03ABC> ids and link markup.
 * The entity escapes are Slack's own and must be undone last, so a message
 * containing a literal "&lt;" does not turn into markup.
 */
export function renderSlack(text, names) {
  return String(text || '')
    .replace(/<@([A-Z0-9]+)(?:\|[^>]*)?>/g, (_, id) => `@${names?.get(id) || id}`)
    .replace(/<#[A-Z0-9]+\|([^>]+)>/g, '#$1')
    .replace(/<!(here|channel|everyone)(?:\|[^>]*)?>/g, '@$1')
    .replace(/<((?:https?:\/\/|mailto:)[^|>]+)\|([^>]+)>/g, '[$2]($1)')
    .replace(/<(https?:\/\/[^>]+)>/g, '$1')
    .replace(/<mailto:([^>]+)>/g, '$1')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
    .trim();
}

/**
 * A fixed section order, so every ticket scans the same way.
 *
 * The conversation is quoted verbatim and attributed — an agent acting on this
 * ticket is acting on what the customer actually wrote, not on a paraphrase.
 */
export function buildDescription({
  sender,
  email,
  channelName,
  permalink,
  why,
  messages,
  names,
  files,
  triggerTs,
}) {
  const who = email ? `${sender} <${email}>` : sender;
  const lines = [
    `**Customer:** ${who} · ${channelName}`,
    `**Raised:** ${formatUtc(messages[0]?.ts || triggerTs)} UTC · ${why}`,
  ];
  if (permalink) lines.push(`**Slack:** [open the thread](${permalink})`);

  lines.push('', `### Conversation (${messages.length} message${messages.length === 1 ? '' : 's'})`, '');
  for (const m of messages) {
    const speaker = names?.get(m.user) || m.username || 'Unknown';
    const body = renderSlack(m.text, names) || '_(no text)_';
    const marker = m.ts === triggerTs && messages.length > 1 ? ' ←' : '';
    lines.push(`**${formatUtc(m.ts)} · ${speaker}**${marker}`);
    for (const line of body.split('\n')) lines.push(`> ${line}`);
    for (const f of m.files || []) lines.push(`> 📎 ${fileName(f)}`);
    lines.push('');
  }

  if (files.length) {
    lines.push('### Attachments', '');
    for (const f of files) lines.push(`- ${fileName(f)}`);
    lines.push('');
  }

  return lines.join('\n');
}

/** Slack ts is "<unix-seconds>.<micros>". Render as YYYY-MM-DD HH:MM. */
export function formatUtc(ts) {
  const ms = ts ? Math.floor(Number(ts) * 1000) : Date.now();
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(
    d.getUTCHours()
  )}:${p(d.getUTCMinutes())}`;
}

/** Remove the first occurrence of `needle`, case-insensitively. */
export function stripFirst(text, needle) {
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
export function cleanForTitle(text) {
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

export function clip(text, max) {
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
export function buildTitle({ source, problem, sender, account }) {
  const parts = [clip(cleanForTitle(problem), 70) || 'No message'];
  if (sender) parts.push(clip(sender, 40));
  if (account) parts.push(clip(account, 30));
  return `${source}: ${parts.join(' - ')}`;
}

/**
 * One follow-up message, as a YouTrack comment.
 *
 * Same shape as a block in the description above, so a ticket reads as one
 * continuous conversation rather than a description in one voice and comments
 * in another.
 */
export function buildComment({ speaker, ts, text, files, names }) {
  const lines = [`**${speaker}** in Slack · ${formatUtc(ts)} UTC`, ''];
  for (const line of (renderSlack(text, names) || '_(no text)_').split('\n')) lines.push(`> ${line}`);
  for (const f of files || []) lines.push(`> 📎 ${fileName(f)}`);
  return lines.join('\n');
}

/**
 * Drop the agent signature YouTrack appends to public comments.
 *
 * It is stored in the comment text, separated by a rule: "test\n___\nKind
 * regards,\nFilip Seidel". In Slack that lands under a "*Filip Seidel:*"
 * prefix we already added, so the customer reads the sender's name twice and a
 * sign-off on a chat message. Only a trailing block is removed, and only after
 * a line that is nothing but underscores — YouTrack's own separator.
 */
export function stripSignature(text) {
  const body = String(text || '');
  const match = /\n\s*_{3,}\s*\n[\s\S]*$/.exec(body);
  return (match ? body.slice(0, match.index) : body).trim();
}

/**
 * Turn a YouTrack comment into something worth reading in Slack.
 *
 * Pasting an image into a comment puts BOTH an attachment on the comment and
 * `![](image2.png){width=70%}` in its text. The relay uploads the attachment,
 * so that markup arrives as literal characters next to the picture it refers
 * to. Markdown links have the same problem — Slack has its own syntax and
 * shows `[label](url)` verbatim — so they are converted rather than dropped,
 * since the address is the part the customer may need.
 *
 * A comment that was nothing but a pasted image comes back empty, which is
 * correct: the caller then sends the file alone.
 */
export function slackifyComment(text) {
  return String(text || '')
    .replace(/[ \t]*!\[[^\]]*\]\([^)]*\)(?:\{[^}]*\})?/g, '')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<$2|$1>')
    .replace(/[ \t]+$/gm, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}
