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
        repo, llm, icp, footer, tg, settings.telegram_allowed_user_ids, settings.telegram_approval_chat_id
    )
    site = settings.sender_website.removeprefix("https://").removeprefix("http://")
    agent = f"{icp.company['name']} research bot" + (f" (+https://{site})" if site else "")
    return Pipeline(repo, HttpFetcher(user_agent=agent), llm, icp, footer, service), service, tg, repo


def _source(settings: Settings, args) -> int:
    from zypher_agent.db import PostgresRepo
    from zypher_agent.sources.apify_maps import ApifyMapsSource, SourceError, estimate_cost_usd
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


def _print_summary(s) -> None:
    print(
        f"\nResearched: {s.researched} | disqualified: {s.disqualified} | drafted: {s.drafted} | "
        f"sent to Telegram for approval: {s.delivered} | no usable email: {s.no_email} | "
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
        print(f"\nLLM calls: {sum(e['type'] == 'llm_call' for e in dry.repo.events)} (mocked, cost ${cost:.2f}). Emails sent: 0 (there is no sending code in Phase 1).")
        return 0

    settings = Settings()
    if args.cmd == "source":
        return _source(settings, args)
    pipeline, service, tg, repo = _live(settings)
    if args.cmd == "run":
        from zypher_agent.pipeline import ConfigError

        try:
            _print_summary(pipeline.run(args.limit))
        except ConfigError as e:
            sys.exit(str(e))
        print("Now run `python -m zypher_agent bot` to handle your button taps.")
    else:
        from zypher_agent.integrations.telegram import poll_forever

        print("Listening for Telegram taps. Ctrl+C to stop.")
        try:
            poll_forever(tg, service)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
