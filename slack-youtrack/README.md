# Slack → YouTrack connector

A Cloudflare Worker that turns the **first message of a thread** in a monitored
customer Slack channel into a YouTrack Customer Support (CS) ticket, then posts
a confirmation back in that thread.

```
Customer posts in #client-acme  ─▶  Worker verifies Slack signature
                                 ─▶  Creates CS ticket (Channel=Slack, Type=Task,
                                     Replied=Not Replied)
                                 ─▶  Replies in-thread: "✅ Ticket CS-123 created…"
```

## What triggers a ticket

- **First message in a thread** (a top-level message). Threaded replies, edits,
  deletes, joins, and bot messages are ignored.
- Only channels listed in `CUSTOMER_CHANNEL_IDS` (allowlist).
- Slack `event_id` is deduped so retries don't create duplicate tickets.

## Ticket shape

| YouTrack field | Value |
| --- | --- |
| Summary | First line of the message (≤140 chars) |
| Description | `Source: Slack` header + sender + channel + thread link + full message |
| Channel | `Slack` |
| Type | `Task` |
| Replied | `Not Replied` |
| Customer Email | *(empty — Slack has no email)* |

> The YouTrack `auto-tag-and-route` workflow still runs on creation. Because we
> set `Channel = Slack` and already include a `Source:` header, the workflow
> leaves them as-is and won't double-prepend a header.

## Setup

### 1. Create the Slack app
1. <https://api.slack.com/apps> → **Create New App** → From scratch.
2. **OAuth & Permissions** → Bot Token Scopes: `channels:history`,
   `channels:read`, `groups:history`, `groups:read`, `users:read`,
   `chat:write`. Install to workspace, copy the **Bot User OAuth Token**
   (`xoxb-…`).
3. **Basic Information** → copy the **Signing Secret**.
4. Invite the bot to each customer channel: `/invite @yourbot`.
5. **Event Subscriptions** → enable, set Request URL to your Worker URL (below),
   subscribe to bot events: `message.channels` and `message.groups` (for
   private channels). Slack will hit the URL with a `url_verification`
   challenge — the Worker answers it automatically.

### 2. Configure the Worker
```bash
cd slack-youtrack
npm install

# Non-secret vars live in wrangler.toml — set the channel allowlist there:
#   CUSTOMER_CHANNEL_IDS = "C0123ABC,C0456DEF"

# Secrets:
npx wrangler secret put SLACK_SIGNING_SECRET
npx wrangler secret put SLACK_BOT_TOKEN
npx wrangler secret put YOUTRACK_TOKEN

npx wrangler deploy
```

The deploy prints the Worker URL — paste it into the Slack **Request URL** field.

### 3. YouTrack token
YouTrack → profile avatar → **Profile** → **Account Security / Authentication**
→ **New token** → scope: YouTrack → copy. Use it for `YOUTRACK_TOKEN`.

> If issue creation returns `project not found`, your instance needs the
> internal project id instead of the shortName. Get it with:
> ```bash
> curl -H "Authorization: Bearer $YOUTRACK_TOKEN" \
>   "https://myrealprofit.youtrack.cloud/api/admin/projects?fields=id,shortName,name"
> ```
> and set `YOUTRACK_PROJECT_ID` (in wrangler.toml) to the returned `id` (e.g. `0-3`).

## Local development
```bash
cp .dev.vars.example .dev.vars   # fill in secrets
npm run dev
```

## Notes
- The Worker acks Slack within 3 seconds and does YouTrack/Slack work in
  `ctx.waitUntil`, so Slack won't retry on timeout.
- Optional: bind a KV namespace called `DEDUPE` (see `wrangler.toml`) for
  cross-isolate dedupe. Without it, dedupe is best-effort per isolate plus
  Slack's own `event_id`.
