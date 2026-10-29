# MRP Helpdesk — project report

**Updated:** 2026-10-29 · **Owner:** Filip Seidel
**Scope:** what was built, what it runs on, how it works, and how it got here.

For the feature-by-feature state see `helpdesk-status.md`. For the full
technical reference — every setting, trap and verification — see
`helpdesk-webhooks.md`. This page is the overview that sits above both.

---

## 1. The problem

Customer requests arrived in three places that could not see each other:

- **Slack** — shared channels with client teams
- **Gmail** — `support@myrealprofit.com`
- **Intercom** — the Messenger on the app, with Fin answering first

Nothing tied them together. A question asked in Slack at 2am was found the next
morning by whoever happened to scroll back. There was no single list of what was
outstanding, no record of what had been answered, and no way to tell whether a
request had been dropped.

The goal was one queue — **YouTrack project CS** — fed by all three, where a
reply written once reaches the customer wherever they asked.

---

## 2. What it is

Four moving parts, ~5,800 lines across 67 commits.

| Component | Runs on | Lines | Job |
|---|---|---|---|
| `slack-youtrack` | Cloudflare Worker | ~1,780 | Slack events → tickets; relays replies back into threads |
| `intercom-youtrack` | Cloudflare Worker | ~1,550 | Fin escalations → tickets; two-way replies |
| `youtrack-workflows/reply-relay` | YouTrack workflow | 93 | Tells both workers when a comment is added |
| `bot.py` | Operator's laptop (CLI) | 322 | Grants/revokes Amazon Selling Partner access from DB requests |

Gmail needs no code — YouTrack's native email channel handles it, with
customised FreeMarker notification templates.

---

## 3. How it works

```
     SLACK                    GMAIL                  INTERCOM
       │                        │                        │
  (night / 🎫)                  │                   Fin answers first
       │                        │                        │
       │                        │                 escalated only
       ▼                        ▼                        ▼
 slack-youtrack          YouTrack native          intercom-youtrack
   (Worker)               email channel               (Worker)
       │                        │                        │
       └────────────────────────┴────────────────────────┘
                                │
                      ┌─────────▼─────────┐
                      │  YouTrack  CS     │   one queue, one history
                      └─────────┬─────────┘
                                │  public comment
                      ┌─────────▼─────────┐
                      │ reply-relay rule  │   tells BOTH workers
                      └─────────┬─────────┘
                                │
              ownership decides which one acts
                                │
                    back to Slack / Intercom / email
```

### Ticket creation

**Slack.** The worker verifies the Slack signature, acks within 3 seconds, and
decides whether the message deserves a ticket. The decision is one config line,
`SLACK_MODE`:

| Mode | Fires when |
|---|---|
| `always` | every new thread |
| `emoji` | someone reacts 🎫 — works on threaded replies, so you can tag the message that states the problem |
| `night` | posted 20:00–08:00, when nobody is watching |
| `night+emoji` | both — **currently live** |
| `off` | files nothing, still logs every decision |

The ticket carries the whole thread, not just the trigger message: every reply,
real names resolved via `users.info`, UTC timestamps, and any attached files
copied into YouTrack.

**One thread is one ticket.** A claim-and-release key in KV means a second 🎫 in
the same thread cannot open a second ticket, and a failure releases the claim so
a retry can succeed.

**Intercom.** Fin answers first. Only conversations Fin hands off
(`escalated`, `routed_to_team`) become tickets — so the ticket arrives with a
**"Fin already tried"** list of the articles it cited, and an agent doesn't send
the customer the same links again.

Three guards stop history flooding in: a hard date floor (`INTERCOM_IGNORE_BEFORE`),
a max age, and a debounce so the ticket is written after the customer stops typing.

**Gmail.** YouTrack's own email channel. The work here was template surgery, not
code: subject threading so replies stay in one Gmail conversation, one sign-off
using the responder's first name, the JetBrains footer removed.

### Replies, in both directions

This was the hard half.

**Visibility is the switch.** A **public** YouTrack comment reaches the customer;
an **internal** one never leaves YouTrack. Agents already used that toggle for
email, so one habit covers all three channels. The workers **fail closed** —
anything they cannot positively confirm as public is treated as internal.

**Routing is by ownership, not by channel.** The YouTrack rule tells *both*
workers about every comment; each checks its own store, and the one that recorded
the ticket acts. This replaced routing on the `Channel` field, which broke the
moment Fin started answering in Slack — such a ticket is *Slack* to a human
reading it and *Intercom* to the machinery that created it, and one field cannot
carry both meanings.

**Echoes are prevented by two marks**, each written before the action it guards:
the Intercom part we create is marked `mirrored:`, and the YouTrack comment the
inbound mirror creates is marked `relayed:`. Without both, an answer loops
YouTrack → Intercom → YouTrack forever.

### Fin in Slack

The newest piece. Fin now answers in connected Slack channels, and a Slack
thread becomes an ordinary Intercom conversation — same webhooks, same
escalation states, same path into YouTrack. The whole chain is proven end to end:

```
question in #helpdesk-testing → Fin answers with cited articles
  → "No, I still need help" → Intercom escalation
  → intercom-youtrack → CS-265
```

Fin is scoped to **@mention only**. Left on its default it answers everything —
including "hi" — and an unanswered thread bills $0.99 as an assumed resolution.
Two casual messages cost $1.98 before that was changed.

---

## 4. What it runs on

| Platform | Used for | Notes |
|---|---|---|
| **Cloudflare Workers** | both integrations | free plan; `wrangler` to deploy |
| **Workers KV** | dedupe, thread↔ticket maps, caches | **1,000 writes/day** on free tier — this shaped the cron interval and the dedupe design |
| **Cron triggers** | Intercom safety sweep | every 5 min; every minute would spend 1,440 KV list ops/day on its own |
| **Workers Observability** | retained logs | without it, an overnight failure leaves no trace |
| **Slack Events API** | messages, reactions, files | HMAC-SHA256 signing, 3-second ack |
| **YouTrack Cloud REST** | issues, comments, attachments, users | project CS, internal id `0-18` |
| **YouTrack workflow API** | the reply-relay rule | `entities.Issue.onChange` |
| **YouTrack FreeMarker** | email templates | subject, body, footer |
| **Intercom API** | conversations, replies, admins | version 2.11 |
| **Intercom Fin** | first-line answers | Messenger + Slack |
| **Claude API** | AI ticket titles | built, switched off — see below |
| **PostgreSQL** | `bot.py` | Hetzner CloudNativePG via Cloudflare Access tunnel |

**32 configuration names** across the two workers. Secrets are never in the
repo: tokens go in with `wrangler secret put`, the workflow file keeps a
placeholder for the shared secret, and `bot.py` takes its password from
`~/.pgpass` rather than the environment.

---

## 5. How it was built

67 commits between 2026-06-10 and 2026-10-20 — **62 of them in September**, the
month the thing went from a prototype to a working desk.

The method, repeatedly: **change one thing, test it on real traffic, let the
result decide the next step.** Almost every design rule below exists because
something failed first.

### What the failures taught

**Guessing is more expensive than checking.** The visibility relay silently did
nothing on CS-255/257 because two unverified assumptions had been stacked — that
a workflow comment id matches a REST comment id, and what a public comment's
visibility looks like. The fix removed both assumptions rather than adding a
third guess.

**Test against what the system actually stored.** The echo guard compared the
body we sent with the body Intercom kept, using `===`. Intercom doesn't store a
reply verbatim — `cool` comes back `<p>cool</p>` — so the guard never matched and
sat inert. It would have duplicated every reply the moment the inbound mirror
started running.

**A fix in one place can break another.** Labelling Fin-in-Slack tickets as
`Channel = Slack` was correct for humans and broke the reply relay the same day,
because the relay routed on that field. That's what moved routing to ownership.

**Scope filters need a scope.** `always` mode filed a ticket for "hi" and
"thanks". The emoji and night triggers exist because of that, and the same
lesson applied again, unprompted, to Fin months later.

**Read the whole payload before concluding it isn't there.** The Slack thread
permalink was recorded as "impossible to reconstruct" — it had been sitting in a
note Fin posts at the top of every conversation. The earlier conclusion came
from looking only at custom attributes and the source object.

**Not every wish is grantable.** YouTrack accepts a comment `author` field,
returns 200, and silently discards it — unchanged at Project Admin and System
Admin, and JetBrains confirm there are no plans to support it. That feature is
parked as a documented no-op rather than quietly deleted, because the matching
half is sound if they ever ship it.

**And the same wish was grantable elsewhere.** Attribution works on the leg that
faces the customer: Intercom honours `admin_id`, so a reply written in YouTrack
reaches Slack under the author's own name and avatar — once that teammate
authenticates their own Slack account.

### Verification habit

Every behavioural change ships with checks run against real captured payloads,
not invented ones — 21 for the Slack-origin and permalink work, 26 for the
mirror and echo guards, 9 for the author fallback. They live in the scratchpad
rather than the repo, since the project has no test harness; the code they
validate is committed.

---

## 6. Where it stands

**Live and verified (20 items).** Slack ticket creation with five trigger modes
and a DST-safe night window; one-thread-one-ticket; full-thread descriptions;
attachments both directions; failure alerts; two-way replies on Slack and
Intercom; Gmail inbound with cleaned templates; Fin escalations; Fin in Slack;
replies carrying the real person's name; one history across all three places;
service-account swap; retained logs.

**On hold (2).**
- *YouTrack-side comment attribution* — proven unsupported; code kept as a no-op.
- *AI ticket titles* (`TITLE_AI`) — built and dry-run tested against real
  threads, off pending an `ANTHROPIC_API_KEY`. Titles only, never descriptions,
  because YouTrack won't let a helpdesk ticket be edited after creation and
  nobody should act on a sentence a model invented. Every failure falls back to
  the quoted title, so switching it on risks nothing.

**Accepted limits.** The ticket reporter is the token owner, not the customer.
Titles are immutable after creation. KV's free tier allows 1,000 writes/day. A
duplicate-file race is narrowed but not closed — Durable Objects would close it
and are paid-plan only; it produced one duplicate pair (CS-251/252) in live
traffic.

**Open config.** `CUSTOMER_CHANNEL_IDS` is empty, so night mode files from every
channel the bot is in. `SLACK_ALERT_CHANNEL` is empty, so filing failures post
into the customer's own thread. `INTERCOM_NOTE_PREFIX` still says
"TESTING HELPDESK". And the personal Slack link on the shared `support@` seat
should be revoked — it is the fallback identity for every unattributed reply.

---

## 7. Running it

```bash
# deploy either worker
cd slack-youtrack   && npm install && npx wrangler deploy
cd intercom-youtrack && npx wrangler deploy

# watch live
npx wrangler tail

# secrets (never committed)
npx wrangler secret put SLACK_BOT_TOKEN
npx wrangler secret put YOUTRACK_TOKEN
npx wrangler secret put YOUTRACK_WEBHOOK_SECRET   # same value in both workers AND the workflow

# master switch, per worker
ENABLED = "false"   in wrangler.toml, then deploy
```

The YouTrack workflow is pasted into Administration → Workflows by hand; the
repo copy keeps a placeholder where the shared secret goes.

`bot.py` needs the Cloudflare Access tunnel up first, and **only runs on the
operator's own machine** — nothing deployed can reach that tunnel:

```bash
cloudflared access tcp --hostname sql.myrealprofit.io --url localhost:15432
python bot.py <request_id> [<request_id> ...]
```
