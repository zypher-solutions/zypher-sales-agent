"""Data access. The pipeline talks to the `Repository` protocol, never to SQL.

Two implementations: PostgresRepo (Supabase, the source of truth) and
InMemoryRepo (for --dry-run and tests, needs nothing installed or configured).
"""
import uuid
from typing import Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from zypher_agent.models import (
    CompanyIn, ContactIn, Hypothesis, Lead, Message, NewMessage, ResearchReport,
)

# Columns update_message may touch. Whitelisted because they become SQL identifiers.
MESSAGE_FIELDS = {
    "subject", "body", "evidence", "version", "status", "feedback",
    "telegram_chat_id", "telegram_message_id",
}


class Repository(Protocol):
    def upsert_lead(self, company: CompanyIn, contact: ContactIn) -> str: ...
    def is_suppressed(self, email: str | None, domain: str | None) -> bool: ...
    def add_suppression(self, reason: str, email: str | None = None, domain: str | None = None) -> None: ...
    def leads_with_status(self, status: str, limit: int) -> list[Lead]: ...
    def get_lead(self, contact_id: str) -> Lead: ...
    def set_lead_status(self, contact_id: str, status: str) -> None: ...
    def save_research(self, company_id: str, report: ResearchReport) -> str: ...
    def latest_research(self, company_id: str) -> ResearchReport | None: ...
    def save_message(self, msg: NewMessage) -> Message: ...
    def get_message(self, message_id: str) -> Message: ...
    def latest_message(self, contact_id: str) -> Message | None: ...
    def set_company_industry(self, company_id: str, industry: str) -> None: ...
    def update_message(self, message_id: str, **fields: object) -> Message: ...
    def message_needing_changes(self, chat_id: int) -> Message | None: ...
    def log_event(
        self, type: str, *, contact_id: str | None = None, message_id: str | None = None,
        actor: str = "agent", payload: dict | None = None,
    ) -> None: ...


def _check_fields(fields: dict) -> None:
    bad = set(fields) - MESSAGE_FIELDS
    if bad:
        raise ValueError(f"cannot update message fields: {sorted(bad)}")


class PostgresRepo:
    def __init__(self, dsn: str) -> None:
        # prepare_threshold=None: Supabase's pooler (transaction mode) rejects prepared statements.
        self.conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row, prepare_threshold=None)

    def upsert_lead(self, company: CompanyIn, contact: ContactIn) -> str:
        with self.conn.transaction():
            comp = self.conn.execute(
                """insert into companies (name, domain, website, country, industry, notes, source)
                   values (%s, %s, %s, %s, %s, %s, %s)
                   on conflict (domain) do update set
                     name = excluded.name,
                     website = coalesce(excluded.website, companies.website),
                     country = coalesce(excluded.country, companies.country),
                     industry = coalesce(excluded.industry, companies.industry),
                     notes = coalesce(excluded.notes, companies.notes)
                   returning id""",
                (company.name, company.domain, company.website, company.country,
                 company.industry, company.notes, company.source),
            ).fetchone()
            row = self.conn.execute(
                """insert into contacts (company_id, full_name, role, email, legacy_lead_id)
                   values (%s, %s, %s, %s, %s)
                   on conflict (company_id, email) do update set
                     full_name = coalesce(excluded.full_name, contacts.full_name),
                     role = coalesce(excluded.role, contacts.role),
                     legacy_lead_id = coalesce(excluded.legacy_lead_id, contacts.legacy_lead_id)
                   returning id""",
                (comp["id"], contact.full_name, contact.role, contact.email, contact.legacy_lead_id),
            ).fetchone()
        return str(row["id"])

    def is_suppressed(self, email: str | None, domain: str | None) -> bool:
        row = self.conn.execute(
            """select 1 from suppression_list
               where (email is not null and lower(email) = lower(%s))
                  or (domain is not null and lower(domain) = lower(%s)) limit 1""",
            (email, domain),
        ).fetchone()
        return row is not None

    def add_suppression(self, reason: str, email: str | None = None, domain: str | None = None) -> None:
        self.conn.execute(
            "insert into suppression_list (email, domain, reason) values (%s, %s, %s) on conflict do nothing",
            (email, domain, reason),
        )

    _LEAD_SQL = """select c.id as contact_id, co.id as company_id, co.name as company_name,
                          co.domain, co.website, co.country, co.industry,
                          c.full_name as contact_name, c.email
                   from contacts c join companies co on co.id = c.company_id"""

    @staticmethod
    def _lead(r: dict) -> Lead:
        return Lead(**{**r, "contact_id": str(r["contact_id"]), "company_id": str(r["company_id"])})

    def leads_with_status(self, status: str, limit: int) -> list[Lead]:
        rows = self.conn.execute(
            self._LEAD_SQL + " where c.status = %s::lead_status order by c.created_at limit %s",
            (status, limit),
        ).fetchall()
        return [self._lead(r) for r in rows]

    def get_lead(self, contact_id: str) -> Lead:
        return self._lead(self.conn.execute(self._LEAD_SQL + " where c.id = %s", (contact_id,)).fetchone())

    def set_lead_status(self, contact_id: str, status: str) -> None:
        self.conn.execute("update contacts set status = %s::lead_status where id = %s", (status, contact_id))

    def save_research(self, company_id: str, report: ResearchReport) -> str:
        row = self.conn.execute(
            """insert into research_reports
                 (company_id, pages_fetched, services, tech_signals, workflow_hypotheses, summary,
                  model, disqualify_reason, decision_maker, public_emails)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id""",
            (company_id, Jsonb(report.pages_fetched), report.services, report.tech_signals,
             Jsonb([h.model_dump() for h in report.hypotheses]), report.summary, report.model,
             report.disqualify_reason, report.decision_maker, report.public_emails),
        ).fetchone()
        return str(row["id"])

    def latest_research(self, company_id: str) -> ResearchReport | None:
        r = self.conn.execute(
            "select * from research_reports where company_id = %s order by created_at desc limit 1",
            (company_id,),
        ).fetchone()
        if r is None:
            return None
        return ResearchReport(
            services=r["services"] or [], tech_signals=r["tech_signals"] or [],
            hypotheses=[Hypothesis(**h) for h in r["workflow_hypotheses"] or []],
            summary=r["summary"] or "", disqualify_reason=r["disqualify_reason"],
            decision_maker=r["decision_maker"], public_emails=r["public_emails"] or [],
            pages_fetched=r["pages_fetched"] or [], model=r["model"] or "",
        )

    @staticmethod
    def _message(r: dict) -> Message:
        return Message(
            id=str(r["id"]), contact_id=str(r["contact_id"]), step=r["step"], channel=r["channel"],
            subject=r["subject"] or "", body=r["body"] or "", evidence=r["evidence"] or {},
            version=r["version"], status=r["status"], feedback=r["feedback"],
            telegram_chat_id=r["telegram_chat_id"], telegram_message_id=r["telegram_message_id"],
        )

    def save_message(self, msg: NewMessage) -> Message:
        row = self.conn.execute(
            """insert into messages (contact_id, step, channel, subject, body, evidence, version, status, feedback)
               values (%s, %s, %s::channel_type, %s, %s, %s, %s, %s::message_status, %s) returning *""",
            (msg.contact_id, msg.step, msg.channel, msg.subject, msg.body, Jsonb(msg.evidence),
             msg.version, msg.status, msg.feedback),
        ).fetchone()
        return self._message(row)

    def get_message(self, message_id: str) -> Message:
        row = self.conn.execute("select * from messages where id = %s", (message_id,)).fetchone()
        if row is None:
            raise KeyError(message_id)
        return self._message(row)

    def latest_message(self, contact_id: str) -> Message | None:
        r = self.conn.execute(
            "select * from messages where contact_id = %s order by created_at desc limit 1", (contact_id,)
        ).fetchone()
        return self._message(r) if r else None

    def set_company_industry(self, company_id: str, industry: str) -> None:
        """Tag the sector only if we don't already have one (the lead list is more trusted)."""
        self.conn.execute(
            "update companies set industry = %s where id = %s and industry is null", (industry, company_id)
        )

    def update_message(self, message_id: str, **fields: object) -> Message:
        _check_fields(fields)
        sets, params = [], []
        for name, value in fields.items():
            if name == "evidence":
                value = Jsonb(value)
            cast = "::message_status" if name == "status" else ""
            sets.append(f"{name} = %s{cast}")
            params.append(value)
        row = self.conn.execute(
            f"update messages set {', '.join(sets)} where id = %s returning *", (*params, message_id)
        ).fetchone()
        return self._message(row)

    def message_needing_changes(self, chat_id: int) -> Message | None:
        r = self.conn.execute(
            """select * from messages where status = 'needs_changes' and telegram_chat_id = %s
               order by created_at desc limit 1""",
            (chat_id,),
        ).fetchone()
        return self._message(r) if r else None

    def log_event(
        self, type: str, *, contact_id: str | None = None, message_id: str | None = None,
        actor: str = "agent", payload: dict | None = None,
    ) -> None:
        self.conn.execute(
            "insert into events (contact_id, message_id, type, actor, payload) values (%s, %s, %s, %s, %s)",
            (contact_id, message_id, type, actor, Jsonb(payload or {})),
        )


class InMemoryRepo:
    """Same behaviour as PostgresRepo, held in dicts. Used by --dry-run and tests."""

    def __init__(self) -> None:
        self.companies: dict[str, dict] = {}
        self.contacts: dict[str, dict] = {}
        self.research: dict[str, list[ResearchReport]] = {}
        self.messages: dict[str, Message] = {}
        self.suppressed: list[dict] = []
        self.events: list[dict] = []

    def upsert_lead(self, company: CompanyIn, contact: ContactIn) -> str:
        comp = next((c for c in self.companies.values() if c["domain"] == company.domain), None)
        if comp is None:
            comp = {"id": str(uuid.uuid4()), **company.model_dump()}
            self.companies[comp["id"]] = comp
        else:
            for k, v in company.model_dump().items():
                if v is not None:
                    comp[k] = v
        existing = next(
            (c for c in self.contacts.values()
             if c["company_id"] == comp["id"] and contact.email is not None and c["email"] == contact.email),
            None,
        )
        if existing:
            for k, v in contact.model_dump().items():
                if v is not None:
                    existing[k] = v
            return existing["id"]
        row = {"id": str(uuid.uuid4()), "company_id": comp["id"], "status": "new", **contact.model_dump()}
        self.contacts[row["id"]] = row
        return row["id"]

    def is_suppressed(self, email: str | None, domain: str | None) -> bool:
        email, domain = (email or "").lower(), (domain or "").lower()
        return any(
            (s["email"] and s["email"].lower() == email) or (s["domain"] and s["domain"].lower() == domain)
            for s in self.suppressed
        )

    def add_suppression(self, reason: str, email: str | None = None, domain: str | None = None) -> None:
        self.suppressed.append({"email": email, "domain": domain, "reason": reason})

    def _lead(self, c: dict) -> Lead:
        co = self.companies[c["company_id"]]
        return Lead(
            contact_id=c["id"], company_id=co["id"], company_name=co["name"], domain=co["domain"],
            website=co["website"], country=co["country"], industry=co["industry"],
            contact_name=c["full_name"], email=c["email"],
        )

    def leads_with_status(self, status: str, limit: int) -> list[Lead]:
        return [self._lead(c) for c in self.contacts.values() if c["status"] == status][:limit]

    def get_lead(self, contact_id: str) -> Lead:
        return self._lead(self.contacts[contact_id])

    def set_lead_status(self, contact_id: str, status: str) -> None:
        self.contacts[contact_id]["status"] = status

    def save_research(self, company_id: str, report: ResearchReport) -> str:
        self.research.setdefault(company_id, []).append(report)
        return str(uuid.uuid4())

    def latest_research(self, company_id: str) -> ResearchReport | None:
        reports = self.research.get(company_id)
        return reports[-1] if reports else None

    def save_message(self, msg: NewMessage) -> Message:
        m = Message(id=str(uuid.uuid4()), **msg.model_dump())
        self.messages[m.id] = m
        return m

    def get_message(self, message_id: str) -> Message:
        return self.messages[message_id]

    def latest_message(self, contact_id: str) -> Message | None:
        found = [m for m in self.messages.values() if m.contact_id == contact_id]
        return found[-1] if found else None

    def set_company_industry(self, company_id: str, industry: str) -> None:
        if self.companies[company_id]["industry"] is None:
            self.companies[company_id]["industry"] = industry

    def update_message(self, message_id: str, **fields: object) -> Message:
        _check_fields(fields)
        self.messages[message_id] = self.messages[message_id].model_copy(update=fields)
        return self.messages[message_id]

    def message_needing_changes(self, chat_id: int) -> Message | None:
        found = [m for m in self.messages.values() if m.status == "needs_changes" and m.telegram_chat_id == chat_id]
        return found[-1] if found else None

    def log_event(
        self, type: str, *, contact_id: str | None = None, message_id: str | None = None,
        actor: str = "agent", payload: dict | None = None,
    ) -> None:
        self.events.append(
            {"type": type, "contact_id": contact_id, "message_id": message_id, "actor": actor, "payload": payload or {}}
        )
