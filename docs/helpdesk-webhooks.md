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
| **Slack** | Selectable — see the table below | Set with `SLACK_MODE`; `night` and `emoji` can run together. |
| **Intercom** | **Fin hands off to a human** — `ai_agent.resolution_state` reaches one of `INTERCOM_HANDOFF_STATES`, on a conversation that is still open and recent | Assignment is deliberately *not* a trigger: an agent replying to a chat must not file a second ticket. |

### Slack modes (`SLACK_MODE`)

One line in `wrangler.toml`, then `npx wrangler deploy`.

| `SLACK_MODE` | Files a ticket when | Good for |
|------|-----------|----------|
| `always` | Every new thread in an allowlisted channel, round the clock | A dedicated support channel where every thread is a request. The original behaviour. |
| `emoji` | Someone reacts `:ticket:` to a message, at any hour | A shared channel where only some messages are requests. The reactor also picks *which* message states the problem — the single biggest lever on title quality. |
| `night` | A new thread is posted between 20:00 and 08:00 | Out-of-hours cover. Nobody is watching Slack, so nothing can be handled live and everything needs a ticket waiting in the morning. No reaction needed. |
| `night+emoji` | Either of the above | Overnight messages file themselves; during the day the team decides case by case with a reaction. |
| `off` | Never. Decisions are still logged | Watching real traffic without touching YouTrack. |

What each mode does with the same four events:

| | posted at night | posted by day | `:ticket:` at night | `:ticket:` by day |
|---|---|---|---|---|
| `always` | file | file | — | — |
| `emoji` | — | — | file | file |
| `night` | file | — | — | — |
| `night+emoji` | file | — | file | file |
| `off` | — | — | — | — |

`night` is matched against **when the customer posted**, which is the whole
point of it — the message arrived when no one was there. Like `all` it fires
only on a thread parent: a reply continues a conversation that has already been
dealt with one way or the other. `emoji` fires on replies too, so you can react
to the message halfway down a thread that actually states the problem.

`SLACK_NIGHT_HOURS` sets the window (20:00 is inside it, 08:00 is outside).
`SLACK_HOURS_TZ` takes a fixed offset (`+02:00`, the default) or an IANA zone
(`Europe/Warsaw`). **A fixed offset does not follow daylight saving** — when
Poland returns to `+01:00` on 25 October 2026, a `+02:00` window starts an hour
early. `Europe/Warsaw` tracks the change. A malformed window or zone treats
every hour as in-window and logs the error: a missed ticket is worse than an
extra one.

### Custom mode (`SLACK_MODE = "custom"`)

Falls through to `SLACK_TRIGGERS`, a comma list combining any of `all`,
`night`, `emoji`, `mention` (the message @-mentions the bot) and `keyword` (it
contains `SLACK_TRIGGER_KEYWORD`, default `!ticket`, which is stripped from the
title). The named modes above are shorthands for the useful combinations.

`all` only ever fires on a thread parent — a reply continues a conversation
that already has, or deliberately does not have, a ticket. The other three fire
on replies too, because someone may only realise halfway down a thread that it
needs filing. `SLACK_CONFIRM` controls what the bot says back: `thread` (public reply,
default), `ephemeral` (visible only to whoever triggered it — lets you test in
a live customer channel without the customer seeing anything), or `off`.

**`CUSTOMER_CHANNEL_IDS` empty means every channel the bot has been invited
to.** That is the setting to check first when tickets appear from somewhere
unexpected.

Everything is deduplicated so webhook retries never create duplicate tickets
(Slack by `event_id`, Intercom by conversation id). Slack additionally keys
`slack:msg:<channel>:<ts>` to the ticket id, so a message cannot be filed twice
when two trigger modes both match it — reacting to a message that already
auto-filed replies with the existing ticket number instead.

---

### Slack ticket contents

The description carries the **whole thread**, not the one message that tripped
the trigger — reacting to the fifth message used to throw away the four above
it, which is where the customer had explained the problem. Messages are quoted
verbatim and attributed, with an arrow marking the one that triggered filing,
so an agent is acting on what the customer actually wrote rather than a
paraphrase.

Slack's wire format is rendered on the way in: `<@U03ABC>` becomes the person's
name, `<url|label>` becomes a markdown link, and Slack's own `&amp;` escapes
are undone. Agents without a Slack seat were otherwise reading raw ids.

Files attached anywhere in the thread are **re-uploaded into YouTrack** rather
than linked — `url_private` needs the bot token and is useless to an agent
without a Slack seat. Uploads happen after the issue exists and each file is
attempted independently, so a failed upload can never cost us the ticket.
Requires the `files:read` **bot** scope.

> **If a download returns HTTP 403**, the token doing the fetching has no
> `files:read`. The OAuth & Permissions page has two separate lists — Bot Token
> Scopes and User Token Scopes — and a scope added to the User list never
> reaches the bot token, however many times the app is reinstalled. On a 403
> the worker logs the scopes the deployed token actually carries, which
> settles it in one line. After fixing the list, reinstall the app **and**
> re-run `wrangler secret put SLACK_BOT_TOKEN`: a token carries the scopes it
> was issued with. (This cost us CS-224 through CS-226.)

### After the ticket exists

A reply in a thread that already has a ticket is added to that ticket as a
**comment**, with any files attached, rather than opening a second ticket.
Without this a ticket was a snapshot of the thread at the moment it was filed:
"actually it is also affecting Gonorth", sent two minutes later, never reached
the agent working it.

**One thread is one ticket.** Everything is keyed on
`slack:thread:<channel>:<threadTs>`. Keying it per message instead let a second
`:ticket:` reaction elsewhere in the same thread open a second ticket for the
same conversation — and replies afterwards could only ever land on one of them,
so the other silently went stale. Reacting again in a filed thread now answers
with the ticket it already has.

A short-lived `creating` claim is written before the issue is created, so two
reactions landing together cannot both get through, and it is deleted if
creation fails — one failed attempt must not wedge a thread out of ever being
filed. Bot messages are excluded throughout, which also stops an agent reply
relayed into Slack from returning as a comment on the ticket it came from.

### When filing fails

Until now a failed creation was logged and nothing else: the event was
acknowledged, and the customer's request simply never became a ticket. For a
helpdesk that is the worst failure available, precisely because it is
invisible.

The worker now posts a warning to `SLACK_ALERT_CHANNEL` — or into the thread
when none is set — naming the channel, linking the message, and quoting the
error. It ignores `SLACK_CONFIRM`, which governs routine confirmations; a
request that silently failed to become a ticket is not routine.

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

### Defects found and fixed (CS-158 → CS-160)

1. ~~`auto-tag-and-route` Gmail-ifies every ticket~~ **FIXED in YouTrack.**
   Its "Build and prepend description header" step now returns early unless
   `Channel == Gmail`, so only Gmail tickets get the Gmail header and search
   link. Verified on CS-159.
2. ~~`Customer Email` is the token owner, not the customer~~ **FIXED in the
   Worker.** The `users:read.email` bot scope was added and `getUser` now
   returns `profile.email`, passed through as `customerEmail`. The workflow
   only writes that field when empty, so it defers to ours. Verified on
   CS-160.

### Workflow inventory (project CS)

| Workflow | Type | Guard | Notes |
|---|---|---|---|
| Auto-tag and Route | on-change | `isReported && (isNew \|\| becomesReported)` | Sets Channel/Replied/Customer Email defaults, Helpdesk tag, Type and Priority heuristics, and the Gmail header (Gmail only). |
| Gmail — Email | SLA | `Channel == Gmail` | First Reply 4h/8h/24h/48h/72h by Priority. |
| Slack — Customer | SLA | `Channel == Slack` | First Reply 1h/2h/4h/8h by Priority. Correct since June; had no tickets to act on until CS-158. |
| Intercom — Live Chat | SLA | `Channel == Intercom` | Same tiers as Slack. Untested. |
| Process Spam | on-change | `spam` tag added by admin | JetBrains stock. |
| Helpdesk | app | — | JetBrains built-in. Do not edit. |

### Ticket titles

Every ticket is named `SOURCE: what they want - who asked - which account`:

```
SLACK: I've shared the list and am looking to connect the dashboard… - Filip Seidel - gigabrain
INT: P&L shows nothing, looks like a Power BI issue - filip@myrealprofit.com
MAIL: Custom Category page - Ryszard Chmura
```

The prefix makes a mixed board readable at a glance and the trailing name and
account let a ticket be routed without opening it. Links are stripped before
the title is built (keeping the label where the sender wrote one) after a
pasted Google Sheets URL swallowed the whole of CS-172's title, and opening
pleasantries are dropped.

"Account" is the Slack **channel** — which for client channels is the client —
or the Intercom **company** when the contact is linked to one. Gmail has no
equivalent, so those titles carry sender only.

The three sources build titles in two places: the Slack and Intercom workers
each set their own, while Gmail's is assembled by the `auto-tag-and-route`
workflow, because Gmail is YouTrack's native channel and no worker sees it.

## Possible upgrades

### AI-written problem statements — built, off by default (Slack)

Implemented in `slack-youtrack/src/summarize.js`, switched by `TITLE_AI` and
**off until an `ANTHROPIC_API_KEY` secret exists**. Intercom and Gmail still use
the deterministic title.

The middle of the title used to be a cleaned truncation of what the customer
wrote — honest but blunt, and often the middle of a sentence:

```
SLACK: or just the number of accounts that are actually connected? As you… - Filip Seidel - #helpdesk-testing
```

With `TITLE_AI = "true"` a single `claude-opus-5` call reads the thread and
returns a problem statement and, where the customer named one, the account:

```
SLACK: Confirm real connected-account count for Gonorth billing - Filip Seidel - Gonorth
```

Two rules hold this in place, both because **YouTrack will not let a helpdesk
ticket be retitled after creation** — whatever is written is permanent:

1. **The model writes the title only.** The description stays the customer's
   verbatim words. Nobody should act on a sentence a model invented, and
   confining the model to a header bounds the damage a bad one can do.
2. **The account must appear literally in the thread.** The model proposes,
   `accountInThread()` verifies it is a substring of what the customer actually
   typed, and drops it otherwise, falling back to the channel name. Without a
   list of real seller names to choose from, this is what keeps an invented
   shop out of a permanent field. It is weaker than a real list — it cannot fix
   a typo or resolve an alias — so if a seller list ever becomes available,
   matching against it is a straight upgrade.

Every failure returns null and the caller keeps the quoted title: no key,
timeout (`TITLE_AI_TIMEOUT_MS`, default 12s), HTTP error, unparseable answer,
empty problem. Switching this on cannot cost a ticket, only the improvement.

The call uses `thinking: { type: "disabled" }` and `effort: "low"` — it sits
between the customer writing and the ticket appearing, and a one-line
extraction does not need deliberation. Each run logs its latency and exact
token counts.

#### Dry run first

```bash
export SLACK_BOT_TOKEN=xoxb-…  ANTHROPIC_API_KEY=sk-ant-…
node scripts/title-dryrun.mjs C0BDDQWPZB4 --limit 20
```

Replays real threads from a channel and prints today's title beside the one the
model would write, **creating nothing**, then totals the tokens used and how
often the account guard fired. Because titles cannot be corrected afterwards,
the first production run is already permanent — this is the only chance to
judge the change on real traffic before that. It imports the same builders the
worker uses rather than copying them, so what it prints is what you would get.
`--file threads.json` replays pasted threads when reaching Slack is awkward.

### Replying to an email customer — it was never a bug

Replies from a mail ticket appeared to go to the support inbox instead of the
customer, and this was carried as an open defect for days. It was not one.

A helpdesk comment box has a single toggle: **"Internal, visible to Customer
Support - Helpdesk Team"**. On, it is a private note nobody outside the team
sees. Off, it is emailed to the customer. There is no separate "reply" button —
that toggle *is* the public/private flag.

Only users designated **agents** may turn it off. Being System Admin or Project
Admin does not confer it; it is a separate designation on the project's People
page (⋯ on the row → *Make an agent*), and YouTrack says so in the comment box
when you lack it. Every "reply" written before that was an internal note that
was never going to reach anyone.

**Making someone an agent flips the default to public.** Before that, nothing
they write can escape; afterwards, an internal note requires deliberately
switching the toggle on. Tell anyone newly made an agent, on a project where
the habit has been that comments are safe.

Proven end to end on CS-233: a comment with the toggle off arrived at an
external Gmail from "My Real Profit Support", carrying YouTrack's
`##- Please enter your reply above this line -##` marker so the customer's
answer threads back onto the same ticket. Agent seats are normally licensed
separately — worth checking the plan before adding people.

### What the email customer actually receives

The only part of this system a customer ever looks at, so it is worth the
attention the internals get.

`commentForReporter` is the template that carries an agent's public reply —
confirmed by reading it, not inferred. That also settles a worry the subject
line raised: customers are **not** on `issueDigest`, so they are not pinged
about internal field changes.

Three things were stripped from it:

| Was | Why it went |
|---|---|
| `[Ticket Updated]` in the subject | Reads as a system notification, not a person answering. Removing it also makes the subject match what the customer originally sent, so it threads in their mail client. |
| The grey *"Updated by … on …"* box | The agent signature already says who wrote it. |
| The JetBrains footer | Edited in `helpdesk_footer.ftl` itself rather than removed from each template — one edit covers the confirmation, reminder and auto-close mails too, and there is no template left to forget. |

`##- Please enter your reply above this line -##` is load-bearing: it is how a
customer's reply threads back onto the ticket. Check it survives any template
edit.

**The `MAIL:` summary prefix was dropped for Gmail tickets** (the rule in
`auto-tag-and-route` is deleted, sender name included). Gmail is the one
channel where the ticket title lands in a customer's inbox, and
`MAIL: Testowanie helpdeska - Ryszard Chmura` shows them our routing prefix and
their own name appended to their own subject. The board loses nothing: the
`Channel` field and the reporter column already carry both. Slack and Intercom
keep their prefixes — those titles never leave YouTrack.

### Two-way replies (Slack) — built

An agent answers in YouTrack; the customer sees it in the Slack thread.
**What travels is decided by the comment's visibility, and nothing else.**

| The agent | The customer |
|---|---|
| Posts a **public** comment | Sees it in the Slack thread |
| Leaves **"Internal, visible to Customer Support - Helpdesk Team"** on | Sees nothing |

That is the same toggle an agent already uses on a Gmail ticket, so there is
**one habit across every channel**. The earlier design used a `Reply:` prefix
on Slack while Gmail used the toggle; two conventions is one too many, and the
thing forgetting the wrong one leaks is an internal note about a customer, to
that customer.

**The workflow does not decide what is public**, and does not say which comment
changed either. It reports only the issue id. The worker then reads that
issue's recent comments over REST and sends every public one it has not sent
before.

That shape was chosen after two guesses failed. Comment visibility is REST-side
state, so a workflow's view of it cannot be trusted — and whether a workflow's
`comment.id` matches a REST comment id is documented nowhere, so asking the
workflow *which* comment would have been a second unverified assumption. The
issue id is the one identifier already proven to work. Reading the list instead
makes the pass **idempotent and self-healing**: a firing that arrives late,
twice, or batched still lands each comment exactly once, and a comment missed
during an outage is picked up by the next firing.

`RELAY_MAX_AGE_MS` bounds that self-healing to an hour. Without it, the first
firing after a fix would flood the customer with every public comment ever
written on the ticket.

The agent signature YouTrack appends to public comments is stripped before
sending: in Slack it would land under the `*Name:*` prefix the relay already
adds, so the customer would read the sender's name twice and a written sign-off
on a chat message.

`isPublicComment()` **fails closed**. A comment travels only when YouTrack
positively says it is unrestricted — no visibility object, or
`UnlimitedVisibility` with nothing listed. Every other shape, *including one we
do not recognise*, counts as internal, as does a comment that could not be read
at all. A false negative costs an agent one re-send; a false positive costs a
customer relationship.

**Two loops had to be closed** when visibility replaced the prefix, because the
rule now fires on every comment rather than on a rare keyword:

- The worker posts each customer message from Slack into the ticket, and those
  comments are public — nothing sets otherwise. Each is claimed in
  `slack:relayed:<commentId>` the moment it is created, so the rule firing on
  it finds it already sent. Without that claim the customer's own words would
  be posted back at them, once per message, forever.
- The workflow no longer writes failures back as a comment. That was more
  helpful, but a failure comment re-fires the rule, fails again, and comments
  again — unbounded whenever the worker is unreachable. Failures show in the
  worker log and YouTrack's workflow error log instead.

Verified: a public comment posts; an internal one does not; an unreachable
YouTrack relays nothing rather than guessing; a Slack-sourced comment does not
bounce back; a retry does not duplicate; a wrong secret is refused; and a
literal "Reply:" in the text is now just text.

`slack:ticket:<id>` maps a ticket back to its thread, written at creation
alongside the forward key. **Tickets created before this shipped have no such
key**, so replies on them log "no Slack thread recorded"; only tickets filed
from now on can be answered this way. After a successful send the ticket's
`Replied` field flips, so the board shows who is still waiting.

Attachments on replies are not carried (that needs `files:write`); text is the
case that matters and files can follow later.

### Give other projects their own sender address

Every YouTrack notification from every project goes out as
`support@myrealprofit.com`, because that is the **global** From address. Mail
is sent through the domain itself (`mailed-by: myrealprofit.com`), so each one
is archived in the support mailbox — an SPB notification addressed to Nestor
still lands there as a sent copy.

Nothing is broken: the headers show `to: nestor@myrealprofit.com`, so nothing
is being *delivered* to support. It is outgoing mail in All Mail, not clutter
in the inbox. But support@ is a customer channel, and internal issue traffic
for unrelated projects should not be flowing under its identity.

Fix: set the global From address (**Administration → Notifications**) to
something neutral like `MyRealProfit <noreply@myrealprofit.com>`, and keep
`My Real Profit Support <support@myrealprofit.com>` on the CS project alone.

One thing to check first: whether YouTrack sends through support@'s own
mailbox credentials rather than JetBrains' servers. `mailed-by:
myrealprofit.com` suggests it does, and in that case changing the From address
alone will not stop the copies being saved — the sending mailbox has to change
too.

Note also that the From address must be a parseable
`Name <address@domain>` pair. It was set to the bare text
`My Real Profit Support`, which silently blocked every save on that settings
page with "Cannot parse email address" — including unrelated edits, since the
form validates every field.

### Others worth considering

- **Slack trigger.** Every first message in a monitored channel currently
  becomes a ticket, which is wrong for a working client channel. A reaction
  (a human puts 🎫 on a message) puts a person in the loop and needs no
  cleverness; the app already subscribes to `reaction_added` and holds the
  `reactions:read` scope, so the plumbing exists.
- **Intercom escalation routing.** Escalated conversations are left unassigned
  with `sla_applied: null`, so nobody owns them and no Intercom-side clock
  runs. An assignment Workflow would fix that and would also give this
  integration a real webhook to trigger on, replacing the state polling.
- **Attachments from Slack.** Intercom attachments are copied into YouTrack;
  Slack file uploads are not yet.
- **Narrow the YouTrack token.** It currently carries YouTrack Administration
  scope, which ticket creation does not need.
- **Upgrade wrangler.** Both workers are pinned to v3 while v4 is current.

### Remaining limitations

- **The ticket reporter is the `YOUTRACK_TOKEN` owner**, not the customer.
  REST cannot set it. Acceptable for Slack and Intercom, where replies go
  back to the source thread rather than by email; `Customer Email` carries
  the real identity. It would matter if these customers ever needed to reply
  by email.
- **Guests and Slack Connect users may expose no email.** `Customer Email`
  then falls back to the reporter via the workflow.
- **`CUSTOMER_CHANNEL_IDS` is empty**, which means EVERY channel the bot is
  invited to files tickets, private ones included (`message.groups` is
  subscribed). Set the allowlist before inviting the bot anywhere real.
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
