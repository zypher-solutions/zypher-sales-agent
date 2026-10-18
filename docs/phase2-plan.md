# Phase 2 plan: sending, limits, follow-ups, bounces

Status: APPROVED 2026-10-19. Built so far: steps 1 to 4 (policy, transport, preflight, worker, Telegram pause/resume/status, dry run, inbox handling with bounces, replies, opt-outs and automatic stop-loss). Not built yet: step 5 (follow-up scheduling). Verified live on 2026-10-19: preflight passes and a test email arrived with SPF, DKIM and DMARC all PASS (Gmail put it in Promotions, normal for a new sender). Sending stays OFF (`SENDING_ENABLED=false`) until preflight passes and the owner switches it on.

## Facts found on 2026-10-19 (DNS lookup of zypher-solutions.com)
- Mail is hosted at **GoDaddy** (`MX: mailstore1.secureserver.net`), not Google Workspace.
- SPF is `v=spf1 include:secureserver.net -all`: only GoDaddy may send for this domain. Good, and it means we must send through GoDaddy's own SMTP, never from another service.
- DMARC is `p=quarantine`: a message that fails authentication goes to spam.
- No DKIM key was found at the usual Google selector. GoDaddy may sign with another selector, so the preflight check must verify this with a real test message (headers show `dkim=pass` or not).
- Consequence: all limits and settings below follow GoDaddy's mail rules, which we must confirm (daily recipient limit, SMTP host and port, app password or normal password).

## Principles (unchanged from CLAUDE.md)
Nothing sends outside the sender module. Platform guardrails are not tenant-configurable: suppression, opt-out, caps, send windows, evidence-backed claims, stop-loss pauses. A human approves every email while autonomy level is 0.

## Modules (all under `src/zypher_agent/sender/`)
| Module | Job |
|---|---|
| `policy.py` | Pure function: given a message and the world, return ALLOW, DEFER(until, why) or BLOCK(why). No network. This is where tests concentrate |
| `transport.py` | `EmailTransport` interface; `SMTPTransport` (GoDaddy now, any provider later); `FakeTransport` for tests and dry run |
| `worker.py` | One pass: poll the inbox, schedule due follow-ups, send what policy allows. Runs every few minutes |
| `inbox.py` | IMAP reader on the sender mailbox: matches replies to our messages, detects bounces and out-of-office, handles opt-outs |
| `sequence.py` | Follow-up scheduling from the `sequences` table: which step is due, for whom |

## The send policy (checked in this order; first failure decides)
1. **Approved:** status is `approved` for exactly this version, and the approval event exists. Level 0 means every email, follow-ups included.
2. **Not paused:** the tenant's `sending_paused` flag is off (kill switch and automatic stop-loss).
3. **Suppression:** neither the address nor its domain is on the do-not-contact list (checked again at send time).
4. **Guardrails again:** the stored draft is re-checked with the current rules, plus footer present. A rule that tightened since approval blocks the send.
5. **One contact per company** and never re-send the same step (idempotent: a message can be claimed only once).
6. **Allowed days and hours:** the lead's local time. UAE Mon to Fri, Qatar Sun to Thu, India Mon to Fri, US Mon to Fri on Eastern time. Two windows: 09:00 to 11:30 and 13:00 to 16:00. Outside the window, DEFER to the next one.
7. **Daily cap with warmup ramp** (below). Follow-ups count toward the cap. Over the cap, DEFER to tomorrow.
8. **Spacing:** random 4 to 12 minutes between sends, so mail never leaves in a burst.

## Daily cap and warmup (main domain, so deliberately small)
| Days since first send | Max per day |
|---|---|
| 1 to 7 | 5 |
| 8 to 14 | 8 |
| 15 to 21 | 12 |
| 22 onward | 15 (hard ceiling 20 while using the main domain) |

The owner may set a lower cap in `.env`, never a higher one. Reaching 20 a day means it is time for a separate sending domain.

## Stop-loss (automatic, with a Telegram alert; only a human resumes)
- 2 or more hard bounces in the last 30 sends, or bounce rate above 2% over 100+ sends: pause.
- Any reply that looks like a spam complaint ("this is spam", "report you"): pause.
- Reply rate under 1% after 300 sends: alert only (targeting or offer is wrong).
- `/pause`, `/resume`, `/status` in Telegram.

## Bounces and replies (inbox.py, reads the sender mailbox over IMAP)
- **Hard bounce** (5.x.x delivery failure notice): mark message `bounced`, contact `bounced`, add the address to the do-not-contact list, count toward stop-loss.
- **Soft bounce** (4.x.x, mailbox full): retry once next day, then treat as hard.
- **Out-of-office** (auto-reply headers): pause that contact's sequence for 7 days.
- **Reply:** stop the sequence at once, save the reply, alert you in Telegram within a minute with the text. The classifier (structured output, stored with the reply) tags it using the existing `reply_class` values.
- **Clear opt-out** ("no thanks", "unsubscribe", "remove me", "stop"): suppress automatically and confirm to you. Anything that is not a clear negative goes to you, never auto-handled (platform rule).
- Drafting reply suggestions is Phase 3. Phase 2 detects, protects and alerts.

## Follow-ups (config-driven, from `docs/sales-playbook.md`)
| Step | Business days after previous | Angle |
|---|---|---|
| 1 | 0 | first touch (exists) |
| 2 | 3 | different observation, one new idea |
| 3 | 5 | ask who owns this at the company; allowed proof wording |
| 4 | 9 | breakup: reply 1 / 2 / 3 |

Each follow-up is drafted, checked and sent to you as a card like the first email. Steps 2 to 4 go out in the same thread (proper reply headers). A sequence stops when the contact replies, bounces, opts out, or is suppressed.

## Message headers and format
Plain text only. No tracking pixel, no links in email 1. `Message-ID` generated by us and stored (replies are matched on it). `List-Unsubscribe: <mailto:sender-address?subject=unsubscribe>` (mailto only, since we host no web page). `Reply-To` is the same address.

## Schema change (migration 004, mirrored in schema.sql)
- `messages`: `message_id_header`, `in_reply_to`, `send_after`, `send_attempts`, `last_error`, `sent_from`.
- `tenants`: `sending_paused`, `pause_reason`, `paused_at`.
- Start date of sending is derived from the first `sent` message, so the ramp needs no extra setting.
- Seed one campaign and its four `sequences` rows from config.

## Build order (tests first for policy, transport and bounce parsing)
1. Migration 004 and `policy.py` with tests for every rule above.
2. `transport.py` with `FakeTransport` and `SMTPTransport`, plus a `preflight` command: DNS (SPF, DKIM, DMARC, MX), SMTP login, IMAP login. Sends nothing.
3. `worker.py` and the `send` command; Telegram `/pause /resume /status`; dry run end to end.
4. `inbox.py`: bounce, out-of-office, reply and opt-out handling, stop-loss, alerts.
5. `sequence.py`: follow-up scheduling and step-aware drafting and guardrails.
6. Staged live rollout: (a) 3 emails to the owner's own other mailboxes (Gmail, Outlook) to check headers and placement; (b) 5 real leads at 5 a day; (c) review bounces and replies after a week before raising the cap.

## What we need from the owner
1. Confirm the mailbox and give its SMTP and IMAP login (put `MAIL_PASSWORD` in `.env` yourself; never paste it in chat). GoDaddy host names will be confirmed by `preflight`.
2. Confirm sending from `muhammedhasheem@zypher-solutions.com` on the main domain with the small caps above.
3. Approve that every email, follow-ups included, goes through your Telegram approval until we have 100 approved sends.
4. Decide where the worker runs: this PC for the first two weeks (it only sends while the PC is on and awake, inside business hours), then a small cloud server.

## Risks and honest limits
- **Main domain:** a deliverability problem here affects normal company email. Mitigated by tiny caps, pause rules and plain text; not removed. Move to a separate domain before volume grows.
- **GoDaddy shared mail servers** have mixed reputation and their own sending limits.
- **Free model speed** (about one lead a minute) is not a bottleneck at 5 to 15 emails a day.
- **PC must be on** during send windows until the worker moves to a server.
- **Verification:** we check that an address's domain can receive mail, but cannot prove the mailbox exists. Bounce detection is the safety net, which is why early volume is tiny.
