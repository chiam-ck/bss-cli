"""CRM schema — 12 tables.

party, individual, contact_medium, customer, customer_identity,
interaction, agent, case, case_note, ticket, ticket_state_history, sla_policy.
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TenantMixin, TimestampMixin, TZDateTime

SCHEMA = "crm"


class Party(Base, TenantMixin, TimestampMixin):
    __tablename__ = "party"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    party_type: Mapped[str] = mapped_column(Text, nullable=False)

    individual: Mapped["Individual | None"] = relationship(back_populates="party")
    contact_mediums: Mapped[list["ContactMedium"]] = relationship(back_populates="party")


class Individual(Base, TenantMixin, TimestampMixin):
    __tablename__ = "individual"
    __table_args__ = {"schema": SCHEMA}

    party_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.party.id"), primary_key=True
    )
    given_name: Mapped[str] = mapped_column(Text, nullable=False)
    family_name: Mapped[str] = mapped_column(Text, nullable=False)
    date_of_birth: Mapped[date | None] = mapped_column(Date)

    party: Mapped["Party"] = relationship(back_populates="individual")


class ContactMedium(Base, TenantMixin, TimestampMixin):
    __tablename__ = "contact_medium"
    __table_args__ = (
        Index(
            "uq_contact_medium_email_active",
            "medium_type",
            "value",
            unique=True,
            postgresql_where="valid_to IS NULL AND medium_type = 'email'",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    party_id: Mapped[str] = mapped_column(Text, ForeignKey(f"{SCHEMA}.party.id"), nullable=False)
    medium_type: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    valid_from: Mapped[datetime | None] = mapped_column(TZDateTime)
    valid_to: Mapped[datetime | None] = mapped_column(TZDateTime)

    party: Mapped["Party"] = relationship(back_populates="contact_mediums")


class Customer(Base, TenantMixin, TimestampMixin):
    __tablename__ = "customer"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    party_id: Mapped[str] = mapped_column(Text, ForeignKey(f"{SCHEMA}.party.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    status_reason: Mapped[str | None] = mapped_column(Text)
    customer_since: Mapped[datetime | None] = mapped_column(TZDateTime)
    kyc_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="not_verified", server_default="not_verified"
    )
    kyc_verified_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    kyc_verification_method: Mapped[str | None] = mapped_column(Text)
    kyc_reference: Mapped[str | None] = mapped_column(Text)
    kyc_expires_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    identity: Mapped["CustomerIdentity | None"] = relationship(back_populates="customer")
    interactions: Mapped[list["Interaction"]] = relationship(back_populates="customer")
    cases: Mapped[list["Case"]] = relationship(back_populates="customer")
    tickets: Mapped[list["Ticket"]] = relationship(back_populates="customer")


class CustomerIdentity(Base, TenantMixin, TimestampMixin):
    __tablename__ = "customer_identity"
    __table_args__ = (
        Index(
            "uq_customer_identity_doc",
            "document_type",
            "document_number_hash",
            "tenant_id",
            unique=True,
        ),
        {"schema": SCHEMA},
    )

    customer_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.customer.id"), primary_key=True
    )
    document_type: Mapped[str] = mapped_column(Text, nullable=False)
    document_number_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # v0.15: PDPA-aligned partial display ("796B" for NRIC S8369796B).
    # Nullable for back-compat with v0.14 rows; v0.15+ writes always populate.
    document_number_last4: Mapped[str | None] = mapped_column(String(4))
    document_country: Mapped[str] = mapped_column(Text, nullable=False)
    date_of_birth: Mapped[date] = mapped_column(Date, nullable=False)
    nationality: Mapped[str | None] = mapped_column(Text)
    verified_by: Mapped[str | None] = mapped_column(Text)
    attestation_payload: Mapped[dict | None] = mapped_column(JSONB)
    verified_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    # v0.15: FK into integrations.kyc_webhook_corroboration. Required when
    # verified_by='didit'; null for prebaked / channel-layer attestations.
    corroboration_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integrations.kyc_webhook_corroboration.id"),
    )

    customer: Mapped["Customer"] = relationship(back_populates="identity")


class Agent(Base, TenantMixin, TimestampMixin):
    __tablename__ = "agent"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text, unique=True)
    role: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")


class Interaction(Base, TenantMixin, TimestampMixin):
    __tablename__ = "interaction"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.customer.id"), nullable=False
    )
    channel: Mapped[str | None] = mapped_column(Text)
    direction: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    agent_id: Mapped[str | None] = mapped_column(Text, ForeignKey(f"{SCHEMA}.agent.id"))
    related_case_id: Mapped[str | None] = mapped_column(Text, ForeignKey(f"{SCHEMA}.case.id"))
    related_ticket_id: Mapped[str | None] = mapped_column(Text, ForeignKey(f"{SCHEMA}.ticket.id"))
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)

    customer: Mapped["Customer"] = relationship(back_populates="interactions")


class Case(Base, TenantMixin, TimestampMixin):
    __tablename__ = "case"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.customer.id"), nullable=False
    )
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    priority: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    resolution_code: Mapped[str | None] = mapped_column(Text)
    opened_by_agent_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.agent.id")
    )
    opened_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    # v0.12 — points at audit.chat_transcript.hash when the case was
    # opened by case.open_for_me from a chat-surface escalation. NULL
    # for cases opened via the CSR/CLI path.
    chat_transcript_hash: Mapped[str | None] = mapped_column(Text)

    customer: Mapped["Customer"] = relationship(back_populates="cases")
    notes: Mapped[list["CaseNote"]] = relationship(back_populates="case")
    tickets: Mapped[list["Ticket"]] = relationship(back_populates="case")


class CaseNote(Base, TenantMixin):
    __tablename__ = "case_note"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    case_id: Mapped[str] = mapped_column(Text, ForeignKey(f"{SCHEMA}.case.id"), nullable=False)
    author_agent_id: Mapped[str | None] = mapped_column(Text, ForeignKey(f"{SCHEMA}.agent.id"))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    case: Mapped["Case"] = relationship(back_populates="notes")


class Ticket(Base, TenantMixin, TimestampMixin):
    __tablename__ = "ticket"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    case_id: Mapped[str | None] = mapped_column(Text, ForeignKey(f"{SCHEMA}.case.id"))
    customer_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.customer.id"), nullable=False
    )
    ticket_type: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    priority: Mapped[str | None] = mapped_column(Text)
    assigned_to_agent_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.agent.id")
    )
    related_order_id: Mapped[str | None] = mapped_column(Text)
    related_subscription_id: Mapped[str | None] = mapped_column(Text)
    related_service_id: Mapped[str | None] = mapped_column(Text)
    sla_due_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    resolution_notes: Mapped[str | None] = mapped_column(Text)
    opened_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    customer: Mapped["Customer"] = relationship(back_populates="tickets")
    case: Mapped["Case | None"] = relationship(back_populates="tickets")
    state_history: Mapped[list["TicketStateHistory"]] = relationship(back_populates="ticket")


class TicketStateHistory(Base, TenantMixin):
    __tablename__ = "ticket_state_history"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticket_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.ticket.id"), nullable=False
    )
    from_state: Mapped[str | None] = mapped_column(Text)
    to_state: Mapped[str | None] = mapped_column(Text)
    changed_by_agent_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.agent.id")
    )
    reason: Mapped[str | None] = mapped_column(Text)
    event_time: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    ticket: Mapped["Ticket"] = relationship(back_populates="state_history")


class SlaPolicy(Base, TenantMixin, TimestampMixin):
    __tablename__ = "sla_policy"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    ticket_type: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(Text, nullable=False)
    target_resolution_minutes: Mapped[int] = mapped_column(BigInteger, nullable=False)


class PortRequest(Base, TenantMixin, TimestampMixin):
    """v0.17 MNP — operator-driven port-in / port-out request.

    Lives in the CRM schema (operator-touched) but is its own aggregate
    distinct from Case: a Case is a customer-incident container that
    parents Tickets; a PortRequest is an operational state machine over
    a single donor MSISDN with a fixed FSM
    (``requested → validated → completed | rejected``).

    Doctrine (CLAUDE.md v0.17+):
      * Customer-self-serve writes are forbidden — operator only.
      * On port-out approve, the carrier's MSISDN flips to terminal
        ``ported_out`` (NOT ``available``) so reserve-next never
        re-issues a number we've handed away.
    """

    __tablename__ = "port_request"
    __table_args__ = (
        Index(
            "uq_port_request_donor_pending",
            "donor_msisdn",
            "tenant_id",
            unique=True,
            postgresql_where="state IN ('requested','validated')",
        ),
        Index(
            "ix_port_request_state_direction",
            "state",
            "direction",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    donor_carrier: Mapped[str] = mapped_column(Text, nullable=False)
    donor_msisdn: Mapped[str] = mapped_column(Text, nullable=False)
    target_subscription_id: Mapped[str | None] = mapped_column(Text)
    requested_port_date: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, default="requested", server_default="requested"
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text)
