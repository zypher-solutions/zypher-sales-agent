"""Orchestration. Each stage picks up leads by status, so a crash or a re-run never repeats work:

    new  --research-->  researched | disqualified
    researched  --draft-->  drafted
    drafted  --deliver-->  awaiting_approval          (then the human decides in Telegram)

A failure on one lead is logged and skipped; it never stops the others, and the lead keeps
its status so the next run retries it.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from zypher_agent.approval import ApprovalService
from zypher_agent.config import Icp, sequence_step
from zypher_agent.db import Repository
from zypher_agent.drafting.email import evidence_for, generate
from zypher_agent.guardrails.checks import footer_problems
from zypher_agent.leads import has_mail_server, rank_contact_emails
from zypher_agent.llm import LLM
from zypher_agent.models import Footer, Lead, NewMessage
from zypher_agent.research.agent import research_lead
from zypher_agent.research.fetcher import Fetcher
from zypher_agent.sender.policy import business_days_since


class ConfigError(Exception):
    pass


@dataclass
class RunSummary:
    researched: int = 0
    disqualified: int = 0
    drafted: int = 0
    delivered: int = 0
    suppressed: int = 0
    no_email: int = 0
    followups: int = 0  # follow-up drafts sent to you for approval
    completed: int = 0  # sequences that reached their end
    failed: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(
        self, repo: Repository, fetcher: Fetcher, llm: LLM, icp: Icp, footer: Footer, approvals: ApprovalService,
        email_ok: Callable[[str], bool] = has_mail_server,
    ) -> None:
        self.repo, self.fetcher, self.llm, self.icp, self.footer, self.approvals = (
            repo, fetcher, llm, icp, footer, approvals,
        )
        self.email_ok = email_ok

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
                if self._suppressed(lead, summary):
                    continue
                self._guard(summary, lead, stage)
        self.run_followups(limit, summary=summary)
        return summary

    def run_followups(self, limit: int = 5, now: datetime | None = None, summary: RunSummary | None = None) -> RunSummary:
        """Draft the next email for every lead whose wait is over. Each one goes to you for approval, like the first.

        A lead is a candidate only while its status is 'in_sequence': that means at least one email has been sent,
        nobody has replied, bounced or opted out, and no follow-up is already waiting on a card.
        """
        summary = summary or RunSummary()
        now = now or datetime.now(UTC)
        drafted = 0
        for lead, last_step, last_sent_at in self.repo.sequence_candidates():
            if drafted >= limit:
                break
            if self._suppressed(lead, summary):
                continue

            def followup(lead: Lead, summary: RunSummary, last_step=last_step, last_sent_at=last_sent_at) -> None:
                nonlocal drafted
                drafted += self._followup(lead, summary, last_step, last_sent_at, now)

            self._guard(summary, lead, followup)
        return summary

    def _followup(self, lead: Lead, summary: RunSummary, last_step: int, last_sent_at: datetime, now: datetime) -> int:
        spec = sequence_step(self.icp, last_step + 1)
        if spec is None:  # the last email of the sequence has gone out: stop for good
            self.repo.set_lead_status(lead.contact_id, "nurture")
            self.repo.log_event("sequence_complete", contact_id=lead.contact_id, actor="system")
            summary.completed += 1
            return 0
        waited = business_days_since(last_sent_at, now, lead.country)
        if waited is None or waited < spec.delay_business_days:
            return 0
        if self.repo.has_event_since(lead.contact_id, "out_of_office", now - timedelta(days=7)):
            return 0  # they are away: wait
        report = self.repo.latest_research(lead.company_id)
        if report is None:
            return 0
        sent = [(m.step, m.subject, m.body) for m in self.repo.sent_messages(lead.contact_id)]
        result = generate(lead, report, self.icp, self.llm, self.footer, step=spec.step, sent=sent)
        msg = self.repo.save_message(NewMessage(
            contact_id=lead.contact_id, subject=result.draft.subject, body=result.text,
            evidence=evidence_for(result, report), step=spec.step,
        ))
        self.approvals.deliver(msg.id)
        summary.followups += 1
        return 1

    def _suppressed(self, lead: Lead, summary: RunSummary) -> bool:
        """Someone can opt out after import. Check at every stage, before spending anything on them."""
        if not self.repo.is_suppressed(lead.email, lead.domain):
            return False
        self.repo.set_lead_status(lead.contact_id, "unsubscribed")
        self.repo.log_event("skipped_suppressed", contact_id=lead.contact_id, actor="system")
        summary.suppressed += 1
        return True

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
        elif lead.email is None and not self._adopt_email(lead, report.public_emails):
            # Sourced companies arrive without an email. No usable public address: a human can still
            # call or use LinkedIn (phone is on the lead), but we never guess an address.
            self.repo.set_lead_status(lead.contact_id, "nurture")
            self.repo.log_event("no_usable_email", contact_id=lead.contact_id, payload={"found": report.public_emails})
            summary.no_email += 1
        else:
            self.repo.set_lead_status(lead.contact_id, "researched")
            summary.researched += 1

    def _adopt_email(self, lead: Lead, found: list[str]) -> str | None:
        """Use the best public business address from their own site that can receive mail."""
        for email in rank_contact_emails(found, lead.domain):
            if self.repo.is_suppressed(email, lead.domain) or not self.email_ok(email):
                continue
            self.repo.set_contact_email(lead.contact_id, email)
            self.repo.log_event("email_found", contact_id=lead.contact_id, payload={"email": email, "from": "website"})
            return email
        return None

    def _draft(self, lead: Lead, summary: RunSummary) -> None:
        if self.repo.first_message_header(lead.contact_id) is not None:  # already emailed: not a first touch
            self.repo.set_lead_status(lead.contact_id, "in_sequence")
            return
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
