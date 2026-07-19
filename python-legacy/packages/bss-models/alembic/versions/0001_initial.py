"""Initial schema — 40 tables across 11 schemas.

Revision ID: 0001
Revises: None
Create Date: 2026-04-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# TIMESTAMP WITH TIME ZONE
TIMESTAMPTZ = sa.DateTime(timezone=True)

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The 11 BSS schemas — explicit list, never dynamic.
BSS_SCHEMAS = [
    "crm",
    "catalog",
    "inventory",
    "payment",
    "order_mgmt",
    "service_inventory",
    "provisioning",
    "subscription",
    "mediation",
    "billing",
    "audit",
]


def upgrade() -> None:
    # ── Create schemas ──────────────────────────────────────────────
    for schema in BSS_SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

    # ══════════════════════════════════════════════════════════════════
    # CRM — 12 tables
    # ══════════════════════════════════════════════════════════════════

    op.create_table(
        "party",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("party_type", sa.Text, nullable=False),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="crm",
    )

    op.create_table(
        "individual",
        sa.Column("party_id", sa.Text, sa.ForeignKey("crm.party.id"), primary_key=True),
        sa.Column("given_name", sa.Text, nullable=False),
        sa.Column("family_name", sa.Text, nullable=False),
        sa.Column("date_of_birth", sa.Date),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="crm",
    )

    op.create_table(
        "contact_medium",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("party_id", sa.Text, sa.ForeignKey("crm.party.id"), nullable=False),
        sa.Column("medium_type", sa.Text, nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("is_primary", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("valid_from", TIMESTAMPTZ),
        sa.Column("valid_to", TIMESTAMPTZ),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="crm",
    )
    op.create_index(
        "uq_contact_medium_email_active",
        "contact_medium",
        ["medium_type", "value"],
        unique=True,
        schema="crm",
        postgresql_where=sa.text("valid_to IS NULL AND medium_type = 'email'"),
    )

    op.create_table(
        "customer",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("party_id", sa.Text, sa.ForeignKey("crm.party.id"), nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("status_reason", sa.Text),
        sa.Column("customer_since", TIMESTAMPTZ),
        sa.Column("kyc_status", sa.Text, nullable=False, server_default="not_verified"),
        sa.Column("kyc_verified_at", TIMESTAMPTZ),
        sa.Column("kyc_verification_method", sa.Text),
        sa.Column("kyc_reference", sa.Text),
        sa.Column("kyc_expires_at", TIMESTAMPTZ),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="crm",
    )

    op.create_table(
        "customer_identity",
        sa.Column("customer_id", sa.Text, sa.ForeignKey("crm.customer.id"), primary_key=True),
        sa.Column("document_type", sa.Text, nullable=False),
        sa.Column("document_number_hash", sa.Text, nullable=False),
        sa.Column("document_country", sa.Text, nullable=False),
        sa.Column("date_of_birth", sa.Date, nullable=False),
        sa.Column("nationality", sa.Text),
        sa.Column("verified_by", sa.Text),
        sa.Column("attestation_payload", JSONB),
        sa.Column("verified_at", TIMESTAMPTZ, nullable=False),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="crm",
    )
    op.create_index(
        "uq_customer_identity_doc",
        "customer_identity",
        ["document_type", "document_number_hash", "tenant_id"],
        unique=True,
        schema="crm",
    )

    op.create_table(
        "agent",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("email", sa.Text, unique=True),
        sa.Column("role", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="crm",
    )

    op.create_table(
        "interaction",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("customer_id", sa.Text, sa.ForeignKey("crm.customer.id"), nullable=False),
        sa.Column("channel", sa.Text),
        sa.Column("direction", sa.Text),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("body", sa.Text),
        sa.Column("agent_id", sa.Text, sa.ForeignKey("crm.agent.id")),
        sa.Column("related_case_id", sa.Text),  # FK added after case table
        sa.Column("related_ticket_id", sa.Text),  # FK added after ticket table
        sa.Column("occurred_at", TIMESTAMPTZ, nullable=False),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="crm",
    )

    op.create_table(
        "case",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("customer_id", sa.Text, sa.ForeignKey("crm.customer.id"), nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("state", sa.Text, nullable=False, server_default="open"),
        sa.Column("priority", sa.Text),
        sa.Column("category", sa.Text),
        sa.Column("resolution_code", sa.Text),
        sa.Column("opened_by_agent_id", sa.Text, sa.ForeignKey("crm.agent.id")),
        sa.Column("opened_at", TIMESTAMPTZ, nullable=False),
        sa.Column("closed_at", TIMESTAMPTZ),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="crm",
    )

    # Add deferred FKs on interaction now that case exists
    op.create_foreign_key(
        "fk_interaction_related_case_id_case",
        "interaction",
        "case",
        ["related_case_id"],
        ["id"],
        source_schema="crm",
        referent_schema="crm",
    )

    op.create_table(
        "case_note",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("case_id", sa.Text, sa.ForeignKey("crm.case.id"), nullable=False),
        sa.Column("author_agent_id", sa.Text, sa.ForeignKey("crm.agent.id")),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        schema="crm",
    )

    op.create_table(
        "ticket",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("case_id", sa.Text, sa.ForeignKey("crm.case.id")),
        sa.Column("customer_id", sa.Text, sa.ForeignKey("crm.customer.id"), nullable=False),
        sa.Column("ticket_type", sa.Text),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("state", sa.Text, nullable=False, server_default="open"),
        sa.Column("priority", sa.Text),
        sa.Column("assigned_to_agent_id", sa.Text, sa.ForeignKey("crm.agent.id")),
        sa.Column("related_order_id", sa.Text),
        sa.Column("related_subscription_id", sa.Text),
        sa.Column("related_service_id", sa.Text),
        sa.Column("sla_due_at", TIMESTAMPTZ),
        sa.Column("resolution_notes", sa.Text),
        sa.Column("opened_at", TIMESTAMPTZ, nullable=False),
        sa.Column("resolved_at", TIMESTAMPTZ),
        sa.Column("closed_at", TIMESTAMPTZ),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="crm",
    )

    # Add deferred FK on interaction now that ticket exists
    op.create_foreign_key(
        "fk_interaction_related_ticket_id_ticket",
        "interaction",
        "ticket",
        ["related_ticket_id"],
        ["id"],
        source_schema="crm",
        referent_schema="crm",
    )

    op.create_table(
        "ticket_state_history",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ticket_id", sa.Text, sa.ForeignKey("crm.ticket.id"), nullable=False),
        sa.Column("from_state", sa.Text),
        sa.Column("to_state", sa.Text),
        sa.Column("changed_by_agent_id", sa.Text, sa.ForeignKey("crm.agent.id")),
        sa.Column("reason", sa.Text),
        sa.Column("event_time", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        schema="crm",
    )

    op.create_table(
        "sla_policy",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("ticket_type", sa.Text, nullable=False),
        sa.Column("priority", sa.Text, nullable=False),
        sa.Column("target_resolution_minutes", sa.BigInteger, nullable=False),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="crm",
    )

    # ══════════════════════════════════════════════════════════════════
    # CATALOG — 7 tables
    # ══════════════════════════════════════════════════════════════════

    op.create_table(
        "product_specification",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column("brand", sa.Text),
        sa.Column("lifecycle_status", sa.Text),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="catalog",
    )

    op.create_table(
        "product_offering",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text),
        sa.Column("spec_id", sa.Text, sa.ForeignKey("catalog.product_specification.id")),
        sa.Column("is_bundle", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("is_sellable", sa.Boolean),
        sa.Column("lifecycle_status", sa.Text),
        sa.Column("valid_from", TIMESTAMPTZ),
        sa.Column("valid_to", TIMESTAMPTZ),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="catalog",
    )

    op.create_table(
        "product_offering_price",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("offering_id", sa.Text, sa.ForeignKey("catalog.product_offering.id"), nullable=False),
        sa.Column("price_type", sa.Text, nullable=False),
        sa.Column("recurring_period_length", sa.SmallInteger),
        sa.Column("recurring_period_type", sa.Text),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.Text, nullable=False, server_default="SGD"),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="catalog",
    )

    op.create_table(
        "bundle_allowance",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("offering_id", sa.Text, sa.ForeignKey("catalog.product_offering.id"), nullable=False),
        sa.Column("allowance_type", sa.Text, nullable=False),
        sa.Column("quantity", sa.BigInteger, nullable=False),
        sa.Column("unit", sa.Text, nullable=False),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="catalog",
    )

    op.create_table(
        "vas_offering",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text),
        sa.Column("price_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.Text, nullable=False, server_default="SGD"),
        sa.Column("allowance_type", sa.Text),
        sa.Column("allowance_quantity", sa.BigInteger),
        sa.Column("allowance_unit", sa.Text),
        sa.Column("expiry_hours", sa.SmallInteger),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="catalog",
    )

    op.create_table(
        "service_specification",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text),
        sa.Column("type", sa.Text),
        sa.Column("parameters", JSONB),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="catalog",
    )

    op.create_table(
        "product_to_service_mapping",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("offering_id", sa.Text, sa.ForeignKey("catalog.product_offering.id"), nullable=False),
        sa.Column("cfs_spec_id", sa.Text, sa.ForeignKey("catalog.service_specification.id"), nullable=False),
        sa.Column("rfs_spec_ids", sa.ARRAY(sa.Text), nullable=False),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="catalog",
    )

    # ══════════════════════════════════════════════════════════════════
    # INVENTORY — 2 tables
    # ══════════════════════════════════════════════════════════════════

    op.create_table(
        "msisdn_pool",
        sa.Column("msisdn", sa.Text, primary_key=True),
        sa.Column("status", sa.Text, nullable=False, server_default="available"),
        sa.Column("reserved_at", TIMESTAMPTZ),
        sa.Column("assigned_to_subscription_id", sa.Text),
        sa.Column("quarantine_until", TIMESTAMPTZ),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="inventory",
    )

    op.create_table(
        "esim_profile",
        sa.Column("iccid", sa.Text, primary_key=True),
        sa.Column("imsi", sa.Text, unique=True, nullable=False),
        sa.Column("ki_ref", sa.Text, nullable=False),
        sa.Column("profile_state", sa.Text, nullable=False, server_default="available"),
        sa.Column("smdp_server", sa.Text),
        sa.Column("matching_id", sa.Text, unique=True),
        sa.Column("activation_code", sa.Text),
        sa.Column("assigned_msisdn", sa.Text, sa.ForeignKey("inventory.msisdn_pool.msisdn")),
        sa.Column("assigned_to_subscription_id", sa.Text),
        sa.Column("reserved_at", TIMESTAMPTZ),
        sa.Column("downloaded_at", TIMESTAMPTZ),
        sa.Column("activated_at", TIMESTAMPTZ),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="inventory",
    )

    # ══════════════════════════════════════════════════════════════════
    # PAYMENT — 2 tables
    # ══════════════════════════════════════════════════════════════════

    op.create_table(
        "payment_method",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("customer_id", sa.Text, nullable=False),
        sa.Column("type", sa.Text, nullable=False, server_default="card"),
        sa.Column("token", sa.Text, unique=True, nullable=False),
        sa.Column("last4", sa.Text, nullable=False),
        sa.Column("brand", sa.Text),
        sa.Column("exp_month", sa.SmallInteger, nullable=False),
        sa.Column("exp_year", sa.SmallInteger, nullable=False),
        sa.Column("is_default", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="payment",
    )

    op.create_table(
        "payment_attempt",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("customer_id", sa.Text, nullable=False),
        sa.Column("payment_method_id", sa.Text, sa.ForeignKey("payment.payment_method.id"), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.Text, nullable=False, server_default="SGD"),
        sa.Column("purpose", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("gateway_ref", sa.Text),
        sa.Column("decline_reason", sa.Text),
        sa.Column("attempted_at", TIMESTAMPTZ, nullable=False),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="payment",
    )

    # ══════════════════════════════════════════════════════════════════
    # ORDER_MGMT (COM) — 3 tables
    # ══════════════════════════════════════════════════════════════════

    op.create_table(
        "product_order",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("customer_id", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="acknowledged"),
        sa.Column("order_date", TIMESTAMPTZ),
        sa.Column("requested_completion_date", TIMESTAMPTZ),
        sa.Column("completed_date", TIMESTAMPTZ),
        sa.Column("msisdn_preference", sa.Text),
        sa.Column("notes", sa.Text),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="order_mgmt",
    )

    op.create_table(
        "order_item",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("order_id", sa.Text, sa.ForeignKey("order_mgmt.product_order.id"), nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("offering_id", sa.Text, nullable=False),
        sa.Column("state", sa.Text),
        sa.Column("target_subscription_id", sa.Text),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="order_mgmt",
    )

    op.create_table(
        "order_state_history",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("order_id", sa.Text, sa.ForeignKey("order_mgmt.product_order.id"), nullable=False),
        sa.Column("from_state", sa.Text),
        sa.Column("to_state", sa.Text),
        sa.Column("changed_by", sa.Text),
        sa.Column("reason", sa.Text),
        sa.Column("event_time", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        schema="order_mgmt",
    )

    # ══════════════════════════════════════════════════════════════════
    # SERVICE_INVENTORY (SOM) — 4 tables
    # ══════════════════════════════════════════════════════════════════

    op.create_table(
        "service_order",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("commercial_order_id", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="acknowledged"),
        sa.Column("started_at", TIMESTAMPTZ),
        sa.Column("completed_at", TIMESTAMPTZ),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="service_inventory",
    )

    op.create_table(
        "service_order_item",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("service_order_id", sa.Text, sa.ForeignKey("service_inventory.service_order.id"), nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("service_spec_id", sa.Text, nullable=False),
        sa.Column("target_service_id", sa.Text),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="service_inventory",
    )

    op.create_table(
        "service",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("subscription_id", sa.Text),
        sa.Column("spec_id", sa.Text, nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("parent_service_id", sa.Text, sa.ForeignKey("service_inventory.service.id")),
        sa.Column("state", sa.Text, nullable=False, server_default="feasibility_checked"),
        sa.Column("characteristics", JSONB),
        sa.Column("activated_at", TIMESTAMPTZ),
        sa.Column("terminated_at", TIMESTAMPTZ),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="service_inventory",
    )

    op.create_table(
        "service_state_history",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("service_id", sa.Text, sa.ForeignKey("service_inventory.service.id"), nullable=False),
        sa.Column("from_state", sa.Text),
        sa.Column("to_state", sa.Text),
        sa.Column("changed_by", sa.Text),
        sa.Column("reason", sa.Text),
        sa.Column("event_time", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        schema="service_inventory",
    )

    # ══════════════════════════════════════════════════════════════════
    # PROVISIONING — 2 tables
    # ══════════════════════════════════════════════════════════════════

    op.create_table(
        "provisioning_task",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("service_id", sa.Text, nullable=False),
        sa.Column("task_type", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="pending"),
        sa.Column("attempts", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.SmallInteger, nullable=False, server_default="3"),
        sa.Column("payload", JSONB),
        sa.Column("last_error", sa.Text),
        sa.Column("started_at", TIMESTAMPTZ),
        sa.Column("completed_at", TIMESTAMPTZ),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="provisioning",
    )

    op.create_table(
        "fault_injection",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("task_type", sa.Text, nullable=False),
        sa.Column("fault_type", sa.Text, nullable=False),
        sa.Column("probability", sa.Numeric(3, 2), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="provisioning",
    )

    # ══════════════════════════════════════════════════════════════════
    # SUBSCRIPTION — 4 tables
    # ══════════════════════════════════════════════════════════════════

    op.create_table(
        "subscription",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("customer_id", sa.Text, nullable=False),
        sa.Column("offering_id", sa.Text, nullable=False),
        sa.Column("msisdn", sa.Text, unique=True, nullable=False),
        sa.Column("iccid", sa.Text, unique=True, nullable=False),
        sa.Column("cfs_service_id", sa.Text),
        sa.Column("state", sa.Text, nullable=False, server_default="pending"),
        sa.Column("state_reason", sa.Text),
        sa.Column("activated_at", TIMESTAMPTZ),
        sa.Column("current_period_start", TIMESTAMPTZ),
        sa.Column("current_period_end", TIMESTAMPTZ),
        sa.Column("next_renewal_at", TIMESTAMPTZ),
        sa.Column("terminated_at", TIMESTAMPTZ),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="subscription",
    )

    op.create_table(
        "bundle_balance",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("subscription_id", sa.Text, sa.ForeignKey("subscription.subscription.id"), nullable=False),
        sa.Column("allowance_type", sa.Text, nullable=False),
        sa.Column("total", sa.BigInteger, nullable=False),
        sa.Column("consumed", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column(
            "remaining",
            sa.BigInteger,
            sa.Computed("total - consumed", persisted=True),
        ),
        sa.Column("unit", sa.Text, nullable=False),
        sa.Column("period_start", TIMESTAMPTZ),
        sa.Column("period_end", TIMESTAMPTZ),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="subscription",
    )

    op.create_table(
        "vas_purchase",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("subscription_id", sa.Text, sa.ForeignKey("subscription.subscription.id"), nullable=False),
        sa.Column("vas_offering_id", sa.Text, nullable=False),
        sa.Column("payment_attempt_id", sa.Text),
        sa.Column("applied_at", TIMESTAMPTZ),
        sa.Column("expires_at", TIMESTAMPTZ),
        sa.Column("allowance_added", sa.BigInteger, nullable=False),
        sa.Column("allowance_type", sa.Text, nullable=False),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="subscription",
    )

    op.create_table(
        "subscription_state_history",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("subscription_id", sa.Text, sa.ForeignKey("subscription.subscription.id"), nullable=False),
        sa.Column("from_state", sa.Text),
        sa.Column("to_state", sa.Text),
        sa.Column("changed_by", sa.Text),
        sa.Column("reason", sa.Text),
        sa.Column("event_time", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        schema="subscription",
    )

    # ══════════════════════════════════════════════════════════════════
    # MEDIATION — 1 table
    # ══════════════════════════════════════════════════════════════════

    op.create_table(
        "usage_event",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("msisdn", sa.Text, nullable=False),
        sa.Column("subscription_id", sa.Text),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("event_time", TIMESTAMPTZ, nullable=False),
        sa.Column("quantity", sa.BigInteger, nullable=False),
        sa.Column("unit", sa.Text, nullable=False),
        sa.Column("source", sa.Text),
        sa.Column("raw_cdr_ref", sa.Text),
        sa.Column("processed", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("processing_error", sa.Text),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="mediation",
    )

    # ══════════════════════════════════════════════════════════════════
    # BILLING — 2 tables
    # ══════════════════════════════════════════════════════════════════

    op.create_table(
        "billing_account",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("customer_id", sa.Text, unique=True, nullable=False),
        sa.Column("payment_method_id", sa.Text),
        sa.Column("currency", sa.Text, nullable=False, server_default="SGD"),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="billing",
    )

    op.create_table(
        "customer_bill",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("billing_account_id", sa.Text, sa.ForeignKey("billing.billing_account.id"), nullable=False),
        sa.Column("subscription_id", sa.Text),
        sa.Column("period_start", TIMESTAMPTZ),
        sa.Column("period_end", TIMESTAMPTZ),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.Text, nullable=False, server_default="SGD"),
        sa.Column("status", sa.Text, nullable=False, server_default="issued"),
        sa.Column("payment_attempt_id", sa.Text),
        sa.Column("issued_at", TIMESTAMPTZ),
        sa.Column("paid_at", TIMESTAMPTZ),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        schema="billing",
    )

    # ══════════════════════════════════════════════════════════════════
    # AUDIT — 1 table
    # ══════════════════════════════════════════════════════════════════

    op.create_table(
        "domain_event",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("event_id", UUID(as_uuid=True), unique=True, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("aggregate_type", sa.Text, nullable=False),
        sa.Column("aggregate_id", sa.Text, nullable=False),
        sa.Column("occurred_at", TIMESTAMPTZ, nullable=False),
        sa.Column("trace_id", sa.Text),
        sa.Column("actor", sa.Text),
        sa.Column("channel", sa.Text),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column("payload", JSONB),
        sa.Column("schema_version", sa.SmallInteger, nullable=False, server_default="1"),
        sa.Column("published_to_mq", sa.Boolean, nullable=False, server_default="false"),
        schema="audit",
    )

    op.create_index(
        "ix_domain_event_aggregate_replay",
        "domain_event",
        ["aggregate_type", "aggregate_id", "occurred_at"],
        schema="audit",
    )
    op.create_index(
        "ix_domain_event_type_time",
        "domain_event",
        ["event_type", "occurred_at"],
        schema="audit",
    )
    op.create_index(
        "ix_domain_event_unpublished",
        "domain_event",
        ["published_to_mq"],
        schema="audit",
        postgresql_where=sa.text("NOT published_to_mq"),
    )


def downgrade() -> None:
    # Drop ONLY the 11 BSS schemas by name. Never public. Never campaignos.
    op.execute("DROP SCHEMA IF EXISTS crm CASCADE")
    op.execute("DROP SCHEMA IF EXISTS catalog CASCADE")
    op.execute("DROP SCHEMA IF EXISTS inventory CASCADE")
    op.execute("DROP SCHEMA IF EXISTS payment CASCADE")
    op.execute("DROP SCHEMA IF EXISTS order_mgmt CASCADE")
    op.execute("DROP SCHEMA IF EXISTS service_inventory CASCADE")
    op.execute("DROP SCHEMA IF EXISTS provisioning CASCADE")
    op.execute("DROP SCHEMA IF EXISTS subscription CASCADE")
    op.execute("DROP SCHEMA IF EXISTS mediation CASCADE")
    op.execute("DROP SCHEMA IF EXISTS billing CASCADE")
    op.execute("DROP SCHEMA IF EXISTS audit CASCADE")
