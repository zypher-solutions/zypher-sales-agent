"""Command line.

    python -m zypher_agent run --dry-run          whole pipeline, fake everything, no keys
    python -m zypher_agent run [--limit 5]        research + draft new leads, send cards to Telegram
    python -m zypher_agent bot                    listen for your button taps and feedback
"""
import argparse
import logging
import sys

from zypher_agent.config import Settings, load_icp


def _require(settings: Settings, *names: str) -> None:
    missing = [n.upper() for n in names if not getattr(settings, n)]
    if missing:
        sys.exit(f"Missing in .env: {', '.join(missing)}")


def _live(settings: Settings):
    from zypher_agent.approval import ApprovalService
    from zypher_agent.db import PostgresRepo
    from zypher_agent.drafting.email import build_footer
    from zypher_agent.integrations.telegram import TelegramClient
    from zypher_agent.llm_factory import build_llm
    from zypher_agent.pipeline import Pipeline
    from zypher_agent.research.fetcher import HttpFetcher

    llm_key = "anthropic_api_key" if settings.llm_provider == "anthropic" else "llm_api_key"
    _require(settings, "database_url", llm_key, "telegram_bot_token", "telegram_allowed_user_ids")
    icp, footer = load_icp(settings.icp_path), build_footer(settings, load_icp(settings.icp_path))
    repo = PostgresRepo(settings.database_url.get_secret_value(), settings.tenant_id)
    llm = build_llm(settings, repo)
    tg = TelegramClient(settings.telegram_bot_token.get_secret_value())
    service = ApprovalService(
        repo, llm, icp, footer, tg, settings.telegram_allowed_user_ids, settings.telegram_approval_chat_id,
        settings.max_open_cards,
    )
    site = settings.sender_website.removeprefix("https://").removeprefix("http://")
    agent = f"{icp.company['name']} research bot" + (f" (+https://{site})" if site else "")
    return Pipeline(repo, HttpFetcher(user_agent=agent), llm, icp, footer, service), service, tg, repo


def _source(settings: Settings, args) -> int:
    from zypher_agent.db import PostgresRepo
    from zypher_agent.sources.apify_maps import (
        ApifyMapsSource,
        SourceError,
        estimate_cost_usd,
    )
    from zypher_agent.sources.base import import_sourced

    _require(settings, "apify_token", *([] if args.preview else ["database_url"]))
    est = sum(estimate_cost_usd(args.max) for _ in args.query)
    print(
        f"Searching {len(args.query)} query(s) in {args.location}, up to {args.max} places each. "
        f"Estimated Apify cost: about ${est:.2f} (Apify also enforces a hard cap per run)."
    )
    source = ApifyMapsSource(settings.apify_token.get_secret_value())
    repo = None if args.preview else PostgresRepo(settings.database_url.get_secret_value(), settings.tenant_id)
    for query in args.query:
        try:
            found = source.search(query, args.location, args.max)
        except SourceError as e:
            print(f"\n[{query}] failed: {e}")
            continue
        print(f"\n[{query}] {len(found)} companies with their own website, {len(source.last_skipped)} skipped")
        for c in found:
            print(f"  {c.name[:40]:<40} {c.domain:<30} {c.industry or ''}")
        for name, why in source.last_skipped:
            print(f"  skipped {name[:40]:<40} ({why})")
        if repo is not None:
            r = import_sourced(repo, found)
            print(
                f"  -> imported {len(r.imported)}, already known {len(r.already_known)}, "
                f"on do-not-contact list {len(r.suppressed)}"
            )
    if repo is not None:
        print("\nNext: `python -m zypher_agent run --limit 5` researches them, finds a public email, drafts, "
              "and sends cards to Telegram.")
    return 0


MEANING = {
    "new": "found, not researched yet", "researched": "ready to draft", "drafted": "drafted, card not sent",
    "awaiting_approval": "WAITING FOR YOU in Telegram", "approved": "approved by you (nothing sent yet)",
    "nurture": "no public email: call or LinkedIn by hand", "disqualified": "not a fit",
    "unsubscribed": "opted out", "needs_changes": "waiting for your feedback text",
}


def _status(settings: Settings) -> int:
    from zypher_agent.db import PostgresRepo

    _require(settings, "database_url")
    repo = PostgresRepo(settings.database_url.get_secret_value(), settings.tenant_id)
    rows = repo.conn.execute(
        "select status, count(*) as n from contacts where tenant_id = %s group by status order by n desc",
        (settings.tenant_id,),
    ).fetchall()
    print("Leads by stage:")
    for r in rows:
        print(f"  {r['n']:>4}  {r['status']:<18} {MEANING.get(r['status'], '')}")
    if not rows:
        print("  none yet. Start with: zypher find \"freight forwarding company\" \"Dubai, United Arab Emirates\" 20")
    sup = repo.conn.execute("select count(*) as n from suppression_list where tenant_id = %s", (settings.tenant_id,)).fetchone()
    cost = repo.conn.execute(
        "select count(*) as n, coalesce(sum((payload->>'cost_usd')::float), 0) as usd from events "
        "where tenant_id = %s and type = 'llm_call'", (settings.tenant_id,),
    ).fetchone()
    print(f"\nDo-not-contact list: {sup['n']} | model calls so far: {cost['n']} (paid cost ${cost['usd']:.2f})")
    return 0


def _testmail(settings: Settings, to: str) -> int:
    """One real email, to a mailbox the owner controls, containing no lead data. Not subject to the lead rules."""
    from zypher_agent.leads import clean_email
    from zypher_agent.sender.transport import (
        OutgoingEmail,
        SendError,
        SMTPTransport,
        new_message_id,
    )

    address = clean_email(to)
    if address is None:
        sys.exit(f"{to!r} is not a valid email address.")
    body = (
        "This is a test from your sales agent. It checks that your mailbox can send and that the message is "
        "authenticated.\n\nIn Gmail: open this message, click the three dots, choose 'Show original', and look "
        "for SPF: PASS, DKIM: PASS and DMARC: PASS at the top.\n\nIf it landed in Spam, mark it 'Not spam'."
    )
    mail = OutgoingEmail(
        to=address, subject="test from your sales agent", body=body, message_id=new_message_id(settings.sender_email),
        from_addr=settings.sender_email, from_name=settings.sender_name,
    )
    try:
        SMTPTransport(settings).send(mail)
    except SendError as e:
        sys.exit(f"Test email failed: {e}")
    print(f"Test email sent to {address}. Check the inbox AND the spam folder, then 'Show original'.")
    return 0


def _inbox_processor(settings: Settings, repo, alert):
    from zypher_agent.llm import LLMError
    from zypher_agent.llm_factory import build_llm
    from zypher_agent.sender.inbox import IMAPMailbox, InboxProcessor

    try:
        llm = build_llm(settings, repo)
    except LLMError:  # no model configured: replies still reach you, just without a label
        class _NoLLM:
            model = "none"

            def call(self, **_kw):
                raise LLMError("no model configured")

        llm = _NoLLM()
    return InboxProcessor(repo, IMAPMailbox(settings), llm, alert)


def _stats(settings: Settings) -> int:
    from zypher_agent.db import PostgresRepo
    from zypher_agent.sender.stats import build_report, format_report

    _require(settings, "database_url")
    repo = PostgresRepo(settings.database_url.get_secret_value(), settings.tenant_id)
    print(format_report(build_report(repo)))
    print("\nMeetings booked are not tracked: the system never sees your calendar. Keep that count yourself.")
    return 0


def _inbox(settings: Settings) -> int:
    from zypher_agent.db import PostgresRepo

    _require(settings, "database_url", "mail_password")
    repo = PostgresRepo(settings.database_url.get_secret_value(), settings.tenant_id)
    r = _inbox_processor(settings, repo, lambda text: print("ALERT:", text)).run_once()
    if r.started:
        print("First run: starting from now. Mail already in the mailbox is never read.")
    else:
        print(f"Handled {r.processed} mail(s) about your emails: {r.replies} reply(ies), {r.bounces} bounce(s), "
              f"{r.auto_replies} auto-reply(ies). Skipped {r.ignored} unrelated mail(s): not stored, logged or shown.")
    return 0


def _send(settings: Settings, loop: bool) -> int:
    import time

    from zypher_agent.db import PostgresRepo
    from zypher_agent.drafting.email import build_footer
    from zypher_agent.integrations.telegram import TelegramClient
    from zypher_agent.sender.transport import SMTPTransport
    from zypher_agent.sender.worker import SendWorker

    _require(settings, "database_url", "telegram_bot_token", "telegram_allowed_user_ids")
    if not settings.sending_enabled:
        sys.exit("Sending is switched off. Set SENDING_ENABLED=true in .env when you are ready (run preflight first).")
    icp = load_icp(settings.icp_path)
    footer = build_footer(settings, icp)
    repo = PostgresRepo(settings.database_url.get_secret_value(), settings.tenant_id)
    tg = TelegramClient(settings.telegram_bot_token.get_secret_value())
    chat = settings.telegram_approval_chat_id or settings.telegram_allowed_user_ids[0]

    def alert(text: str) -> None:
        try:
            tg.send(chat, "🔔 " + text)
        except Exception as e:  # noqa: BLE001 - an alert that cannot be delivered must not stop the worker
            print(f"(could not send Telegram alert: {type(e).__name__})")

    worker = SendWorker(repo, SMTPTransport(settings), settings, footer, icp, alert=alert)
    inbox = _inbox_processor(settings, repo, alert)
    followups = None
    if loop:  # the long-running worker also writes follow-ups when they fall due; they still wait for your approval
        try:
            followups = _live(settings)[0]
        except SystemExit:
            print("(follow-up drafting is off: the model, database or Telegram settings are incomplete)")
    cycle = 0
    if settings.send_allowlist:
        print(f"TEST MODE: only these recipients can receive email: {settings.send_allowlist}")
    while True:
        stamp = time.strftime("%H:%M:%S")
        try:  # read bounces, replies and opt-outs BEFORE sending anything
            r = inbox.run_once()
            if r.processed:
                print(f"[{stamp}] inbox: {r.replies} reply(ies), {r.bounces} bounce(s), {r.auto_replies} auto-reply(ies)")
        except Exception as e:  # noqa: BLE001 - unreadable inbox means we cannot see opt-outs: do not send this cycle
            print(f"[{stamp}] cannot read the mailbox ({type(e).__name__}): skipping sending this cycle")
            if not loop:
                return 1
            time.sleep(60)
            continue
        if followups is not None and cycle % 30 == 0:  # about every 30 minutes
            try:
                fs = followups.run_followups(limit=5)
                if fs.followups or fs.completed:
                    print(f"[{stamp}] follow-ups drafted for your approval: {fs.followups} (sequences finished: {fs.completed})")
            except Exception as e:  # noqa: BLE001 - drafting trouble must not stop the sending worker
                print(f"[{stamp}] follow-up drafting failed ({type(e).__name__}); will retry")
        if cycle % 60 == 0:  # about hourly: advice only, it never pauses sending
            try:
                from zypher_agent.sender.stats import check_reply_rate

                check_reply_rate(repo, alert)
            except Exception as e:  # noqa: BLE001 - advice must never stop the sending worker
                print(f"[{stamp}] reply-rate check failed ({type(e).__name__})")
        cycle += 1
        s = worker.run_once()
        if s.paused:
            print(f"[{stamp}] sending is PAUSED: {s.paused}")
        else:
            print(f"[{stamp}] sent {s.sent} | waiting {s.deferred} | blocked {len(s.blocked)} | problems {len(s.problems)}")
            for b in s.blocked:
                print("   blocked:", b)
        if not loop:
            return 0
        try:
            time.sleep(60)
        except KeyboardInterrupt:
            print("Worker stopped.")
            return 0


def _preflight(settings: Settings) -> int:
    from zypher_agent.drafting.email import build_footer
    from zypher_agent.guardrails.checks import footer_problems
    from zypher_agent.sender.preflight import FAIL, run_preflight

    footer = build_footer(settings, load_icp(settings.icp_path))
    checks = run_preflight(settings, footer_problems(footer))
    marks = {"ok": "PASS", "warn": "WARN", "fail": "FAIL"}
    for c in checks:
        print(f"  {marks[c.status]}  {c.name:<14} {c.detail}")
    failed = [c for c in checks if c.status == FAIL]
    print("\nReady to send." if not failed else f"\nNot ready: fix the {len(failed)} FAIL item(s) above first.")
    return 1 if failed else 0


def _print_summary(s) -> None:
    print(
        f"\nResearched: {s.researched} | disqualified: {s.disqualified} | drafted: {s.drafted} | "
        f"sent to Telegram for approval: {s.delivered} | follow-ups drafted: {s.followups} | no usable email: {s.no_email} | "
        f"skipped (opted out): {s.suppressed} | failed: {len(s.failed)}"
    )
    for f in s.failed:
        print(f"  FAILED {f}")


def main(argv: list[str] | None = None) -> int:
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs full URLs, and Telegram's contain the bot token
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252 and choke on arrows and emoji
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="zypher_agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="research and draft new leads, send them for approval")
    run.add_argument("--dry-run", action="store_true", help="fake websites, mocked LLM, console instead of Telegram")
    run.add_argument("--limit", type=int, default=5, help="max leads per stage (default 5)")
    sub.add_parser("bot", help="listen for Telegram button taps and feedback")
    sub.add_parser("refresh", help="redraft every draft still awaiting approval with the current prompt, replace its card")
    sub.add_parser("status", help="show how many leads are at each stage and what needs your attention")
    sub.add_parser("send", help="one pass: send approved emails that the send policy allows right now")
    sub.add_parser("stats", help="emails sent, replies, bounces, opt-outs and what the numbers mean")
    sub.add_parser("inbox", help="read the mailbox once: bounces, replies and opt-outs about YOUR emails only")
    sub.add_parser("worker", help="keep sending approved emails as the policy allows (leave running; Ctrl+C stops)")
    tm = sub.add_parser("testmail", help="send ONE plain test email to an address you own, to check SPF/DKIM headers")
    tm.add_argument("--to", required=True, help="your own other mailbox, e.g. a Gmail address")
    sub.add_parser("preflight", help="check DNS records and mailbox logins before the first real send (sends nothing)")
    src = sub.add_parser("source", help="find companies on Google Maps (Apify) and add them as new leads")
    src.add_argument("--query", action="append", required=True, help='e.g. "logistics company" (repeat for more)')
    src.add_argument("--location", required=True, help='e.g. "Dubai, United Arab Emirates"')
    src.add_argument("--max", type=int, default=20, help="max places per query, 1-100 (default 20)")
    src.add_argument(
        "--preview", action="store_true", help="search and show results but write nothing (still uses Apify credit)"
    )
    args = ap.parse_args(argv)

    if args.cmd == "run" and args.dry_run:
        from zypher_agent.dry_run import run_dry_run

        dry, summary = run_dry_run()
        _print_summary(summary)
        statuses = {c["id"]: c["status"] for c in dry.repo.contacts.values()}
        print("\nFinal lead statuses:")
        for cid, status in statuses.items():
            print(f"  {dry.repo.get_lead(cid).company_name:<24} {status}")
        cost = sum(e["payload"]["cost_usd"] or 0 for e in dry.repo.events if e["type"] == "llm_call")
        print(f"\nLLM calls: {sum(e['type'] == 'llm_call' for e in dry.repo.events)} (mocked, cost ${cost:.2f}). Emails sent: 0 so far.")
        from zypher_agent.dry_run import simulate_sending

        summary, transport = simulate_sending(dry)
        print(
            f"\nSending step (fake mail server, Tuesday 10:00 in Dubai): sent {summary.sent}, "
            f"waiting {summary.deferred}, blocked {len(summary.blocked)}"
        )
        for out in transport.sent:
            print(f"  WOULD SEND to {out.to}: {out.subject}  (fake server: nothing really left this PC)")
        from zypher_agent.dry_run import simulate_followups

        print("\nFollow-ups, if nobody replies (simulated days later; each would need your approval):")
        for when, summary in simulate_followups(dry):
            print(f"  {when}: {summary}")
        return 0

    settings = Settings()
    if args.cmd == "source":
        return _source(settings, args)
    if args.cmd == "status":
        return _status(settings)
    if args.cmd == "preflight":
        return _preflight(settings)
    if args.cmd == "stats":
        return _stats(settings)
    if args.cmd == "inbox":
        return _inbox(settings)
    if args.cmd == "testmail":
        return _testmail(settings, args.to)
    if args.cmd in ("send", "worker"):
        return _send(settings, loop=args.cmd == "worker")
    pipeline, service, tg, repo = _live(settings)
    if args.cmd == "refresh":
        print(f"Refreshed {service.refresh_pending()} draft(s). New cards are in Telegram.")
        return 0
    if args.cmd == "run":
        from zypher_agent.pipeline import ConfigError

        try:
            _print_summary(pipeline.run(args.limit))
        except ConfigError as e:
            sys.exit(str(e))
        print("Now run `python -m zypher_agent bot` to handle your button taps.")
    else:
        from zypher_agent.integrations.telegram import poll_forever

        try:
            tg.set_commands()  # keep the "/" menu in Telegram in step with what the bot can do
        except Exception as e:  # noqa: BLE001 - a menu that fails to update must not stop the bot
            print(f"(could not update the Telegram command menu: {type(e).__name__})")
        print("Listening for Telegram taps. Ctrl+C to stop.")
        try:
            poll_forever(tg, service)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
