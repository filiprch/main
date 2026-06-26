# Intercom → YouTrack connector

A Cloudflare Worker that creates a YouTrack Customer Support (CS) ticket when an
Intercom conversation is **escalated to a human** (Fin AI hands off to a
teammate).

```
Fin AI → assigns conversation to Lisa  ─▶  Worker verifies X-Hub-Signature
                                        ─▶  Creates CS ticket (Channel=Intercom,
                                            Type=Task, Replied=Not Replied,
                                            Customer Email = contact email)
```

## What triggers a ticket

- Topic **`conversation.admin.assigned`** where the assignee is a **human
  teammate**. Assignments to Fin / Operator bots are skipped via
  `INTERCOM_EXCLUDE_ADMIN_IDS`.
- Each conversation is deduped, so reassignment doesn't create a second ticket.

## Ticket shape

| YouTrack field | Value |
| --- | --- |
| Summary | Conversation subject, else first message line (≤140 chars) |
| Description | `Source: Intercom` header + sender + email + conversation link + full message |
| Channel | `Intercom` |
| Type | `Task` |
| Replied | `Not Replied` |
| Customer Email | Contact email (from payload, or looked up via API) |

## Setup

### 1. Create / configure the Intercom app
1. <https://developers.intercom.com/> → your app → **Webhooks**.
2. Add a webhook topic: **`conversation.admin.assigned`**. Set the endpoint to
   your Worker URL (below).
3. **Basic information** → copy the **Client Secret** (used to verify
   `X-Hub-Signature`).
4. Create an **Access Token** for contact lookups.
5. Find each bot/operator admin id (Fin) under **Settings → Teammates** /
   the `/admins` API, and list them in `INTERCOM_EXCLUDE_ADMIN_IDS` so Fin
   assignments don't create tickets.

### 2. Configure the Worker
```bash
cd intercom-youtrack
npm install

# Non-secret vars in wrangler.toml:
#   INTERCOM_APP_ID = "abcd1234"
#   INTERCOM_EXCLUDE_ADMIN_IDS = "1234567"   # Fin / Operator admin id(s)

# Secrets:
npx wrangler secret put INTERCOM_CLIENT_SECRET
npx wrangler secret put INTERCOM_TOKEN
npx wrangler secret put YOUTRACK_TOKEN

npx wrangler deploy
```

The deploy prints the Worker URL — paste it into the Intercom webhook endpoint.
Use the dashboard's **Send test notification** (topic `ping`) to confirm the
endpoint responds `200`.

### 3. YouTrack token / project id
Same as the Slack connector — see the troubleshooting note there if
`YOUTRACK_PROJECT_ID = "CS"` is rejected and you need the internal id.

## Local development
```bash
cp .dev.vars.example .dev.vars   # fill in secrets
npm run dev
```

## Notes
- The Worker acks Intercom immediately and does YouTrack work in
  `ctx.waitUntil`.
- If you later want **every new conversation** to create a ticket (not just
  escalations), add the `conversation.user.created` topic and handle it in
  `handleEvent` — the structure is already there.
