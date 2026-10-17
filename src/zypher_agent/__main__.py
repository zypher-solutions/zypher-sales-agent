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
    from zypher_agent.llm import ClaudeLLM
    from zypher_agent.pipeline import Pipeline
    from zypher_agent.research.fetcher import HttpFetcher

    _require(settings, "database_url", "anthropic_api_key", "telegram_bot_token", "telegram_allowed_user_ids")
    icp, footer = load_icp(settings.icp_path), build_footer(settings, load_icp(settings.icp_path))
    repo = PostgresRepo(settings.database_url.get_secret_value())
    llm = ClaudeLLM(settings, repo)
    tg = TelegramClient(settings.telegram_bot_token.get_secret_value())
    service = ApprovalService(repo, llm, icp, footer, tg, settings.telegram_allowed_user_ids)
    return Pipeline(repo, HttpFetcher(), llm, icp, footer, service), service, tg, repo


def _print_summary(s) -> None:
    print(
        f"\nResearched: {s.researched} | disqualified: {s.disqualified} | drafted: {s.drafted} | "
        f"sent to Telegram for approval: {s.delivered} | failed: {len(s.failed)}"
    )
    for f in s.failed:
        print(f"  FAILED {f}")


def main(argv: list[str] | None = None) -> int:
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs full URLs, and Telegram's contain the bot token
    ap = argparse.ArgumentParser(prog="zypher_agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="research and draft new leads, send them for approval")
    run.add_argument("--dry-run", action="store_true", help="fake websites, mocked LLM, console instead of Telegram")
    run.add_argument("--limit", type=int, default=5, help="max leads per stage (default 5)")
    sub.add_parser("bot", help="listen for Telegram button taps and feedback")
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
