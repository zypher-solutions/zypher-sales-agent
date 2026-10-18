"""Data access. The pipeline talks to the `Repository` protocol, never to SQL.

Two implementations: PostgresRepo (Supabase, the source of truth) and
InMemoryRepo (for --dry-run and tests, needs nothing installed or configured).

Tenancy: a repository is bound to ONE tenant when it is created. Every read and
write is scoped to it, and ids belonging to another tenant behave as if they do
not exist. Callers never pass a tenant per call, so it cannot be forgotten.
"""
import uuid
from typing import Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from zypher_agent.config import DEFAULT_TENANT_ID
from zypher_agent.models import (
    CompanyIn,
    ContactIn,
    Hypothesis,
    Lead,
    Message,
    NewMessage,
    ResearchReport,
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
    def __init__(self, dsn: str, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        self.tenant_id, self._dsn = tenant_id, dsn
        self.conn = self._connect()

    def _connect(self) -> psycopg.Connection:
        # prepare_threshold=None: Supabase's pooler (transaction mode) rejects prepared statements.
        # TCP keepalives stop idle connections being silently dropped while the bot waits for taps.
        return psycopg.connect(
            self._dsn, autocommit=True, row_factory=dict_row, prepare_threshold=None,
            keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
        )

    def _exec(self, sql: str, params: tuple | None = None) -> psycopg.Cursor:
        """Execute, reconnecting first if the connection was closed or broken by an earlier failure.

        We never retry a statement that failed mid-flight (it might have been applied); the failure
        surfaces, the connection is marked broken, and the next call reconnects.
        """
        if self.conn.closed or self.conn.broken:
            self.conn = self._connect()
        return self.conn.execute(sql, params)

    def upsert_lead(self, company: CompanyIn, contact: ContactIn) -> str:
        with self.conn.transaction():
            comp = self._exec(
                """insert into companies (tenant_id, name, domain, website, country, industry, notes, source)
                   values (%s, %s, %s, %s, %s, %s, %s, %s)
                   on conflict (tenant_id, domain) do update set
                     name = excluded.name,
                     website = coalesce(excluded.website, companies.website),
                     country = coalesce(excluded.country, companies.country),
                     industry = coalesce(excluded.industry, companies.industry),
                     notes = coalesce(excluded.notes, companies.notes)
                   returning id""",
                (self.tenant_id, company.name, company.domain, company.website, company.country,
                 company.industry, company.notes, company.source),
            ).fetchone()
            row = self._exec(
                """insert into contacts (tenant_id, company_id, full_name, role, email, legacy_lead_id)
                   values (%s, %s, %s, %s, %s, %s)
                   on conflict (company_id, email) do update set
                     full_name = coalesce(excluded.full_name, contacts.full_name),
                     role = coalesce(excluded.role, contacts.role),
                     legacy_lead_id = coalesce(excluded.legacy_lead_id, contacts.legacy_lead_id)
                   returning id""",
                (self.tenant_id, comp["id"], contact.full_name, contact.role, contact.email,
                 contact.legacy_lead_id),
            ).fetchone()
        return str(row["id"])

    def is_suppressed(self, email: str | None, domain: str | None) -> bool:
        row = self._exec(
            """select 1 from suppression_list
               where tenant_id = %s
                 and ((email is not null and lower(email) = lower(%s))
                   or (domain is not null and lower(domain) = lower(%s))) limit 1""",
            (self.tenant_id, email, domain),
        ).fetchone()
        return row is not None

    def add_suppression(self, reason: str, email: str | None = None, domain: str | None = None) -> None:
        self._exec(
            """insert into suppression_list (tenant_id, email, domain, reason) values (%s, %s, %s, %s)
               on conflict do nothing""",
            (self.tenant_id, email, domain, reason),
        )

    _LEAD_SQL = """select c.id as contact_id, co.id as company_id, co.name as company_name,
                          co.domain, co.website, co.country, co.industry,
                          c.full_name as contact_name, c.email
                   from contacts c join companies co on co.id = c.company_id
                   where c.tenant_id = %s and co.tenant_id = %s"""

    @staticmethod
    def _lead(r: dict) -> Lead:
        return Lead(**{**r, "contact_id": str(r["contact_id"]), "company_id": str(r["company_id"])})

    def leads_with_status(self, status: str, limit: int) -> list[Lead]:
        rows = self._exec(
            self._LEAD_SQL + " and c.status = %s::lead_status order by c.created_at limit %s",
            (self.tenant_id, self.tenant_id, status, limit),
        ).fetchall()
        return [self._lead(r) for r in rows]

    def get_lead(self, contact_id: str) -> Lead:
        row = self._exec(
            self._LEAD_SQL + " and c.id = %s", (self.tenant_id, self.tenant_id, contact_id)
        ).fetchone()
        if row is None:
            raise KeyError(contact_id)
        return self._lead(row)

    def set_lead_status(self, contact_id: str, status: str) -> None:
        self._exec(
            "update contacts set status = %s::lead_status where id = %s and tenant_id = %s",
            (status, contact_id, self.tenant_id),
        )

    def save_research(self, company_id: str, report: ResearchReport) -> str:
        # insert ... select: inserts nothing unless the company belongs to this tenant.
        row = self._exec(
            """insert into research_reports
                 (tenant_id, company_id, pages_fetched, services, tech_signals, workflow_hypotheses,
                  summary, model, disqualify_reason, decision_maker, public_emails)
               select %s, co.id, %s, %s, %s, %s, %s, %s, %s, %s, %s
               from companies co where co.id = %s and co.tenant_id = %s
               returning id""",
            (self.tenant_id, Jsonb(report.pages_fetched), report.services, report.tech_signals,
             Jsonb([h.model_dump() for h in report.hypotheses]), report.summary, report.model,
             report.disqualify_reason, report.decision_maker, report.public_emails,
             company_id, self.tenant_id),
        ).fetchone()
        if row is None:
            raise KeyError(company_id)
        return str(row["id"])

    def latest_research(self, company_id: str) -> ResearchReport | None:
        r = self._exec(
            """select * from research_reports where company_id = %s and tenant_id = %s
               order by created_at desc limit 1""",
            (company_id, self.tenant_id),
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
        # insert ... select: inserts nothing unless the contact belongs to this tenant.
        row = self._exec(
            """insert into messages (tenant_id, contact_id, step, channel, subject, body, evidence,
                                     version, status, feedback)
               select %s, c.id, %s, %s::channel_type, %s, %s, %s, %s, %s::message_status, %s
               from contacts c where c.id = %s and c.tenant_id = %s
               returning *""",
            (self.tenant_id, msg.step, msg.channel, msg.subject, msg.body, Jsonb(msg.evidence),
             msg.version, msg.status, msg.feedback, msg.contact_id, self.tenant_id),
        ).fetchone()
        if row is None:
            raise KeyError(msg.contact_id)
        return self._message(row)

    def get_message(self, message_id: str) -> Message:
        row = self._exec(
            "select * from messages where id = %s and tenant_id = %s", (message_id, self.tenant_id)
        ).fetchone()
        if row is None:
            raise KeyError(message_id)
        return self._message(row)

    def latest_message(self, contact_id: str) -> Message | None:
        r = self._exec(
            """select * from messages where contact_id = %s and tenant_id = %s
               order by created_at desc limit 1""",
            (contact_id, self.tenant_id),
        ).fetchone()
        return self._message(r) if r else None

    def set_company_industry(self, company_id: str, industry: str) -> None:
        """Tag the sector only if we don't already have one (the lead list is more trusted)."""
        self._exec(
            "update companies set industry = %s where id = %s and tenant_id = %s and industry is null",
            (industry, company_id, self.tenant_id),
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
        row = self._exec(
            f"update messages set {', '.join(sets)} where id = %s and tenant_id = %s returning *",
            (*params, message_id, self.tenant_id),
        ).fetchone()
        if row is None:
            raise KeyError(message_id)
        return self._message(row)

    def message_needing_changes(self, chat_id: int) -> Message | None:
        r = self._exec(
            """select * from messages
               where status = 'needs_changes' and telegram_chat_id = %s and tenant_id = %s
               order by created_at desc limit 1""",
            (chat_id, self.tenant_id),
        ).fetchone()
        return self._message(r) if r else None

    def log_event(
        self, type: str, *, contact_id: str | None = None, message_id: str | None = None,
        actor: str = "agent", payload: dict | None = None,
    ) -> None:
        self._exec(
            """insert into events (tenant_id, contact_id, message_id, type, actor, payload)
               values (%s, %s, %s, %s, %s, %s)""",
            (self.tenant_id, contact_id, message_id, type, actor, Jsonb(payload or {})),
        )


class _Store:
    """The shared dicts behind InMemoryRepo, so several tenant-bound repos can see the same 'database'."""

    def __init__(self) -> None:
        self.companies: dict[str, dict] = {}
        self.contacts: dict[str, dict] = {}
        self.research: dict[str, list[tuple[str, ResearchReport]]] = {}  # company_id -> [(tenant_id, report)]
        self.messages: dict[str, Message] = {}
        self.message_tenant: dict[str, str] = {}
        self.suppressed: list[dict] = []
        self.events: list[dict] = []


class InMemoryRepo:
    """Same behaviour as PostgresRepo, held in dicts. Used by --dry-run and tests."""

    def __init__(self, tenant_id: str = DEFAULT_TENANT_ID, _store: _Store | None = None) -> None:
        self.tenant_id = tenant_id
        self._store = _store or _Store()
        s = self._store
        # Raw, all-tenant views (rows carry tenant_id); handy for test assertions.
        self.companies, self.contacts, self.messages = s.companies, s.contacts, s.messages
        self.suppressed, self.events = s.suppressed, s.events

    def for_tenant(self, tenant_id: str) -> "InMemoryRepo":
        """Another tenant's view of the same in-memory database (used to test isolation)."""
        return InMemoryRepo(tenant_id, self._store)

    def _contact(self, contact_id: str) -> dict:
        c = self.contacts.get(contact_id)
        if c is None or c["tenant_id"] != self.tenant_id:
            raise KeyError(contact_id)
        return c

    def _company(self, company_id: str) -> dict:
        c = self.companies.get(company_id)
        if c is None or c["tenant_id"] != self.tenant_id:
            raise KeyError(company_id)
        return c

    def _msg(self, message_id: str) -> Message:
        if self._store.message_tenant.get(message_id) != self.tenant_id:
            raise KeyError(message_id)
        return self.messages[message_id]

    def upsert_lead(self, company: CompanyIn, contact: ContactIn) -> str:
        comp = next(
            (c for c in self.companies.values()
             if c["tenant_id"] == self.tenant_id and c["domain"] == company.domain),
            None,
        )
        if comp is None:
            comp = {"id": str(uuid.uuid4()), "tenant_id": self.tenant_id, **company.model_dump()}
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
        row = {"id": str(uuid.uuid4()), "tenant_id": self.tenant_id, "company_id": comp["id"],
               "status": "new", **contact.model_dump()}
        self.contacts[row["id"]] = row
        return row["id"]

    def is_suppressed(self, email: str | None, domain: str | None) -> bool:
        email, domain = (email or "").lower(), (domain or "").lower()
        return any(
            s["tenant_id"] == self.tenant_id
            and ((s["email"] and s["email"].lower() == email) or (s["domain"] and s["domain"].lower() == domain))
            for s in self.suppressed
        )

    def add_suppression(self, reason: str, email: str | None = None, domain: str | None = None) -> None:
        self.suppressed.append(
            {"tenant_id": self.tenant_id, "email": email, "domain": domain, "reason": reason}
        )

    def _lead(self, c: dict) -> Lead:
        co = self.companies[c["company_id"]]
        return Lead(
            contact_id=c["id"], company_id=co["id"], company_name=co["name"], domain=co["domain"],
            website=co["website"], country=co["country"], industry=co["industry"],
            contact_name=c["full_name"], email=c["email"],
        )

    def leads_with_status(self, status: str, limit: int) -> list[Lead]:
        return [
            self._lead(c) for c in self.contacts.values()
            if c["tenant_id"] == self.tenant_id and c["status"] == status
        ][:limit]

    def get_lead(self, contact_id: str) -> Lead:
        return self._lead(self._contact(contact_id))

    def set_lead_status(self, contact_id: str, status: str) -> None:
        c = self.contacts.get(contact_id)
        if c is not None and c["tenant_id"] == self.tenant_id:  # like SQL: an update that matches nothing
            c["status"] = status

    def save_research(self, company_id: str, report: ResearchReport) -> str:
        self._company(company_id)
        self._store.research.setdefault(company_id, []).append((self.tenant_id, report))
        return str(uuid.uuid4())

    def latest_research(self, company_id: str) -> ResearchReport | None:
        mine = [r for t, r in self._store.research.get(company_id, []) if t == self.tenant_id]
        return mine[-1] if mine else None

    def save_message(self, msg: NewMessage) -> Message:
        self._contact(msg.contact_id)
        m = Message(id=str(uuid.uuid4()), **msg.model_dump())
        self.messages[m.id] = m
        self._store.message_tenant[m.id] = self.tenant_id
        return m

    def get_message(self, message_id: str) -> Message:
        return self._msg(message_id)

    def latest_message(self, contact_id: str) -> Message | None:
        found = [
            m for m in self.messages.values()
            if m.contact_id == contact_id and self._store.message_tenant[m.id] == self.tenant_id
        ]
        return found[-1] if found else None

    def set_company_industry(self, company_id: str, industry: str) -> None:
        c = self.companies.get(company_id)
        if c is not None and c["tenant_id"] == self.tenant_id and c["industry"] is None:
            c["industry"] = industry

    def update_message(self, message_id: str, **fields: object) -> Message:
        _check_fields(fields)
        self._msg(message_id)
        self.messages[message_id] = self.messages[message_id].model_copy(update=fields)
        return self.messages[message_id]

    def message_needing_changes(self, chat_id: int) -> Message | None:
        found = [
            m for m in self.messages.values()
            if m.status == "needs_changes" and m.telegram_chat_id == chat_id
            and self._store.message_tenant[m.id] == self.tenant_id
        ]
        return found[-1] if found else None

    def log_event(
        self, type: str, *, contact_id: str | None = None, message_id: str | None = None,
        actor: str = "agent", payload: dict | None = None,
    ) -> None:
        self.events.append(
            {"tenant_id": self.tenant_id, "type": type, "contact_id": contact_id,
             "message_id": message_id, "actor": actor, "payload": payload or {}}
        )
