# Helpdesk Webhooks — Slack & Intercom → YouTrack

**Status:** Slack and Intercom both LIVE and verified; Gmail live via YouTrack's
native email channel. Feature-by-feature state: see `helpdesk-status.md`.
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

### The workers act as Support - Agent, not a person

`YOUTRACK_TOKEN` is a token on the **Support - Agent** account
(`support@myrealprofit.com`), not on anyone's personal account. Before this,
every ticket, comment and relayed reply across all three channels read
**Filip Seidel** — including messages written by other people in Slack, which
is actively misleading about who said what.

It also narrows the blast radius considerably. Filip's account carries System
Admin, Contributor and Project Admin; Support - Agent carries Contributor
alone, and that proved sufficient to create issues, comment and attach files.
A dedicated `Helpdesk Bridge` user would have been marginally cleaner — a
non-agent cannot post public comments at all, which would make the relay loop
structurally impossible rather than guarded — but JetBrains bill per user and
this account already exists and is already paid for. Not worth a seat.

Two consequences worth knowing:

- Tickets created before the swap keep their old attribution. Authorship is
  recorded at creation and does not rewrite.
- YouTrack's built-in roles here are Contributor / Project Admin / System
  Admin; there is no Developer role in this instance, whatever the JetBrains
  docs describe.

#### Slack comments are credited to whoever wrote them — ON HOLD

**This does not work, and cannot be made to work as built.** Tested
2026-09-23. The code is left in place because the matching half of it is sound
and costs nothing; only the last step is a dead end.

What was proven, in order:

1. The email match works. The tail shows
   `comment: attributing to filip@myrealprofit.com (2-8)` — `2-8` is the real
   YouTrack user id — followed by the comment landing on CS-263.
2. `author: { id }` is sent on the request. YouTrack answers **200 OK** and
   discards the field without an error.
3. It is not a permission. Unchanged with the token account raised to Project
   Admin, then System Admin. *(Both have since been removed — they were granted
   only for this test.)*
4. JetBrains confirm it. The on-behalf-of mechanism exists for creating
   **issues**, so an agent can open a ticket without becoming its author; they
   state there are "no plans to make the same mechanism for comments".

So every Slack comment lands as **Support - Agent**. The author's real name is
already the first line of the comment body — "**Lucjan Rosłanowski** in Slack ·
2026-09-16 08:15 UTC" — so the information is there; only the avatar and byline
are wrong.

The one route that would work is posting with **the commenter's own permanent
token**: a comment made with Lucjan's token *is* authored by Lucjan, with no
`author` field for YouTrack to ignore. Everyone at MRP is a full YouTrack user,
so the reporter-type blocker JetBrains describe does not apply. Deferred
because it means the worker storing one long-lived, full-access credential per
person, with no expiry and a re-deploy needed to rotate or revoke one.

The matching itself stays and still runs: the worker looks the Slack author's
email up in YouTrack and passes `author: { id }`, which is a no-op until
JetBrains support it. The lookup is cached both ways —
`yt:user:<email>` — because most commenters repeat and because the negative
answer is the common one: customers in a shared channel have no account and
never will. The negative has a shorter TTL so somebody who joins next week is
picked up without anyone clearing a cache.

**Everything falls back to posting as the integration**, which is what happens
in every case today: no email on the Slack profile, no matching account, a
near-miss address, a failed lookup, or the field being ignored. A comment under
the wrong name still beats a comment lost.

The email match is **exact**. YouTrack's user search also matches on name and
login, so an unfiltered result would eventually attribute one person's words to
another — worse than not attributing them at all.

Intercom deliberately does not do this: those conversations are with customers,
who have no YouTrack account, and guessing at one would be wrong every time.

Comments created over REST arrive **internal** (locked to the helpdesk team)
regardless of the account's agent status, which is right: a customer's own
message relayed from Slack is context for an agent, not something to send back
to the customer.

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
edit. Its wording is editable under **Project Settings → Notifications →
Delimiter text**, and it can be switched off there entirely — at the cost of
less precise trimming of quoted history.

#### Where the sign-off lives

**In the email template, not in the agent signature setting.** The per-agent
"Agent signature" is appended to the comment *text*, which means it is stored
in YouTrack, shown on every comment in the ticket, and has to be stripped again
by both workers on the way to Slack and Intercom. Putting it in
`helpdesk_comment_for_reporter_email.ftl` instead means it renders for email
and nowhere else:

```
<p style="${styles.paragraph}">Kind regards,<br>${(comment.author.visibleName!"")?split(" ")?first!""}<br>My Real Profit</p>
```

`comment.author.visibleName` is whoever wrote the comment, so Lisa's replies
sign Lisa and Angelina's sign Angelina — a fixed name would be wrong the moment
a second person answers. `?split(" ")?first` takes the first name only; the
`!""` defaults guard an account whose full name is empty, which would otherwise
send a customer "Kind regards," and a blank line.

The workers still strip signatures from comment text. That is now belt and
braces rather than the only defence, and it keeps older comments — written
while the setting was in use — from carrying a sign-off into a chat.

**The `MAIL:` summary prefix was dropped for Gmail tickets** (the rule in
`auto-tag-and-route` is deleted, sender name included). Gmail is the one
channel where the ticket title lands in a customer's inbox, and
`MAIL: Testowanie helpdeska - Ryszard Chmura` shows them our routing prefix and
their own name appended to their own subject. The board loses nothing: the
`Channel` field and the reporter column already carry both. Slack and Intercom
keep their prefixes — those titles never leave YouTrack.

### Two-way replies (Intercom) — built

An agent's **public** comment is sent to the customer as a real reply in the
chat (`message_type: "comment"`). An **internal** one sends nothing at all —
not even a note.

That last part is a deliberate change. Notes were scaffolding from the period
when nothing was allowed to reach a customer; leaving them in would put an
agent's private thinking into the conversation record where the customer's own
history lives. The only note that remains is the one posted when a ticket is
created.

Same contract as Slack, on purpose: one habit for an agent across all three
channels. `ticket:<issueId>` maps a ticket back to its conversation, written
alongside the forward key at creation.

When that key is missing — every ticket filed before it existed — the worker
recovers the id from **the Intercom link in the ticket's own description**,
then caches it. The conversation id is already written down there and cannot
drift, so no ticket is stranded by the mapping arriving late. A ticket with no
such link is refused rather than guessed at.

#### One conversation, three places, one story

Slack, Intercom and the ticket should all show the same exchange. Three paths
keep them together, and each one has to avoid feeding the others.

| Written in | Reaches | How |
|---|---|---|
| YouTrack (public comment) | Intercom **and** Slack | The worker replies to the conversation; Intercom posts it into the thread |
| Intercom inbox (teammate) | Slack **and** YouTrack | Intercom posts it into the thread; `mirrorAdminReplies` copies it onto the ticket |
| Slack (customer) | Intercom **and** YouTrack | Fin creates the conversation; `commentNewMessages` copies it onto the ticket |

##### Who the reply appears to come from

Intercom, unlike YouTrack, **honours the sender**: pass `admin_id` on a reply
and the customer sees that teammate's name and avatar. So the worker reads the
YouTrack comment's author, matches them to an Intercom admin **by exact email**,
and sends as them — an answer Filip writes in YouTrack reaches Slack as Filip.

Matching is exact because the cost of a loose match is a reply signed by the
wrong colleague, which is worse than one signed by the team account. No match,
no email, or a failed lookup all fall back to the service account, because a
reply under the team's name still beats a reply that never went.

This is the same wish that proved impossible on YouTrack's own comments — and
it works here only because Intercom supports what YouTrack does not.

**Decision (2026-09-25): MRP is not creating Intercom seats per person.** Only
people who already hold one — Lisa, and whoever else exists — can be attributed
this way. Everyone else reaches customers under the service identity with their
name in the message body, which is the intended end state rather than a gap. So
the two steps below apply only to teammates who already have a seat; do not
propose creating seats for the others.

**Where a seat does exist, two things are needed, and neither is code:**

1. **Their own Intercom seat**, on the same email as their YouTrack account. A
   shared seat cannot tell one teammate from another.
2. **They click "Authenticate your Slack account"** themselves, in the Intercom
   inbox. It OAuth-links that teammate to that person's Slack account and needs
   the *Can manage workspace data* permission.

It **cannot be done centrally**: the link requires signing in to Slack as that
person. Anyone who skips it falls back to the generic team name — nothing
breaks, the reply is simply not personalised.

Verified 2026-09-25: after authenticating, a comment written in YouTrack and
relayed **through the API** arrived in the Slack thread under *Filip Seidel*
with his avatar, where the same path had shown *MRP Support Team* minutes
earlier. So a reply carrying `admin_id` inherits that teammate's linked Slack
identity — not only replies typed in the inbox, which is all the documentation
promised.

**The failure mode to avoid:** authenticating the *shared* seat against one
person's Slack. Every reply from that seat then appears as that person,
colleagues' included — right in testing, wrong in front of a customer.

It is worse than it first looks, because the shared seat is also the
**fallback** for anything that cannot be attributed. A YouTrack comment whose
author has no Intercom seat goes out as the token owner — so if a person has
authenticated that seat, **their** name and avatar appear on **someone else's**
words. Confidently wrong, which beats vague only in the wrong direction.

Two things guard against it, and only the first actually closes it:

1. **Config, and this is the real fix:** the seat used as the service identity
   must be one no person has authenticated. Since most people have no seat of
   their own, that shared seat is the fallback for nearly every reply — so a
   personal Slack link on it puts one colleague's face on everybody's words.
   Revoking that link is required, not optional, and it does not disturb a
   teammate who authenticated their OWN seat.
2. **Code, as a safety net:** a reply that could not be attributed is prefixed
   with the author's name in bold, so the text says who wrote it even when the
   avatar cannot. The name is escaped before it is added — it is user data, and
   a colleague's display name must not be able to inject markup into a message
   going to a customer. Attributed replies are not prefixed, since they already
   carry the right name.

The email must match end to end, YouTrack account → Intercom teammate. Someone
whose two addresses differ is silently unmatchable.

##### Not echoing

An answer must not travel YouTrack → Intercom → back to YouTrack. Two marks
prevent it, both written **before** the action they guard:

- Relaying outward, the worker reads back the id of the part Intercom created
  and marks it `mirrored:<part id>`, so the inbound mirror skips it. The part is
  matched **by body**, not taken as "the last one" — a message arriving in the
  same moment would otherwise be silenced instead of ours.

  That comparison must be `sameBody()`, never `===`. **Intercom does not store
  a reply verbatim**: send `cool` and it comes back `<p>cool</p>`, or
  `<p class="no-margin">2000</p>`. An exact comparison never matched, which
  left this guard inert — harmless only for as long as the inbound mirror was
  not running. Tags and whitespace are stripped from both sides before
  comparing.
- Mirroring inward, the YouTrack comment it creates is marked
  `relayed:<comment id>`, so the outbound rule does not send it back out.

Both `comment` and `assignment` parts are mirrored. **Intercom's
assign-and-reply produces an `assignment` part carrying the message**, so
taking only `comment` dropped most first replies — the one where a teammate
picks the conversation up and answers in the same action.

The mirrored comment is created **public**, because it was public — the
customer has already seen it. Fin's own messages are not mirrored: they are
many, and what matters about them is already on the ticket under "Fin already
tried".

##### Intercom must be subscribed to the topic

Accepting `conversation.admin.replied` in the worker does nothing unless
**Intercom is configured to send it** — the topic has to be ticked in the
webhook subscription. Nothing in this repository can enable it, and its absence
looks exactly like a code fault: replies typed in Intercom simply never appear
on the ticket.

##### The topic that must stay mirror-only

`conversation.admin.replied` was once ignored entirely, because treating a
teammate's reply as a handoff **filed a ticket every time Lisa answered a
client**. That reasoning still holds and is now enforced rather than implied:
the topic is in `MIRROR_ONLY_TOPICS`, returns before any handoff or ticket
decision, and does nothing at all unless a ticket already exists.

#### Which worker gets told about a comment

The YouTrack rule tells **both workers** about every new comment and lets
**ownership** decide which one acts. Each looks the ticket up in its own store;
the one that recorded it relays the comment, the other logs a line and stops.

It used to route on the `Channel` field, and that broke the moment Fin started
answering in Slack. Such a ticket is **Slack to a human reading it and Intercom
to the machinery that created it**, so `Channel = Slack` sent it to the Slack
worker, which had never heard of the ticket and silently did nothing. The field
cannot carry both meanings, so routing stopped using it.

A Slack thread that Fin owns needs only the Intercom call: **Intercom posts an
agent's reply straight into the Slack thread**, so one public comment appears in
both places. Relaying it through the Slack worker as well would show the
customer the same answer twice — which ownership prevents, because the Slack
worker has no record of that ticket.

"Not my ticket" is therefore normal rather than exceptional, and both workers
log it at info level so that real failures remain visible.

#### Fin in Slack — how it should behave (open design)

**This is the project's current main focus.** The plumbing is proven (see the
section below); what is undecided is when Fin should speak and how it steps
back. These are settings and conventions, not code in this repo, but they
determine what reaches YouTrack.

##### When Fin responds

Out of the box Fin answers **every message** in a connected channel. Verified
2026-09-25: "hi" drew "Hello! How may I assist you today? 😊". That is wrong for
our customer channels, where clients talk to each other and do not treat Slack
as a knowledge base.

Fin over Slack inherits the configuration set for Fin over chat — audience
rules and handover — and the trigger lives in **Workflows**. Available triggers
are **@mention, keyword and emoji**. Intercom's own guidance: *avoid
over-triggering by setting clear keywords and mentions*.

**Decision: @mention-only** for customer channels. It puts a human intention in
the loop, which is the same reason the Slack worker moved off `always` mode.

Planned gesture vocabulary, one meaning each:

```
@Fin …     ask the bot, answered in thread
react 🎫   file this as a ticket (slack-youtrack, unchanged)
overnight  night mode catches what nobody triggered
```

Worth settling before customers learn it; changing it afterwards is expensive.

##### Two risks this is guarding against

**Interjecting in a client-to-client conversation.** In a Slack Connect
channel that is visible to the customer's whole team. Two people discussing
something, Fin answers confidently from the help centre and has the context
wrong — worse than silence, and public in a way a 1:1 Messenger chat is not.

**Cost.** Fin bills **$0.99 per outcome, at most once per conversation** (one
Slack thread = one conversation). Nothing is charged when the customer asks for
a human — CS-265 was free. But a customer who does not reply within 24 hours of
Fin's last answer counts as an **assumed resolution and bills**. So a greeting
Fin answers and nobody closes out costs $0.99. In a chatty channel that is the
real exposure, not verbosity. There is also a 50-outcome/month minimum (~$49).

##### Stopping Fin inside a thread — UNRESOLVED

There is **no un-mention**. The documented way to stop Fin in a conversation is
for a **human to reply or take the conversation over**. Two caveats from
Intercom's community: Fin may respond again in escalated threads when workflow
criteria still match, and there is a reported case of it resuming after a
teammate replied and the customer replied again.

**The open question, and the next thing to test:** when an MRP person replies
*in the Slack thread*, does Intercom record it as an **admin reply** (Fin steps
back) or as another **contact message** (Fin keeps going)? This is the
difference between "just answer and Fin gets out of the way" and "you must open
the Intercom inbox to silence it". The likely deciding factor is Intercom's
*"Authenticate your Slack account to reply as yourself"* prompt — an
authenticated teammate's Slack reply probably registers as an admin reply.

That same authentication is worth noting for another reason: an authenticated
agent's reply posts into Slack **under their own name and avatar**. That is the
comment-attribution problem, solved on the Fin path by Intercom, with nothing
for us to build. It does not revive YouTrack's comment `author` field, but it
means replies sent through Intercom carry the real person while replies sent
through the Slack worker post as the bot.

##### Guidance customers will need

A pinned message per channel, once the trigger is decided. Roughly: *@Fin for
instant answers from our help centre; otherwise just talk normally — we are
watching the channel.*

#### A Slack thread reaching YouTrack through Fin

Fin answers in Slack as well as in the Messenger, and a connected Slack thread
becomes an **ordinary Intercom conversation** — same webhooks, same escalation
states, same path into YouTrack. Verified end to end on 2026-09-25: a question
in #helpdesk-testing, Fin's answer, "No, I still need help", and CS-265.

By the time the conversation reaches this worker, nothing in the shape of the
payload says it came from Slack. The only marker is a custom attribute:

```
custom_attributes["Slack channel"]    e.g. "helpdesk-testing"
custom_attributes["Slack workspace"]  e.g. "My Real Profit"
```

`conversationOrigin()` reads it and switches the ticket's prefix to `SLACK:`
and its **Channel** field to `Slack`, and names the channel in the description.
Without it every Slack escalation files as `INT:` with Channel = Intercom —
wrong in the title and, worse, silently wrong in channel reporting. CS-265 was
created before this and carries the old labels.

The contact is resolved by Fin, not by us: it arrives with
`external_id: "slack:<Slack user id>"` and, where Fin can determine it, the
person's email. So a Slack customer gets a real Intercom contact with history,
which is more than the Slack worker can do on its own.

The description links **Slack first, then Intercom** — the thread is where the
customer actually is; the Intercom conversation is the mirror of it.

The permalink comes from Fin itself. When triggered in Slack it opens the
conversation with a **note**:

```
View this conversation in Slack:
https://<workspace>.slack.com/archives/<channel>/p<ts>?thread_ts=<ts>&cid=<channel>
```

That note is the only place the thread timestamp appears — the conversation's
own fields carry the channel name but no timestamp, so the link cannot be
reconstructed from them. `slackThreadLink()` reads it from the **notes only**,
never from the customer's own messages: someone pasting a Slack link into their
question must not be mistaken for the thread of record. With no note, the
description falls back to naming the channel.

Comment text is converted to Intercom's HTML: the agent signature and any
`![](image.png){...}` left by a pasted image are dropped, and everything is
**escaped before any tag is added**, so an agent cannot accidentally send a
customer markup they did not intend. Attachments are not carried yet — Intercom
takes files by public URL, and whether YouTrack's signed attachment links
qualify is untested.

Verified: a public comment arrives as a visible reply; an internal one produces
no Intercom request whatsoever; a retry does not double-send; a wrong secret is
refused; and an ordinary Intercom webhook on the normal path still has its
signature checked.

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

**Attachments on a public comment travel with it.** A screenshot an agent
attaches in YouTrack appears in the Slack thread. Requires the `files:write`
bot scope.

`files.upload` was retired, so this uses Slack's three-step external flow: ask
for an upload URL, POST the bytes, then share the result into the thread. The
`thread_ts` must be the thread **parent** — Slack rejects a reply's ts — which
is what `slack:ticket:<id>` already stores.

Order and failure handling matter here:

- Files go **after** the text, so a file that will not move cannot cost the
  answer. An upload failure is logged and swallowed; the agent's words have
  already reached the customer.
- Anything over `MAX_RELAY_FILE_BYTES` (20 MB) is skipped with a logged reason.
  Every byte passes through the worker's memory, which is far smaller than
  either service's. A screenshot is the case that matters; a video is the case
  that would take the relay down with it.
- A comment with an attachment and no text still sends, with the agent's name
  as the file's comment.
- The log counts what **arrived**, not what was attempted — `(0/1 file)` when
  an upload failed, because a line claiming a file was sent when it was not is
  worse than no line at all.

An internal comment sends neither its text nor its files.

**Duplicate suppression is best-effort, not exact.** The relay is guarded by
claims in KV, and KV is eventually consistent: two firings arriving close
together in different locations can both read a comment as unclaimed and both
relay it. A photo was sent twice once, on the first test after deploy.
Attachments carry a second, narrower claim of their own (`slack:file:<id>`),
taken before the bytes move — the upload is the slow part and therefore the
widest window — because a duplicated sentence is untidy where a duplicated
photo looks broken.

That narrows the window rather than closing it. Closing it properly needs a
primitive with real atomicity — a Durable Object — which is a paid-plan
feature. Worth doing if duplicates recur; not worth it for one occurrence that
has not repeated across later tests.

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
- **Narrow the YouTrack token.** Done in part: the workers now run as the
  `Support - Agent` service account rather than a personal admin token. Project
  Admin and System Admin were added to that account to test comment author
  attribution and should be removed now the answer is known.
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
