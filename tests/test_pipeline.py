from pathlib import Path

import pytest
from pydantic import ValidationError

from zypher_agent.dry_run import DRY_FOOTER, build, press, run_dry_run, type_text
from zypher_agent.llm import LLMError
from zypher_agent.models import Footer
from zypher_agent.pipeline import ConfigError, Pipeline

SRC = Path(__file__).resolve().parents[1] / "src"


def status_by_company(run):
    return {run.repo.get_lead(cid).company_name: c["status"] for cid, c in run.repo.contacts.items()}


def test_dry_run_end_to_end():
    run, summary = run_dry_run(echo=False)
    assert (summary.researched, summary.disqualified, summary.drafted, summary.delivered) == (2, 2, 2, 2)
    assert summary.failed == []
    assert status_by_company(run) == {
        "Acme Freight Co": "approved", "Sunrise Family Clinic": "researched",  # draft rejected by the human: redrafted next run
    }  # the two rejected companies are gone from the database; only their websites are remembered
    assert run.repo.is_rejected("devcraft-demo.com") and run.repo.is_rejected("ghost-trading-demo.com")
    acme = run.repo.latest_message(next(i for i, c in run.repo.contacts.items() if "acme" in c["email"]))
    assert (acme.status, acme.version, acme.feedback) == ("approved", 2, "make it shorter")


def test_every_llm_call_is_logged_with_inputs_outputs_and_model():
    run, _ = run_dry_run(echo=False)
    calls = [e for e in run.repo.events if e["type"] == "llm_call"]
    assert {e["payload"]["task"] for e in calls} == {"research", "draft", "redraft"}
    assert all(e["payload"]["system"] and e["payload"]["user"] and e["payload"]["model"] == "mock" for e in calls)
    assert all(e["payload"]["output"] is not None and e["payload"]["cost_usd"] == 0.0 for e in calls)
    # 3 researched (Ghost never reaches the model); Acme drafted once + Sunrise twice (retry); 1 redraft
    assert len(calls) == 3 + 3 + 1


def test_guardrail_retry_fixed_the_clinic_draft_before_you_saw_it():
    run, _ = run_dry_run(echo=False)
    clinic_card = next(c for c in run.notifier.sent if "Sunrise" in c["text"] and c["buttons"])
    assert "Passed all checks" in clinic_card["text"] and "revolutionary" not in clinic_card["text"].lower()
    assert clinic_card["text"].count("Hi there,") == 1  # "Dr. Priya Nair" is not greeted as "Hi Dr,"


def test_injection_attempt_cannot_reach_a_draft():
    run, _ = run_dry_run(echo=False)
    assert run.repo.is_rejected("ghost-trading-demo.com") and not run.repo.company_exists("ghost-trading-demo.com")
    assert not any("ghost" in (m.body or "").lower() for m in run.repo.messages.values())  # never drafted
    assert any(e["type"] == "possible_prompt_injection" for e in run.repo.events)
    assert not any("ghost-trading" in c["user"] for c in run.llm.calls)  # the model never saw that page
    assert "500 happy clients" not in " ".join(c["text"] for c in run.notifier.sent)


def test_invented_evidence_is_dropped_before_you_see_it():
    run, _ = run_dry_run(echo=False)
    dropped = [e for e in run.repo.events if e["type"] == "research_hypotheses_dropped"]
    assert "not found" in dropped[0]["payload"]["dropped"][0]["reason"]
    clinic_card = next(c for c in run.notifier.sent if "Sunrise" in c["text"] and c["buttons"])
    assert "wait over an hour" not in clinic_card["text"]


def test_vendor_is_disqualified_without_drafting():
    run, _ = run_dry_run(echo=False)
    assert any(e["type"] == "disqualified" and e["payload"]["reason"].startswith("it_or_software_vendor") for e in run.repo.events)


def test_rerunning_does_not_duplicate_work():
    run = build(echo=False)
    run.pipeline.run(10)
    llm_calls = len(run.llm.calls)
    again = run.pipeline.run(10)
    assert (again.researched, again.drafted, again.delivered) == (0, 0, 0) and len(run.llm.calls) == llm_calls
    assert len(run.repo.messages) == 2 and len(run.notifier.sent) == 2


def test_limit_caps_leads_per_stage():
    run = build(echo=False)
    summary = run.pipeline.run(limit=1)
    assert summary.researched + summary.disqualified == 1


def test_placeholder_footer_stops_the_run_before_any_llm_call():
    run = build(echo=False)
    bad = DRY_FOOTER.model_copy(update={"opt_out_line": "TODO: opt-out line"})
    pipeline = Pipeline(run.repo, None, run.llm, run.pipeline.icp, bad, run.service)
    with pytest.raises(ConfigError, match="opt_out_line"):
        pipeline.run()
    assert run.llm.calls == []


def test_one_failing_lead_does_not_stop_the_others():
    run = build(echo=False)
    original = run.llm.responses["research"]

    def flaky(system, user):
        if "acme-freight-demo.com" in user:
            raise LLMError("model unavailable")
        return original(system, user)

    run.llm.responses["research"] = flaky
    summary = run.pipeline.run(10)
    assert len(summary.failed) == 1 and "Acme Freight" in summary.failed[0]
    assert summary.delivered == 1  # the clinic still got through
    assert status_by_company(run)["Acme Freight Co"] == "new"  # untouched: retried next run
    assert any(e["type"] == "pipeline_error" for e in run.repo.events)


def test_only_the_sender_package_can_send_email():
    """The platform rule: nothing sends outside zypher_agent/sender. This test fails if mail code leaks elsewhere."""
    forbidden = ("smtplib", "aiosmtplib", "sendmail", "sendgrid", "mailgun", "yagmail", "starttls", "send_message")
    allowed_dir = SRC / "zypher_agent" / "sender"
    offenders = [
        f"{p.relative_to(SRC)}: {word}"
        for p in SRC.rglob("*.py") if allowed_dir not in p.parents
        for word in forbidden if word in p.read_text(encoding="utf-8").lower()
    ]
    assert offenders == []


def test_approving_after_a_rejection_or_unknown_company_is_safe():
    run, _ = run_dry_run(echo=False)
    with pytest.raises(StopIteration):  # no live card for a company that was never drafted
        press(run, "Ghost Trading", "approve")
    type_text(run, "hello?")  # text with nothing pending is just answered with help
    assert run.notifier.sent[-1]["text"].startswith("Tap Suggest changes")


def test_footer_type_is_enforced():
    with pytest.raises(ValidationError):
        Footer(opt_out_line="x")


def test_opted_out_leads_are_skipped_at_every_stage():
    run = build(echo=False)
    run.repo.add_suppression("manual", domain="acme-freight-demo.com")
    summary = run.pipeline.run(10)
    acme = next(c for c in run.repo.contacts.values() if c["email"].endswith("@acme-freight-demo.com"))
    assert acme["status"] == "unsubscribed" and summary.suppressed == 1
    assert not any("acme-freight-demo.com" in str(c["user"]) for c in run.llm.calls)  # nothing spent on them


def test_dry_run_sends_only_the_approved_email_through_the_fake_server():
    from zypher_agent.dry_run import simulate_sending
    run, _ = run_dry_run(echo=False)
    summary, transport = simulate_sending(run)
    assert summary.sent == 1 and [e.to for e in transport.sent] == ["rahul@acme-freight-demo.com"]  # not the rejected clinic


def test_a_rejected_company_is_purged_and_never_researched_again():
    from zypher_agent.sources.base import SourcedCompany, import_sourced
    run = build(echo=False)
    run.pipeline.run(10)
    rejected = [e for e in run.repo.events if e["type"] == "disqualified"]
    assert len(rejected) == 2 and all(e["payload"]["purged"] and e["payload"]["domain"] for e in rejected)
    assert all(c["company_id"] in run.repo.companies for c in run.repo.contacts.values())  # nothing left dangling
    calls = len(run.llm.calls)
    r = import_sourced(run.repo, [SourcedCompany(
        name="DevCraft Studio", domain="devcraft-demo.com", website="https://devcraft-demo.com", source="apify_maps")])
    assert r.rejected_before == ["DevCraft Studio"] and r.imported == []
    run.pipeline.run(10)
    assert not run.repo.company_exists("devcraft-demo.com")
    assert not any("devcraft" in c["user"].lower() for c in run.llm.calls[calls:])  # never sent to the model again


# ---------- a website we cannot read is not a rejection ----------

def test_a_site_that_blocks_us_is_parked_for_a_human_not_rejected():
    from zypher_agent.models import CompanyIn, ContactIn
    run = build(echo=False)
    cid = run.repo.upsert_lead(CompanyIn(name="Blocked LLC", domain="blocked-site.ae"), ContactIn(phone="+971 4"))
    summary = run.pipeline.run(10)
    assert run.repo.get_lead(cid).status == "nurture" and run.repo.company_exists("blocked-site.ae")
    assert not run.repo.is_rejected("blocked-site.ae") and summary.unreadable == 1
    event = next(e for e in run.repo.events if e["type"] == "website_unreadable")
    assert "site not found" in event["payload"]["errors"][0]
    assert not any(e["type"] == "disqualified" and e["payload"]["domain"] == "blocked-site.ae" for e in run.repo.events)


# ---------- progress lines while it works ----------

def test_progress_is_reported_for_every_company_as_it_goes():
    run = build(echo=False)
    lines: list[str] = []
    run.pipeline.progress = lines.append
    run.pipeline.run(10)
    text = "\n".join(lines)
    for name in ("Acme Freight Co", "Sunrise Family Clinic", "DevCraft Studio", "Ghost Trading LLC"):
        assert f"Researching {name}" in text, name
    assert "rejected (it_or_software_vendor)" in text and "ready to draft" in text
    assert "Writing the email for Acme Freight Co" in text and "drafted" in text
    assert "card(s) sent to Telegram" in text


def test_progress_is_silent_by_default():
    run = build(echo=False)
    run.pipeline.run(10)  # no progress callback set: nothing printed, nothing breaks
