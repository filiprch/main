# Helpdesk — feature status

**Updated:** 2026-09-25 · **Owner:** Filip Seidel

> **Current main focus: Fin (Intercom) in Slack.** The chain is proven end to
> end — a question in #helpdesk-testing, Fin's answer, "No, I still need help",
> and CS-265. What is open is *when Fin should speak* and *how it steps back*.
> See "Fin in Slack — how it should behave" in `helpdesk-webhooks.md`.

One page saying what works, what is parked and what is left. Detail for every
line lives in `helpdesk-webhooks.md`; this is the index.

---

## Done — live and verified

| # | Feature | Verified by |
|---|---------|-------------|
| 1 | **Slack → YouTrack ticket creation** | CS-158 and many since |
| 2 | **Trigger modes** — `always` / `emoji` / `night` / `night+emoji` / `off`, switched by one line in `wrangler.toml` | live on `night+emoji` |
| 3 | **Night window** — 20:00–08:00, DST-safe when set to an IANA zone | tested across midnight and both boundaries |
| 4 | **One thread = one ticket** — claim-and-release, so a second 🎫 in the same thread cannot open a second ticket | reproduced the old double-ticket, then fixed |
| 5 | **Full-thread descriptions** — every message, real names resolved, timestamps in UTC | CS-233 onward |
| 6 | **Slack attachments → YouTrack** | needed `files:read` on the **Bot** token, not the User token |
| 7 | **Later Slack replies become YouTrack comments** — tickets stop going stale | live |
| 8 | **Failure alerts** — a ticket that cannot be filed is shouted about instead of vanishing | tested with a bad token |
| 9 | **Two-way replies, Slack** — a **public** YouTrack comment lands in the Slack thread; internal comments never leave YouTrack | CS-255/257, fail-closed |
| 10 | **Attachments YouTrack → Slack** | via `files.getUploadURLExternal` |
| 11 | **YouTrack markup stripped** from relayed text — no stray `![](image2.png){width=70%}` | live |
| 12 | **Intercom → YouTrack** for Fin escalations, with debounce, max-age and a hard date floor | live since 07 Sep |
| 13 | **Two-way replies, Intercom** — public comment → visible reply, internal → nothing at all | verified both ways |
| 14 | **Gmail inbound** via YouTrack's native email channel | CS-233 |
| 15 | **Email templates** — subject threads into one Gmail conversation, one sign-off using the responder's first name, no JetBrains footer, no reply delimiter | live |
| 16 | **Service account** — workers run as `Support - Agent`, not a personal admin token | swapped and deployed |
| 17 | **Secrets audit** — nothing sensitive in the repo; the workflow file keeps a placeholder | audited |
| 18 | **Retained logs** on both workers | Cloudflare Observability tab |
| 19 | **Replies reach the customer under the real person's name** — a YouTrack comment relayed via Intercom arrives in Slack as *Filip Seidel*, not the service account | Verified 2026-09-25. Needs a per-person Intercom seat plus that teammate authenticating their own Slack. |
| 20 | **One history in all three places** — YouTrack ↔ Intercom ↔ Slack, including replies typed in the Intercom inbox, with echo guards in both directions | Verified 2026-09-25 on CS-276 |

---

## On hold

| Feature | State | Why it is parked |
|---------|-------|------------------|
| **Comment author attribution — inside YouTrack** (who a relayed Slack message is credited to on the ticket). The customer-facing half is **solved**, see Done #19. | Built, matching works, last step impossible | YouTrack accepts `author: { id }` with 200 and discards it. Not a permission — unchanged at System Admin. JetBrains: "no plans to make the same mechanism for comments." Only route is one permanent token per person in the worker; deferred on security grounds. Code left in place as a no-op. |
| **AI-written titles** (`TITLE_AI`) | Built and dry-run tested against real threads, switched off | Needs `ANTHROPIC_API_KEY` as a worker secret. Falls back to today's quoted title on any failure, so turning it on risks nothing. |

---

## To do — configuration and cleanup

These are settings and admin, not code. Each is small.

| # | Item | Where | Why it matters |
|---|------|-------|----------------|
| 1 | `CUSTOMER_CHANNEL_IDS` is **empty** | `slack-youtrack/wrangler.toml` | Night mode currently files from **every channel the bot is in**, not just customer ones. |
| 2 | `INTERCOM_NOTE_PREFIX` still `"TESTING HELPDESK"` | `intercom-youtrack/wrangler.toml` | Internal-only, so no customer sees it — but it is no longer true. |
| 3 | Strip **Project Admin** and **System Admin** from `Support - Agent` | YouTrack | Granted only to test author attribution. Broadest rights in the system, on a token held by a worker. |
| 4 | `SLACK_ALERT_CHANNEL` is **empty** | `slack-youtrack/wrangler.toml` | Filing failures currently post into the customer's own thread. |
| 5 | Lisa and Angelina need **agent** status | YouTrack | With the warning that their comments then default to **public**, and their agent signatures should be cleared. |
| 6 | Revoke Filip's personal YouTrack token | YouTrack | Superseded by the service account. |
| 7 | Give other projects their own **From** address | YouTrack | The global sender makes support@ archive every project's notifications. |
| 8 | Delete the test tickets in CS | YouTrack | Housekeeping. |

---

## To do — Fin in Slack (current focus)

| # | Item | Kind | Note |
|---|------|------|------|
| 1 | **Switch Fin to @mention-only** | Intercom Workflow | Gates the whole trial. Fin currently answers every message, including "hi" — which bills $0.99 if nobody closes the thread out. |
| 2 | **Test the disengage gesture** | test | Does an MRP reply *in the Slack thread* stop Fin, or must it come from the Intercom inbox? Unresolved and important. |
| 3 | **Revoke the personal Slack link on the shared `support@` seat** | Intercom/Slack | **Decided: no new Intercom seats.** So that seat is the fallback for nearly every reply, and a personal Slack link on it puts one colleague's face on everyone's words. Teammates who already have their own seat (Lisa) are unaffected by the revoke. Attribution for everyone else is the name prefix in the message body. Previously: proven to work Cannot be done centrally — each person clicks it, and needs *Can manage workspace data*. Their YouTrack and Intercom emails must match. Do **not** authenticate the shared seat against one person's Slack: every colleague's reply would then show as them. |
| 4 | **Write the pinned channel guidance** | copy | Once the trigger is settled. |
| 5 | **Deploy the SLACK-origin labelling fix** | deploy | Committed; `npx wrangler deploy` in `intercom-youtrack/`. |

## To do — build

| Feature | Size | Note |
|---------|------|------|
| **Close the duplicate-ticket race** | medium | CS-251/252 proved it in production. More urgent now: Fin adds a second source of Intercom conversations on the path that already duplicated. |
| **Intercom reply attachments** | small | Intercom takes files by public URL; whether YouTrack's signed links qualify is untested. |
| **Intercom escalation routing** | small | Escalated conversations sit unassigned with `sla_applied: null`. An assignment Workflow would fix ownership *and* give a real webhook to replace state polling. |
| **Wrangler v3 → v4** | small | Both workers pinned to v3. |

---

## Known limitations — accepted, not bugs

- **The ticket reporter is the token owner**, not the customer. Same root cause as the attribution finding above.
- **Titles are immutable** after creation under YouTrack helpdesk policy. This is why AI touches the title only, before creation, and never the description.
- **KV free tier allows 1,000 writes/day.** It shaped the cron interval and the dedupe design; heavy traffic would need the paid plan.
- **Duplicate-file race is narrowed, not closed.** KV is eventually consistent; Durable Objects would close it and are paid-plan only. Seen once.
- **Unexplained:** why last week's Intercom escalations produced no tickets. Worth a look if it recurs.
