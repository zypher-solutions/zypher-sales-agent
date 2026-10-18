"""Compare LLM models on OUR tasks: research a lead's site, then draft the first-touch email.

    python scripts/eval_models.py leads.csv --models openai/gpt-oss-120b,qwen/qwen3.8-27b [--limit 10]

Nothing is written to the database or sent anywhere: leads go to an in-memory repo. Each site is
fetched once and shown to every model, so differences come from the model, not the pages.
Reports, per model: how often drafts pass the guardrails (first try and after retries), how many
verified hypotheses research produced, word count, tokens and time. It also writes the drafts to
eval_out/ with the model names hidden (A/B/C) so you can rate them blind before reading the key.
Cost: free-tier models only use your daily token allowance.
"""
import argparse
import random
import sys
import time
from pathlib import Path

from zypher_agent.config import Settings, load_icp
from zypher_agent.db import InMemoryRepo
from zypher_agent.drafting.email import build_footer, generate
from zypher_agent.guardrails.checks import footer_problems
from zypher_agent.leads import import_rows, parse_csv
from zypher_agent.llm_factory import build_llm
from zypher_agent.research.agent import research_lead
from zypher_agent.research.fetcher import HttpFetcher, SiteFetch

OUT_DIR = Path(__file__).resolve().parents[1] / "eval_out"


class CachingFetcher:
    """Fetch each site once so every model sees identical pages."""

    def __init__(self) -> None:
        self.inner, self.cache = HttpFetcher(), {}

    def fetch_site(self, website: str) -> SiteFetch:
        if website not in self.cache:
            self.cache[website] = self.inner.fetch_site(website)
        return self.cache[website]


def run_model(model: str, leads_csv: Path, limit: int, settings: Settings, fetcher: CachingFetcher):
    icp = load_icp(settings.icp_path)
    footer = build_footer(settings, icp)
    repo = InMemoryRepo()
    parsed = parse_csv(leads_csv)
    import_rows(repo, parsed.rows[:limit])
    llm = build_llm(settings.model_copy(update={"llm_models": model}), repo)
    stats = {"leads": 0, "research_ok": 0, "hyps": 0, "drafted": 0, "first_try": 0, "final_pass": 0,
             "attempts": 0, "words": 0, "errors": []}
    drafts: list[tuple[str, str, bool]] = []  # (company, email text, passed)
    start = time.monotonic()
    for lead in repo.leads_with_status("new", limit):
        stats["leads"] += 1
        try:
            report = research_lead(lead, fetcher=fetcher, llm=llm, icp=icp, repo=repo)
        except Exception as e:  # noqa: BLE001 - an eval must keep going and count failures
            stats["errors"].append(f"{lead.company_name} research: {type(e).__name__}: {e}"[:200])
            continue
        if report.disqualify_reason or not report.hypotheses:
            continue
        stats["research_ok"] += 1
        stats["hyps"] += len(report.hypotheses)
        try:
            result = generate(lead, report, icp, llm, footer)
        except Exception as e:  # noqa: BLE001
            stats["errors"].append(f"{lead.company_name} draft: {type(e).__name__}: {e}"[:200])
            continue
        stats["drafted"] += 1
        stats["attempts"] += result.attempts
        stats["first_try"] += result.check.passed and result.attempts == 1
        stats["final_pass"] += result.check.passed
        stats["words"] += len(result.draft.body.split())
        drafts.append((lead.company_name, result.text, result.check.passed))
    calls = [e["payload"] for e in repo.events if e["type"] == "llm_call"]
    stats["tokens_in"] = sum(c["input_tokens"] for c in calls)
    stats["tokens_out"] = sum(c["output_tokens"] for c in calls)
    stats["calls"] = len(calls)
    stats["seconds"] = round(time.monotonic() - start)
    return stats, drafts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", type=Path)
    ap.add_argument("--models", required=True, help="comma-separated model names to compare")
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()
    settings = Settings()
    if settings.llm_api_key is None:
        sys.exit("Set LLM_API_KEY (or GROQ_API_KEY) in .env first.")
    problems = footer_problems(build_footer(settings, load_icp(settings.icp_path)))
    if problems:
        sys.exit("Fix these in .env first, or every draft will fail the guardrails: " + "; ".join(problems))

    fetcher, results = CachingFetcher(), {}
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        print(f"== {model}", flush=True)
        results[model] = run_model(model, args.csv, args.limit, settings, fetcher)

    print(f"\n{'model':32} {'drafted':>8} {'1st-try':>8} {'final':>6} {'hyps/lead':>9} {'words':>6} "
          f"{'tok in':>8} {'tok out':>8} {'secs':>5} {'errs':>4}")
    for model, (s, _) in results.items():
        d = max(s["drafted"], 1)
        print(f"{model:32} {s['drafted']:>3}/{s['leads']:<4} {s['first_try']:>8} {s['final_pass']:>6} "
              f"{s['hyps'] / max(s['research_ok'], 1):>9.1f} {s['words'] / d:>6.0f} "
              f"{s['tokens_in']:>8} {s['tokens_out']:>8} {s['seconds']:>5} {len(s['errors']):>4}")
        for err in s["errors"][:3]:
            print("   ", err)

    labels = [chr(65 + i) for i in range(len(results))]
    random.shuffle(labels)
    key = dict(zip(results, labels, strict=True))
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"blind_{int(time.time())}.md"
    lines = ["# Blind review: rate each email 1-5 (specific? plain? would you send it?)\n"]
    for company in dict.fromkeys(c for _, d in results.values() for c, _, _ in d):
        lines.append(f"\n## {company}\n")
        for model, (_, drafts) in sorted(results.items(), key=lambda kv: key[kv[0]]):
            for c, text, passed in drafts:
                if c == company:
                    lines.append(f"### Model {key[model]} (guardrails: {'pass' if passed else 'FAIL'})\n\n{text}\n")
    lines.append("\n---\nKey (read AFTER rating): " + ", ".join(f"{v} = {k}" for k, v in sorted(key.items(), key=lambda kv: kv[1])))
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nBlind drafts: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
