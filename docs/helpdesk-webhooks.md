# Helpdesk Webhooks — Slack & Intercom → YouTrack

**Status:** Slack channel LIVE and verified (CS-158, 2026-09-03). Intercom untested.
**Owner:** Filip Seidel
**Audience:** developers / whoever hosts and maintains these integrations

This document describes two small webhook services that feed the YouTrack
Customer Support (CS) helpdesk from Slack and Intercom. It covers what they do,
how they're built, the hosting decision, the security model, and step-by-step
deployment — everything a developer needs to take ownership.

---

## 1. Why these exist

MyRealProfit runs a unified helpdesk on **YouTrack Cloud** (project **CS**).
Email already flows in via YouTrack's built-in Gmail/IMAP channel. The two
remaining customer channels — **Slack** (agency clients) and **Intercom**
(live chat with Fin AI) — have no native "create a YouTrack ticket" path, so
we bridge them ourselves.

Each bridge is a tiny HTTP service that:

1. receives a webhook from Slack / Intercom,
2. verifies it's authentic,
3. creates a YouTrack ticket via the REST API,
4. (Slack only) posts a confirmation back to the channel.

That's the entire scope. No database, no UI, ~200 lines of code each.

---

## 2. Architecture at a glance

```
                          ┌─────────────────────────┐
  Customer in Slack  ───▶ │  slack-youtrack (Worker) │ ──┐
  (agency channel)        └─────────────────────────┘   │
                                                          │   POST /api/issues
                          ┌─────────────────────────┐    │   (Bearer token)
  Fin AI escalates   ───▶ │ intercom-youtrack(Worker)│ ──┼──────────────▶  YouTrack Cloud
  to a human              └─────────────────────────┘    │                 project CS
                                                          │
  slack-youtrack also posts "✅ Ticket CS-XXX created" ◀─┘  ──▶ auto-tag workflow
  back into the Slack thread                                     fires on create
```

Both services are **stateless HTTP endpoints**. They are currently written as
**Cloudflare Workers** but the core logic is portable (see §7).

---

## 3. What triggers a ticket

These behaviours were decided with the product owner and are configurable.

| Channel | Trigger | Notes |
|---------|---------|-------|
| **Slack** | The **first message of a thread** in a monitored channel | Threaded replies, edits, joins, and bot messages are ignored. Only channels on an allowlist count. |
| **Intercom** | **Human escalation** — conversation assigned to a human teammate (`conversation.admin.assigned`) | Assignments to Fin / the Operator bot are excluded, so Fin resolving a chat alone does **not** create a ticket. |

Both are deduplicated so webhook retries never create duplicate tickets
(Slack by `event_id`, Intercom by conversation id).

---

## 4. How a ticket is built (field mapping)

Every ticket is created in project **CS** with these fields. The existing
YouTrack `auto-tag-and-route` workflow still runs on creation; because we set
`Channel` explicitly and include a `Source:` header, the workflow leaves those
alone instead of defaulting them to Gmail.

| YouTrack field | Slack value | Intercom value |
|----------------|-------------|----------------|
| Summary | First line of the message (≤140 chars) | Conversation subject, else first message line |
| Description | `Source: Slack` header + sender + channel + **thread link** + full message | `Source: Intercom` header + sender + email + **conversation link** + full message |
| Channel | `Slack` | `Intercom` |
| Type | `Task` | `Task` |
| Replied | `Not Replied` | `Not Replied` |
| Customer Email | *(empty — Slack has no email)* | Contact email (from payload or API lookup) |

**Description header example (Slack):**
```
Source: Slack
Sender: Jane Doe (#client-acme)
Received: 2026-07-03 14:22 UTC
Thread: https://myrealprofit.slack.com/archives/C0123/p1720012920000100

Full message:

Hi, our dashboard hasn't refreshed since this morning…
```

Enum fields (`Channel`, `Type`, `Replied`) are set through YouTrack's REST
`customFields` array using the `SingleEnumIssueCustomField` projection — this
is the reliable equivalent of the workflow's `ctx.Field.Value` pattern and
avoids the enum cast errors noted in the helpdesk docs.

---

## 5. Data flow (step by step)

### Slack
1. Customer posts a message in an allowlisted customer channel.
2. Slack sends an `event_callback` POST to the Worker.
3. Worker verifies the `X-Slack-Signature` (HMAC-SHA256 of the raw body with
   the signing secret, timestamp within 5 min).
4. Worker **acks within 3 seconds** (Slack's hard limit) and does the rest in
   the background (`waitUntil`).
5. Worker filters: message event, no subtype, not a bot, top-level (thread
   parent), channel on the allowlist, not a duplicate `event_id`.
6. Worker looks up the sender's display name, channel name, and a message
   permalink via Slack Web API.
7. Worker creates the YouTrack ticket.
8. Worker posts `✅ Ticket CS-XXX created. Our team will respond shortly.`
   back **in the thread**.

### Intercom
1. Fin AI (or routing) assigns a conversation to a human teammate.
2. Intercom sends a `conversation.admin.assigned` webhook to the Worker.
3. Worker verifies the `X-Hub-Signature` (HMAC-SHA1 of the raw body with the
   app client secret).
4. Worker acks immediately, processes in the background.
5. Worker checks the assignee is a human (not Fin / excluded bot ids) and the
   conversation hasn't already produced a ticket.
6. Worker extracts the contact's name/email (looks up the contact via the
   Intercom API if the payload doesn't include the email).
7. Worker creates the YouTrack ticket with `Customer Email` populated.

---

## 6. Repository layout

```
slack-youtrack/
├── src/index.js        # Slack Events API handler
├── src/youtrack.js     # shared YouTrack REST helper
├── wrangler.toml       # non-secret config (project id, channel allowlist)
├── .dev.vars.example   # template for local secrets
├── package.json
└── README.md

intercom-youtrack/
├── src/index.js        # Intercom webhook handler
├── src/youtrack.js     # shared YouTrack REST helper (copy)
├── wrangler.toml       # non-secret config (app id, Fin exclude list)
├── .dev.vars.example
├── package.json
└── README.md

docs/helpdesk-webhooks.md   # this file
```

Each README has service-specific setup detail; this doc is the overview.

---

## 7. Hosting: why Cloudflare Workers, and the alternatives

**The hard requirement:** Slack and Intercom deliver events as webhooks, so we
need a **public HTTPS endpoint with code running 24/7**. That cannot be avoided
— you can't point a webhook at a laptop, and YouTrack can't receive these
webhooks itself. The only question is *where* that endpoint lives.

**Why Cloudflare Workers is the current choice:**
- Free tier comfortably covers this volume (100k requests/day; we'll use a
  tiny fraction).
- Always-on, no server to patch, no cold-start billing.
- The whole thing is ~200 lines — not worth a VM.

**Portability — this is a genuine option, not a lock-in:** the business logic
(YouTrack calls, signature verification, field mapping) is standard JS. Only
the request/response wrapper is Workers-specific. Porting to another runtime is
a few hours, not a rewrite.

| Option | When it makes sense | Trade-off |
|--------|--------------------|-----------|
| **Cloudflare Workers** (current) | No existing infra, want zero maintenance | New vendor (free) |
| **AWS Lambda + API Gateway / Function URL** | Company already on AWS | Port the handler wrapper |
| **GCP Cloud Run / Cloud Functions** | Company already on Google Cloud | Port the handler wrapper |
| **Azure Functions** | Company already on Azure (we use Microsoft/Power BI) | Port the handler wrapper |
| **Existing always-on server / container** | Team already runs one | Add an endpoint + maintain it |
| **Make / Zapier / n8n / Pipedream** | Want no code hosting at all | Monthly cost, less control over verification/edge cases, rebuild logic in their UI |

**Recommendation:** if we already run AWS/GCP/Azure, host there to consolidate
vendors — the port is small. If we don't, Cloudflare Workers is the right call
and the code is ready. Avoid spinning up a dedicated server for something this
small.

---

## 8. Credentials & secrets

Seven credentials total. **Five are secrets** (never committed — set via
`wrangler secret put` or the platform's secret store). **Two are non-secret
identifiers** already in `wrangler.toml`.

| # | Credential | Kind | Used by | Where set |
|---|-----------|------|---------|-----------|
| 1 | YouTrack permanent token (`perm:…`) | secret | both | `YOUTRACK_TOKEN` |
| 2 | Slack signing secret | secret | slack | `SLACK_SIGNING_SECRET` |
| 3 | Slack bot token (`xoxb-…`) | secret | slack | `SLACK_BOT_TOKEN` |
| 4 | Intercom client secret | secret | intercom | `INTERCOM_CLIENT_SECRET` |
| 5 | Intercom access token | secret | intercom | `INTERCOM_TOKEN` |
| 6 | Intercom app id (`agc8lkpy`) | id | intercom | `wrangler.toml` ✅ |
| 7 | Fin bot admin id (`5286994`) | id | intercom | `wrangler.toml` exclude list ✅ |

**Slack bot scopes required:** `channels:history`, `channels:read`,
`groups:history`, `groups:read`, `users:read`, `chat:write`.

**Intercom permissions required:** Read conversations (for the contact/email
lookup). The app already has this.

---

## 9. Deployment

Per Worker (`slack-youtrack` and `intercom-youtrack`):

```bash
cd slack-youtrack        # or intercom-youtrack
npm install
npx wrangler login       # opens a browser to authorise the Cloudflare account

# DEPLOY FIRST. `wrangler secret put` fails with "This Worker does not exist
# on your account" until the Worker has been created by a deploy.
npx wrangler deploy

# then set the secrets (one at a time — the prompt reads a SINGLE value and
# displays nothing as you paste; pasting several at once sends the extra
# lines to your shell):
npx wrangler secret put YOUTRACK_TOKEN
npx wrangler secret put SLACK_SIGNING_SECRET     # slack only
npx wrangler secret put SLACK_BOT_TOKEN          # slack only
npx wrangler secret put INTERCOM_CLIENT_SECRET   # intercom only
npx wrangler secret put INTERCOM_TOKEN           # intercom only

# set the Slack channel allowlist in slack-youtrack/wrangler.toml first:
#   CUSTOMER_CHANNEL_IDS = "C0123ABC,C0456DEF"

npx wrangler deploy      # re-deploy after any config change
```

Copy the printed Worker URL — you need it for the wiring step.

### Post-deploy wiring

**Slack** (in the Slack app at api.slack.com/apps):
1. **Event Subscriptions** → enable → **Request URL** = the Worker URL. Slack
   sends a `url_verification` challenge; the Worker answers automatically and
   the field turns green.
2. **Subscribe to bot events:** `message.channels` (and `message.groups` for
   private channels).
3. **Invite the bot** to each customer channel: `/invite @YouTrack Helpdesk`.
4. Add each of those channel IDs to `CUSTOMER_CHANNEL_IDS` and redeploy.

**Intercom** (Developer Hub → your app → Webhooks):
1. **Endpoint URL** = the Worker URL.
2. **Subscribe to topic:** `conversation.admin.assigned`.
3. Use **Send test notification** (topic `ping`) → the Worker replies `200`.

### YouTrack project id — RESOLVED
The API rejects the short name with
`400 {"error":"bad_request","error_description":"Invalid structure of entity id: CS"}`.
Our instance requires the **internal id**, which is **`0-18`** — already set in
both `wrangler.toml` files. This was the single cause of the Slack integration
silently creating nothing for months. To re-derive it:
```bash
curl -H "Authorization: Bearer $YOUTRACK_TOKEN" \
  "https://myrealprofit.youtrack.cloud/api/admin/projects?fields=id,shortName,name"
```
Set `YOUTRACK_PROJECT_ID` in `wrangler.toml` to the returned `id` (e.g. `0-3`).

---

## 10. Security model

- **Authenticity:** every request is verified before any work happens — Slack
  via HMAC-SHA256 signing secret (+ 5-minute replay window), Intercom via
  HMAC-SHA1 client-secret signature. Unsigned/forged requests get `401`.
- **Secrets** live only in the platform secret store, never in git. `.dev.vars`
  is gitignored.
- **Least privilege:** Slack scopes and the Intercom token are scoped to only
  what's needed (read messages/conversations, post to Slack, look up contacts).
- **No customer PII stored:** the services hold nothing — they translate a
  webhook into a YouTrack ticket and forget it. YouTrack is the system of
  record.
- **Idempotency:** duplicate webhook deliveries are ignored, preventing
  duplicate tickets.

---

## 11. Testing & verification

- **Slack:** post a message in a test customer channel the bot is in → expect a
  new CS ticket (Channel=Slack) and a threaded `✅ Ticket CS-XXX` reply.
- **Intercom:** assign a test conversation to a human teammate → expect a new
  CS ticket (Channel=Intercom) with the contact email populated. Assign it to
  Fin instead → expect **no** ticket.
- **Logs:** `npx wrangler tail` streams live logs from a Worker for debugging
  signature failures, YouTrack errors, or filter decisions.

---

## 12. Operations & cost

- **Cost:** $0 on Cloudflare's free tier at expected volume.
- **Monitoring:** `wrangler tail` for live logs; Cloudflare dashboard for
  request counts and error rate. Optional: bind a KV namespace named `DEDUPE`
  (see each `wrangler.toml`) for cross-isolate dedupe if volume grows.
- **Failure modes to watch:** expired YouTrack token (issue creation returns
  401), Slack signing-secret rotation, Intercom client-secret rotation. All
  surface clearly in logs.

---

## 13. Open decisions / future work

- ~~Confirm YouTrack enum spellings~~ **DONE.** Verified against the live
  schema: `Channel` accepts Gmail/Intercom/Slack, `Type` accepts Task,
  `Replied` accepts `Not Replied`. All correct as written.
- ~~Whether REST-created issues become real helpdesk tickets~~ **DONE — they
  do.** CS-158 was created by the Slack Worker over `POST /api/issues` and is
  a full ticket: `Helpdesk` tag, `Customer Support - Helpdesk Team`
  visibility, SLA `First Reply` timer running, served at `/tickets/CS-158`.
  Structurally identical to a Gmail-channel ticket. **No online-form or email
  relay migration is needed.**

### Known defects (as of CS-158)

1. **The `auto-tag-and-route` workflow Gmail-ifies every ticket.** It prepends
   a `Source: Gmail - find original email` header and a Gmail search link to
   the description regardless of channel, so a Slack ticket claims to come
   from Gmail and links to a search that finds nothing. Our Slack header ends
   up nested inside it. Fix: skip the Gmail header when `Channel != Gmail`.
2. **`Customer Email` is the token owner, not the customer.** It resolves to
   whoever owns `YOUTRACK_TOKEN` rather than the person who wrote in Slack.
   Fix: add the `users:read.email` Slack scope, read `profile.email` in
   `getUserName`, and pass it as `customerEmail`.
- **Intercom scope** is currently "human escalation only." If we later want
  *every* new conversation to create a ticket, add the
  `conversation.user.created` topic — the handler is structured for it.
- **AI triage (Phase 6)** in the helpdesk plan is downstream of these and out
  of scope here.

---

## 14. What we need from the hosting owner

1. A decision on **where to host** (§7) — Cloudflare, or an existing cloud.
2. Access to set the **five secrets** (§8) in that platform.
3. Ownership of the **Slack app** and **Intercom app** already created, or a
   handover of them.
4. Sign-off to **invite the bot** to the real customer Slack channels and
   enable the Intercom webhook (go-live).
```
