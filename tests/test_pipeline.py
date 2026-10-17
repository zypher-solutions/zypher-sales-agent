from pathlib import Path

import pytest

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
        "Acme Freight Co": "approved", "Sunrise Family Clinic": "disqualified",  # rejected by the human
        "DevCraft Studio": "disqualified", "Ghost Trading LLC": "disqualified",
    }
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
    ghost_id = next(i for i, c in run.repo.contacts.items() if "ghost" in c["email"])
    assert run.repo.latest_message(ghost_id) is None  # never drafted
    assert run.repo.contacts[ghost_id]["status"] == "disqualified"
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


def test_nothing_in_phase_1_can_send_email():
    forbidden = ("smtplib", "aiosmtplib", "sendmail", "sendgrid", "mailgun", "yagmail", "smtp_host", "starttls")
    offenders = [
        f"{p.relative_to(SRC)}: {word}"
        for p in SRC.rglob("*.py") for word in forbidden if word in p.read_text(encoding="utf-8").lower()
    ]
    assert offenders == []
    assert not hasattr(__import__("zypher_agent.config", fromlist=["Settings"]).Settings(_env_file=None), "smtp_host")


def test_approving_after_a_rejection_or_unknown_company_is_safe():
    run, _ = run_dry_run(echo=False)
    with pytest.raises(StopIteration):  # no live card for a company that was never drafted
        press(run, "Ghost Trading", "approve")
    type_text(run, "hello?")  # text with nothing pending is just answered with help
    assert run.notifier.sent[-1]["text"].startswith("Tap Suggest changes")


def test_footer_type_is_enforced():
    with pytest.raises(Exception):
        Footer(opt_out_line="x")
