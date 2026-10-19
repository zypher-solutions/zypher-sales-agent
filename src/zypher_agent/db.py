"""Data access. The pipeline talks to the `Repository` protocol, never to SQL.

Two implementations: PostgresRepo (Supabase, the source of truth) and
InMemoryRepo (for --dry-run and tests, needs nothing installed or configured).

Tenancy: a repository is bound to ONE tenant when it is created. Every read and
write is scoped to it, and ids belonging to another tenant behave as if they do
not exist. Callers never pass a tenant per call, so it cannot be forgotten.
"""
import uuid
from datetime import UTC, datetime
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
    SendStats,
)

# Columns update_message may touch. Whitelisted because they become SQL identifiers.
MESSAGE_FIELDS = {
    "subject", "body", "evidence", "version", "status", "feedback",
    "telegram_chat_id", "telegram_message_id",
    "message_id_header", "in_reply_to", "send_after", "send_attempts", "last_error", "sent_from", "sent_at",
}


class Repository(Protocol):
    def upsert_lead(self, company: CompanyIn, contact: ContactIn) -> str: ...
    def company_exists(self, domain: str) -> bool: ...
    def reject_company(self, company_id: str, reason: str, icp_version: int | None = None) -> bool: ...
    def is_rejected(self, domain: str) -> bool: ...
    def rejected_count(self) -> int: ...
    def set_contact_email(self, contact_id: str, email: str) -> None: ...
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
    def messages_with_status(self, statuses: list[str]) -> list[Message]: ...
    # sending
    def approved_to_send(self, now: datetime) -> list[Message]: ...
    def approved_version(self, message_id: str) -> int | None: ...
    def claim_for_sending(self, message_id: str, message_id_header: str, from_addr: str) -> bool: ...
    def send_stats(self, day_start: datetime) -> SendStats: ...
    def company_contacted(self, company_id: str, except_contact_id: str) -> bool: ...
    def sending_state(self) -> tuple[bool, str | None]: ...
    def set_sending_paused(self, paused: bool, reason: str | None = None) -> None: ...
    def first_message_header(self, contact_id: str) -> tuple[str, str] | None: ...
    # follow-ups
    def sequence_candidates(self) -> list[tuple[Lead, int, datetime]]: ...
    def sent_messages(self, contact_id: str) -> list[Message]: ...
    def cancel_unsent(self, contact_id: str, reason: str) -> int: ...
    def has_event_since(self, contact_id: str, type: str, since: datetime) -> bool: ...
    # inbox
    def message_by_header(self, header: str) -> Message | None: ...
    def lead_by_email(self, email: str) -> Lead | None: ...
    def recent_sent(self, limit: int) -> list[Message]: ...
    # statistics
    def sent_since(self, since: datetime | None) -> list[Message]: ...
    def events_of(self, types: list[str], since: datetime | None) -> list[dict]: ...
    def inbox_cursor(self) -> tuple[int, int] | None: ...
    def set_inbox_cursor(self, uidvalidity: int, uid: int) -> None: ...
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
                """insert into contacts (tenant_id, company_id, full_name, role, email, phone, legacy_lead_id)
                   values (%s, %s, %s, %s, %s, %s, %s)
                   on conflict (company_id, email) do update set
                     full_name = coalesce(excluded.full_name, contacts.full_name),
                     role = coalesce(excluded.role, contacts.role),
                     phone = coalesce(excluded.phone, contacts.phone),
                     legacy_lead_id = coalesce(excluded.legacy_lead_id, contacts.legacy_lead_id)
                   returning id""",
                (self.tenant_id, comp["id"], contact.full_name, contact.role, contact.email, contact.phone,
                 contact.legacy_lead_id),
            ).fetchone()
        return str(row["id"])

    def company_exists(self, domain: str) -> bool:
        row = self._exec(
            "select 1 from companies where tenant_id = %s and domain = %s", (self.tenant_id, domain)
        ).fetchone()
        return row is not None

    def reject_company(self, company_id: str, reason: str, icp_version: int | None = None) -> bool:
        """Forget a company research rejected: delete it (contacts, research and drafts cascade) and keep only its
        domain, so imports skip it and we never pay to research it again. A company we have already emailed is
        never purged (its history is needed for replies and opt-outs). Returns True when purged."""
        with self.conn.transaction():
            co = self._exec(
                """select co.domain,
                          exists (select 1 from contacts c join messages m on m.contact_id = c.id
                                  where c.company_id = co.id and m.sent_at is not null) as emailed
                   from companies co where co.id = %s and co.tenant_id = %s""",
                (company_id, self.tenant_id),
            ).fetchone()
            if co is None or co["emailed"]:
                return False
            self._exec(
                """insert into rejected_domains (tenant_id, domain, reason, icp_version) values (%s, %s, %s, %s)
                   on conflict do nothing""",
                (self.tenant_id, co["domain"], reason, icp_version),
            )
            self._exec("delete from companies where id = %s and tenant_id = %s", (company_id, self.tenant_id))
        return True

    def is_rejected(self, domain: str) -> bool:
        row = self._exec(
            "select 1 from rejected_domains where tenant_id = %s and lower(domain) = lower(%s)", (self.tenant_id, domain)
        ).fetchone()
        return row is not None

    def rejected_count(self) -> int:
        row = self._exec("select count(*) as n from rejected_domains where tenant_id = %s", (self.tenant_id,)).fetchone()
        return int(row["n"])

    def set_contact_email(self, contact_id: str, email: str) -> None:
        self._exec(
            "update contacts set email = %s where id = %s and tenant_id = %s", (email, contact_id, self.tenant_id)
        )

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
                          c.full_name as contact_name, c.email, c.phone, c.status::text as status
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
                  summary, model, disqualify_reason, decision_maker, public_emails, qualification)
               select %s, co.id, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
               from companies co where co.id = %s and co.tenant_id = %s
               returning id""",
            (self.tenant_id, Jsonb(report.pages_fetched), report.services, report.tech_signals,
             Jsonb([h.model_dump() for h in report.hypotheses]), report.summary, report.model,
             report.disqualify_reason, report.decision_maker, report.public_emails, Jsonb(report.qualification),
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
            qualification=r.get("qualification") or [], pages_fetched=r["pages_fetched"] or [], model=r["model"] or "",
        )

    @staticmethod
    def _message(r: dict) -> Message:
        return Message(
            id=str(r["id"]), contact_id=str(r["contact_id"]), step=r["step"], channel=r["channel"],
            subject=r["subject"] or "", body=r["body"] or "", evidence=r["evidence"] or {},
            version=r["version"], status=r["status"], feedback=r["feedback"],
            telegram_chat_id=r["telegram_chat_id"], telegram_message_id=r["telegram_message_id"],
            direction=r.get("direction") or "outbound", reply_class=r.get("reply_class"),
            message_id_header=r.get("message_id_header"), in_reply_to=r.get("in_reply_to"),
            send_after=r.get("send_after"), send_attempts=r.get("send_attempts") or 0,
            last_error=r.get("last_error"), sent_from=r.get("sent_from"), sent_at=r.get("sent_at"),
        )

    def save_message(self, msg: NewMessage) -> Message:
        # insert ... select: inserts nothing unless the contact belongs to this tenant.
        row = self._exec(
            """insert into messages (tenant_id, contact_id, step, channel, subject, body, evidence,
                                     version, status, feedback, direction, reply_class)
               select %s, c.id, %s, %s::channel_type, %s, %s, %s, %s, %s::message_status, %s, %s, %s::reply_class
               from contacts c where c.id = %s and c.tenant_id = %s
               returning *""",
            (self.tenant_id, msg.step, msg.channel, msg.subject, msg.body, Jsonb(msg.evidence),
             msg.version, msg.status, msg.feedback, msg.direction, msg.reply_class, msg.contact_id, self.tenant_id),
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
            """select * from messages where contact_id = %s and tenant_id = %s and direction = 'outbound'
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

    def approved_to_send(self, now: datetime) -> list[Message]:
        rows = self._exec(
            """select * from messages where tenant_id = %s and status = 'approved'
               and (send_after is null or send_after <= %s) order by created_at""",
            (self.tenant_id, now),
        ).fetchall()
        return [self._message(r) for r in rows]

    def approved_version(self, message_id: str) -> int | None:
        row = self._exec(
            """select (payload->>'version')::int as v from events
               where tenant_id = %s and message_id = %s and type = 'approved' order by id desc limit 1""",
            (self.tenant_id, message_id),
        ).fetchone()
        return row["v"] if row else None

    def claim_for_sending(self, message_id: str, message_id_header: str, from_addr: str) -> bool:
        """Atomically move approved -> queued. Two workers can never both win, so nothing is sent twice."""
        row = self._exec(
            """update messages set status = 'queued', send_attempts = send_attempts + 1,
                      message_id_header = %s, sent_from = %s, last_error = null
               where id = %s and tenant_id = %s and status = 'approved' returning id""",
            (message_id_header, from_addr, message_id, self.tenant_id),
        ).fetchone()
        return row is not None

    def send_stats(self, day_start: datetime) -> SendStats:
        row = self._exec(
            """select count(*) filter (where sent_at >= %s) as today, min(sent_at) as first, max(sent_at) as last
               from messages where tenant_id = %s and sent_at is not null and direction = 'outbound'""",
            (day_start, self.tenant_id),
        ).fetchone()
        return SendStats(sent_today=row["today"], first_sent_at=row["first"], last_sent_at=row["last"])

    def company_contacted(self, company_id: str, except_contact_id: str) -> bool:
        row = self._exec(
            """select 1 from messages m join contacts c on c.id = m.contact_id
               where m.tenant_id = %s and c.company_id = %s and c.id <> %s
                 and m.step = 1 and m.sent_at is not null limit 1""",
            (self.tenant_id, company_id, except_contact_id),
        ).fetchone()
        return row is not None

    def sequence_candidates(self) -> list[tuple[Lead, int, datetime]]:
        """Leads mid-sequence with their last SENT step and when it left. Awaiting-approval follow-ups are excluded
        because the lead's status is not 'in_sequence' while a card is open."""
        rows = self._exec(
            """select c.id as contact_id, co.id as company_id, co.name as company_name, co.domain, co.website,
                      co.country, co.industry, c.full_name as contact_name, c.email, c.phone, c.status::text as status,
                      last.step as last_step, last.sent_at as last_sent_at
               from contacts c join companies co on co.id = c.company_id
               join lateral (select step, sent_at from messages m where m.contact_id = c.id and m.direction = 'outbound'
                             and m.sent_at is not null order by m.step desc limit 1) last on true
               where c.tenant_id = %s and co.tenant_id = %s and c.status = 'in_sequence' order by last.sent_at""",
            (self.tenant_id, self.tenant_id),
        ).fetchall()
        return [(self._lead(r), r["last_step"], r["last_sent_at"]) for r in rows]

    def sent_messages(self, contact_id: str) -> list[Message]:
        rows = self._exec(
            """select * from messages where tenant_id = %s and contact_id = %s and direction = 'outbound'
               and sent_at is not null order by step""",
            (self.tenant_id, contact_id),
        ).fetchall()
        return [self._message(r) for r in rows]

    def cancel_unsent(self, contact_id: str, reason: str) -> int:
        """Withdraw every email to this contact that has not left yet (they replied, bounced or opted out)."""
        rows = self._exec(
            """update messages set status = 'rejected', last_error = %s
               where tenant_id = %s and contact_id = %s and direction = 'outbound' and sent_at is null
                 and status in ('draft', 'awaiting_approval', 'needs_changes', 'approved') returning id""",
            (reason, self.tenant_id, contact_id),
        ).fetchall()
        return len(rows)

    def has_event_since(self, contact_id: str, type: str, since: datetime) -> bool:
        row = self._exec(
            "select 1 from events where tenant_id = %s and contact_id = %s and type = %s and created_at >= %s limit 1",
            (self.tenant_id, contact_id, type, since),
        ).fetchone()
        return row is not None

    def message_by_header(self, header: str) -> Message | None:
        r = self._exec(
            "select * from messages where tenant_id = %s and message_id_header = %s", (self.tenant_id, header)
        ).fetchone()
        return self._message(r) if r else None

    def lead_by_email(self, email: str) -> Lead | None:
        r = self._exec(
            self._LEAD_SQL + " and lower(c.email) = lower(%s) order by c.created_at desc limit 1",
            (self.tenant_id, self.tenant_id, email),
        ).fetchone()
        return self._lead(r) if r else None

    def recent_sent(self, limit: int) -> list[Message]:
        rows = self._exec(
            """select * from messages where tenant_id = %s and direction = 'outbound' and sent_at is not null
               order by sent_at desc limit %s""",
            (self.tenant_id, limit),
        ).fetchall()
        return [self._message(r) for r in rows]

    def sent_since(self, since: datetime | None) -> list[Message]:
        rows = self._exec(
            """select * from messages where tenant_id = %s and direction = 'outbound' and sent_at is not null
               and (%s::timestamptz is null or sent_at >= %s) order by sent_at""",
            (self.tenant_id, since, since),
        ).fetchall()
        return [self._message(r) for r in rows]

    def events_of(self, types: list[str], since: datetime | None) -> list[dict]:
        """(contact_id, type, payload, at) of events of the given types, oldest first."""
        rows = self._exec(
            """select contact_id::text as contact_id, type, payload, created_at as at from events
               where tenant_id = %s and type = any(%s) and (%s::timestamptz is null or created_at >= %s) order by id""",
            (self.tenant_id, types, since, since),
        ).fetchall()
        return [dict(r) for r in rows]

    def inbox_cursor(self) -> tuple[int, int] | None:
        """(UIDVALIDITY, last UID handled) of the mailbox poll. Kept in the event log: no extra table."""
        r = self._exec(
            """select (payload->>'uidvalidity')::bigint v, (payload->>'uid')::bigint u from events
               where tenant_id = %s and type = 'inbox_cursor' order by id desc limit 1""",
            (self.tenant_id,),
        ).fetchone()
        return (r["v"], r["u"]) if r else None

    def set_inbox_cursor(self, uidvalidity: int, uid: int) -> None:
        self.log_event("inbox_cursor", actor="system", payload={"uidvalidity": uidvalidity, "uid": uid})

    def first_message_header(self, contact_id: str) -> tuple[str, str] | None:
        """(Message-ID, subject) of the first email we sent this contact: follow-ups thread under it."""
        row = self._exec(
            """select message_id_header, subject from messages where tenant_id = %s and contact_id = %s
               and step = 1 and sent_at is not null and message_id_header is not null order by sent_at limit 1""",
            (self.tenant_id, contact_id),
        ).fetchone()
        return (row["message_id_header"], row["subject"]) if row else None

    def sending_state(self) -> tuple[bool, str | None]:
        row = self._exec("select sending_paused, pause_reason from tenants where id = %s", (self.tenant_id,)).fetchone()
        return (bool(row["sending_paused"]), row["pause_reason"]) if row else (True, "tenant not found")

    def set_sending_paused(self, paused: bool, reason: str | None = None) -> None:
        self._exec(
            "update tenants set sending_paused = %s, pause_reason = %s, "
            "paused_at = case when %s then now() else null end where id = %s",
            (paused, reason if paused else None, paused, self.tenant_id),
        )

    def messages_with_status(self, statuses: list[str]) -> list[Message]:
        rows = self._exec(
            "select * from messages where tenant_id = %s and status = any(%s::message_status[]) order by created_at",
            (self.tenant_id, statuses),
        ).fetchall()
        return [self._message(r) for r in rows]

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
        self.rejected: list[dict] = []  # {tenant_id, domain, reason, icp_version}
        self.events: list[dict] = []
        self.paused: dict[str, tuple[bool, str | None]] = {}  # tenant_id -> (paused, reason)


class InMemoryRepo:
    """Same behaviour as PostgresRepo, held in dicts. Used by --dry-run and tests."""

    def __init__(self, tenant_id: str = DEFAULT_TENANT_ID, _store: _Store | None = None) -> None:
        self.tenant_id = tenant_id
        self._store = _store or _Store()
        s = self._store
        # Raw, all-tenant views (rows carry tenant_id); handy for test assertions.
        self.companies, self.contacts, self.messages = s.companies, s.contacts, s.messages
        self.suppressed, self.events, self.rejected = s.suppressed, s.events, s.rejected

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
            contact_name=c["full_name"], email=c["email"], phone=c.get("phone"), status=c["status"],
        )

    def company_exists(self, domain: str) -> bool:
        return any(c["tenant_id"] == self.tenant_id and c["domain"] == domain for c in self.companies.values())

    def reject_company(self, company_id: str, reason: str, icp_version: int | None = None) -> bool:
        co = self.companies.get(company_id)
        if co is None or co["tenant_id"] != self.tenant_id:
            return False
        contact_ids = {c["id"] for c in self.contacts.values() if c["company_id"] == company_id}
        msgs = [m for m in self.messages.values() if m.contact_id in contact_ids]
        if any(m.sent_at is not None for m in msgs):
            return False
        if not self.is_rejected(co["domain"]):
            self.rejected.append({"tenant_id": self.tenant_id, "domain": co["domain"], "reason": reason,
                                  "icp_version": icp_version})
        for m in msgs:  # like ON DELETE CASCADE: company -> contacts -> messages, company -> research
            del self.messages[m.id]
            self._store.message_tenant.pop(m.id, None)
        for cid in contact_ids:
            del self.contacts[cid]
        self._store.research.pop(company_id, None)
        del self.companies[company_id]
        return True

    def is_rejected(self, domain: str) -> bool:
        d = (domain or "").lower()
        return any(r["tenant_id"] == self.tenant_id and r["domain"].lower() == d for r in self.rejected)

    def rejected_count(self) -> int:
        return sum(r["tenant_id"] == self.tenant_id for r in self.rejected)

    def set_contact_email(self, contact_id: str, email: str) -> None:
        c = self.contacts.get(contact_id)
        if c is not None and c["tenant_id"] == self.tenant_id:
            c["email"] = email

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
            if m.contact_id == contact_id and m.direction == "outbound"
            and self._store.message_tenant[m.id] == self.tenant_id
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

    def approved_to_send(self, now: datetime) -> list[Message]:
        return [
            m for m in self.messages.values()
            if m.status == "approved" and self._store.message_tenant[m.id] == self.tenant_id
            and (m.send_after is None or m.send_after <= now)
        ]

    def approved_version(self, message_id: str) -> int | None:
        found = [
            e for e in self.events
            if e["tenant_id"] == self.tenant_id and e["message_id"] == message_id and e["type"] == "approved"
        ]
        return found[-1]["payload"].get("version") if found else None

    def claim_for_sending(self, message_id: str, message_id_header: str, from_addr: str) -> bool:
        m = self._msg(message_id)
        if m.status != "approved":
            return False
        self.messages[message_id] = m.model_copy(update={
            "status": "queued", "send_attempts": m.send_attempts + 1, "message_id_header": message_id_header,
            "sent_from": from_addr, "last_error": None,
        })
        return True

    def send_stats(self, day_start: datetime) -> SendStats:
        sent = [
            m.sent_at for m in self.messages.values()
            if m.sent_at is not None and self._store.message_tenant[m.id] == self.tenant_id
        ]
        return SendStats(
            sent_today=sum(t >= day_start for t in sent),
            first_sent_at=min(sent, default=None), last_sent_at=max(sent, default=None),
        )

    def company_contacted(self, company_id: str, except_contact_id: str) -> bool:
        return any(
            m.step == 1 and m.sent_at is not None and self._store.message_tenant[m.id] == self.tenant_id
            and self.contacts[m.contact_id]["company_id"] == company_id and m.contact_id != except_contact_id
            for m in self.messages.values()
        )

    def sequence_candidates(self) -> list[tuple[Lead, int, datetime]]:
        out = []
        for c in self.contacts.values():
            if c["tenant_id"] != self.tenant_id or c["status"] != "in_sequence":
                continue
            sent = [m for m in self.messages.values()
                    if m.contact_id == c["id"] and m.direction == "outbound" and m.sent_at is not None
                    and self._store.message_tenant[m.id] == self.tenant_id]
            if sent:
                last = max(sent, key=lambda m: m.step)
                out.append((self._lead(c), last.step, last.sent_at))
        return sorted(out, key=lambda t: t[2])

    def sent_messages(self, contact_id: str) -> list[Message]:
        return sorted(
            (m for m in self.messages.values()
             if m.contact_id == contact_id and m.direction == "outbound" and m.sent_at is not None
             and self._store.message_tenant[m.id] == self.tenant_id),
            key=lambda m: m.step,
        )

    def cancel_unsent(self, contact_id: str, reason: str) -> int:
        n = 0
        for mid, m in list(self.messages.items()):
            if (m.contact_id == contact_id and m.direction == "outbound" and m.sent_at is None
                    and m.status in ("draft", "awaiting_approval", "needs_changes", "approved")
                    and self._store.message_tenant[mid] == self.tenant_id):
                self.messages[mid] = m.model_copy(update={"status": "rejected", "last_error": reason})
                n += 1
        return n

    def has_event_since(self, contact_id: str, type: str, since: datetime) -> bool:
        return any(
            e["tenant_id"] == self.tenant_id and e["contact_id"] == contact_id and e["type"] == type
            and e.get("at", datetime.now(since.tzinfo)) >= since
            for e in self.events
        )

    def message_by_header(self, header: str) -> Message | None:
        return next(
            (m for m in self.messages.values()
             if m.message_id_header == header and self._store.message_tenant[m.id] == self.tenant_id), None,
        )

    def lead_by_email(self, email: str) -> Lead | None:
        mine = [c for c in self.contacts.values()
                if c["tenant_id"] == self.tenant_id and (c["email"] or "").lower() == email.lower()]
        return self._lead(mine[-1]) if mine else None

    def recent_sent(self, limit: int) -> list[Message]:
        sent = [m for m in self.messages.values()
                if m.direction == "outbound" and m.sent_at is not None and self._store.message_tenant[m.id] == self.tenant_id]
        return sorted(sent, key=lambda m: m.sent_at, reverse=True)[:limit]

    def sent_since(self, since: datetime | None) -> list[Message]:
        return sorted(
            (m for m in self.messages.values()
             if m.direction == "outbound" and m.sent_at is not None and (since is None or m.sent_at >= since)
             and self._store.message_tenant[m.id] == self.tenant_id),
            key=lambda m: m.sent_at,
        )

    def events_of(self, types: list[str], since: datetime | None) -> list[dict]:
        return [
            {"contact_id": e["contact_id"], "type": e["type"], "payload": e["payload"], "at": e["at"]}
            for e in self.events
            if e["tenant_id"] == self.tenant_id and e["type"] in types and (since is None or e["at"] >= since)
        ]

    def inbox_cursor(self) -> tuple[int, int] | None:
        found = [e for e in self.events if e["tenant_id"] == self.tenant_id and e["type"] == "inbox_cursor"]
        return (found[-1]["payload"]["uidvalidity"], found[-1]["payload"]["uid"]) if found else None

    def set_inbox_cursor(self, uidvalidity: int, uid: int) -> None:
        self.log_event("inbox_cursor", actor="system", payload={"uidvalidity": uidvalidity, "uid": uid})

    def first_message_header(self, contact_id: str) -> tuple[str, str] | None:
        sent = sorted(
            (m for m in self.messages.values()
             if m.contact_id == contact_id and m.step == 1 and m.sent_at and m.message_id_header
             and self._store.message_tenant[m.id] == self.tenant_id),
            key=lambda m: m.sent_at,
        )
        return (sent[0].message_id_header, sent[0].subject) if sent else None

    def sending_state(self) -> tuple[bool, str | None]:
        return self._store.paused.get(self.tenant_id, (False, None))

    def set_sending_paused(self, paused: bool, reason: str | None = None) -> None:
        self._store.paused[self.tenant_id] = (paused, reason if paused else None)

    def messages_with_status(self, statuses: list[str]) -> list[Message]:
        return [
            m for m in self.messages.values()
            if m.status in statuses and self._store.message_tenant[m.id] == self.tenant_id
        ]

    def log_event(
        self, type: str, *, contact_id: str | None = None, message_id: str | None = None,
        actor: str = "agent", payload: dict | None = None,
    ) -> None:
        self.events.append(
            {"tenant_id": self.tenant_id, "type": type, "contact_id": contact_id,
             "message_id": message_id, "actor": actor, "payload": payload or {}, "at": datetime.now(UTC)}
        )
