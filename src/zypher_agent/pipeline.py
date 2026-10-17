"""Orchestration. Each stage picks up leads by status, so a crash or a re-run never repeats work:

    new  --research-->  researched | disqualified
    researched  --draft-->  drafted
    drafted  --deliver-->  awaiting_approval          (then the human decides in Telegram)

A failure on one lead is logged and skipped; it never stops the others, and the lead keeps
its status so the next run retries it.
"""
from dataclasses import dataclass, field
from typing import Callable

from zypher_agent.approval import ApprovalService
from zypher_agent.config import Icp
from zypher_agent.db import Repository
from zypher_agent.drafting.email import evidence_for, generate
from zypher_agent.guardrails.checks import footer_problems
from zypher_agent.llm import LLM
from zypher_agent.models import Footer, Lead, NewMessage
from zypher_agent.research.agent import research_lead
from zypher_agent.research.fetcher import Fetcher


class ConfigError(Exception):
    pass


@dataclass
class RunSummary:
    researched: int = 0
    disqualified: int = 0
    drafted: int = 0
    delivered: int = 0
    failed: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(
        self, repo: Repository, fetcher: Fetcher, llm: LLM, icp: Icp, footer: Footer, approvals: ApprovalService
    ) -> None:
        self.repo, self.fetcher, self.llm, self.icp, self.footer, self.approvals = (
            repo, fetcher, llm, icp, footer, approvals,
        )

    def run(self, limit: int = 5) -> RunSummary:
        # Fail before spending anything: a draft with a placeholder footer could never be approved.
        problems = footer_problems(self.footer)
        if problems:
            raise ConfigError(
                "Email footer is not ready: " + "; ".join(problems)
                + ". Set OPT_OUT_LINE, SENDER_EMAIL and SENDER_POSTAL_ADDRESS in .env."
            )
        summary = RunSummary()
        for status, stage in (("new", self._research), ("researched", self._draft), ("drafted", self._deliver)):
            for lead in self.repo.leads_with_status(status, limit):
                self._guard(summary, lead, stage)
        return summary

    def _guard(self, summary: RunSummary, lead: Lead, stage: Callable[[Lead, RunSummary], None]) -> None:
        try:
            stage(lead, summary)
        except Exception as e:
            summary.failed.append(f"{lead.company_name}: {type(e).__name__}: {e}")
            self.repo.log_event(
                "pipeline_error", contact_id=lead.contact_id,
                payload={"stage": stage.__name__, "error": f"{type(e).__name__}: {e}"},
            )

    def _research(self, lead: Lead, summary: RunSummary) -> None:
        report = research_lead(lead, fetcher=self.fetcher, llm=self.llm, icp=self.icp, repo=self.repo)
        self.repo.save_research(lead.company_id, report)
        if report.industry:
            self.repo.set_company_industry(lead.company_id, report.industry)
        if report.disqualify_reason:
            self.repo.set_lead_status(lead.contact_id, "disqualified")
            self.repo.log_event("disqualified", contact_id=lead.contact_id, payload={"reason": report.disqualify_reason})
            summary.disqualified += 1
        else:
            self.repo.set_lead_status(lead.contact_id, "researched")
            summary.researched += 1

    def _draft(self, lead: Lead, summary: RunSummary) -> None:
        report = self.repo.latest_research(lead.company_id)
        result = generate(lead, report, self.icp, self.llm, self.footer)
        self.repo.save_message(NewMessage(
            contact_id=lead.contact_id, subject=result.draft.subject, body=result.text,
            evidence=evidence_for(result, report),
        ))
        self.repo.set_lead_status(lead.contact_id, "drafted")
        summary.drafted += 1

    def _deliver(self, lead: Lead, summary: RunSummary) -> None:
        msg = self.repo.latest_message(lead.contact_id)
        if msg is None:
            raise LookupError("lead is 'drafted' but has no message")
        self.approvals.deliver(msg.id)
        summary.delivered += 1
