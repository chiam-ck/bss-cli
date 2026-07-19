-- BSS-CLI schema baseline (Phase 8 — Alembic freeze → sqlx baseline).
--
-- This is the FROZEN end-state of the Python Alembic tree
-- (packages/bss-models/alembic, 32 migrations) captured via
--   pg_dump --schema-only --no-owner --no-privileges (PostgreSQL 16),
-- with public.alembic_version excluded and the psql \restrict/\unrestrict
-- meta-commands + the `set_config('search_path','')` reset stripped (all objects
-- are already schema-qualified, and the empty search_path otherwise hides sqlx's
-- own _sqlx_migrations ledger from its post-migration bookkeeping). sqlx runs SQL,
-- not psql. It is the go-forward schema
-- source for the all-Rust stack; new schema changes land as 000N_*.sql siblings,
-- applied by `bss admin migrate` (sqlx migrator). Do not hand-edit.
--
-- Fresh install: the Postgres instance must provide the `vector` extension
-- (pgvector) — the knowledge schema depends on it. See
-- docs/runbooks/rust-schema-baseline.md for fresh-vs-existing install paths.
--
--
-- PostgreSQL database dump
--


-- Dumped from database version 16.14 (Debian 16.14-1.pgdg12+1)
-- Dumped by pg_dump version 16.14 (Debian 16.14-1.pgdg13+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: audit; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA audit;


--
-- Name: billing; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA billing;


--
-- Name: catalog; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA catalog;


--
-- Name: cockpit; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA cockpit;


--
-- Name: crm; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA crm;


--
-- Name: integrations; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA integrations;


--
-- Name: inventory; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA inventory;


--
-- Name: knowledge; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA knowledge;


--
-- Name: mediation; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA mediation;


--
-- Name: order_mgmt; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA order_mgmt;


--
-- Name: payment; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA payment;


--
-- Name: portal_auth; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA portal_auth;


--
-- Name: provisioning; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA provisioning;


--
-- Name: service_inventory; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA service_inventory;


--
-- Name: subscription; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA subscription;


--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: chat_transcript; Type: TABLE; Schema: audit; Owner: -
--

CREATE TABLE audit.chat_transcript (
    hash text NOT NULL,
    customer_id text NOT NULL,
    body text NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: chat_usage; Type: TABLE; Schema: audit; Owner: -
--

CREATE TABLE audit.chat_usage (
    customer_id text NOT NULL,
    period_yyyymm integer NOT NULL,
    requests_count integer DEFAULT 0 NOT NULL,
    cost_cents integer DEFAULT 0 NOT NULL,
    last_updated timestamp with time zone DEFAULT now() NOT NULL,
    citations jsonb DEFAULT '[]'::jsonb NOT NULL
);


--
-- Name: domain_event; Type: TABLE; Schema: audit; Owner: -
--

CREATE TABLE audit.domain_event (
    id bigint NOT NULL,
    event_id uuid NOT NULL,
    event_type text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id text NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    trace_id text,
    actor text,
    channel text,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    payload jsonb,
    schema_version smallint DEFAULT '1'::smallint NOT NULL,
    published_to_mq boolean DEFAULT false NOT NULL,
    service_identity text DEFAULT 'default'::text NOT NULL,
    published_attempts integer DEFAULT '0'::smallint NOT NULL,
    last_publish_error text
);


--
-- Name: domain_event_id_seq; Type: SEQUENCE; Schema: audit; Owner: -
--

CREATE SEQUENCE audit.domain_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: domain_event_id_seq; Type: SEQUENCE OWNED BY; Schema: audit; Owner: -
--

ALTER SEQUENCE audit.domain_event_id_seq OWNED BY audit.domain_event.id;


--
-- Name: billing_account; Type: TABLE; Schema: billing; Owner: -
--

CREATE TABLE billing.billing_account (
    id text NOT NULL,
    customer_id text NOT NULL,
    payment_method_id text,
    currency text DEFAULT 'SGD'::text NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: customer_bill; Type: TABLE; Schema: billing; Owner: -
--

CREATE TABLE billing.customer_bill (
    id text NOT NULL,
    billing_account_id text NOT NULL,
    subscription_id text,
    period_start timestamp with time zone,
    period_end timestamp with time zone,
    amount numeric(12,2) NOT NULL,
    currency text DEFAULT 'SGD'::text NOT NULL,
    status text DEFAULT 'issued'::text NOT NULL,
    payment_attempt_id text,
    issued_at timestamp with time zone,
    paid_at timestamp with time zone,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: bundle_allowance; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.bundle_allowance (
    id text NOT NULL,
    offering_id text NOT NULL,
    allowance_type text NOT NULL,
    quantity bigint NOT NULL,
    unit text NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: product_offering; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.product_offering (
    id text NOT NULL,
    name text,
    spec_id text,
    is_bundle boolean DEFAULT true NOT NULL,
    is_sellable boolean,
    lifecycle_status text,
    valid_from timestamp with time zone,
    valid_to timestamp with time zone,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: product_offering_price; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.product_offering_price (
    id text NOT NULL,
    offering_id text NOT NULL,
    price_type text NOT NULL,
    recurring_period_length smallint,
    recurring_period_type text,
    amount numeric(12,2) NOT NULL,
    currency text DEFAULT 'SGD'::text NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    valid_from timestamp with time zone,
    valid_to timestamp with time zone
);


--
-- Name: product_specification; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.product_specification (
    id text NOT NULL,
    name text,
    description text,
    brand text,
    lifecycle_status text,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: product_to_service_mapping; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.product_to_service_mapping (
    id bigint NOT NULL,
    offering_id text NOT NULL,
    cfs_spec_id text NOT NULL,
    rfs_spec_ids text[] NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: product_to_service_mapping_id_seq; Type: SEQUENCE; Schema: catalog; Owner: -
--

CREATE SEQUENCE catalog.product_to_service_mapping_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: product_to_service_mapping_id_seq; Type: SEQUENCE OWNED BY; Schema: catalog; Owner: -
--

ALTER SEQUENCE catalog.product_to_service_mapping_id_seq OWNED BY catalog.product_to_service_mapping.id;


--
-- Name: promotion; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.promotion (
    id text NOT NULL,
    code text,
    offer_definition_id text,
    discount_type text NOT NULL,
    discount_value numeric(12,2) NOT NULL,
    currency text DEFAULT 'SGD'::text NOT NULL,
    applicable_offering_ids text[],
    duration_kind text NOT NULL,
    periods_total smallint,
    valid_from timestamp with time zone,
    valid_to timestamp with time zone,
    state text NOT NULL,
    created_by text NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    audience text DEFAULT 'public'::text NOT NULL,
    name text,
    CONSTRAINT ck_promotion_ck_promotion_audience CHECK ((audience = ANY (ARRAY['public'::text, 'targeted'::text]))),
    CONSTRAINT ck_promotion_ck_promotion_discount_type CHECK ((discount_type = ANY (ARRAY['percent'::text, 'absolute'::text]))),
    CONSTRAINT ck_promotion_ck_promotion_duration_kind CHECK ((duration_kind = ANY (ARRAY['single'::text, 'multi'::text, 'perpetual'::text]))),
    CONSTRAINT ck_promotion_ck_promotion_periods_total_matches_kind CHECK (((duration_kind = 'multi'::text) = (periods_total IS NOT NULL))),
    CONSTRAINT ck_promotion_ck_promotion_state CHECK ((state = ANY (ARRAY['pending_link'::text, 'active'::text, 'retired'::text, 'exhausted'::text])))
);


--
-- Name: promotion_eligibility; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.promotion_eligibility (
    id bigint NOT NULL,
    promotion_id text NOT NULL,
    customer_id text NOT NULL,
    created_by text NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    loyalty_offer_id text
);


--
-- Name: promotion_eligibility_id_seq; Type: SEQUENCE; Schema: catalog; Owner: -
--

CREATE SEQUENCE catalog.promotion_eligibility_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: promotion_eligibility_id_seq; Type: SEQUENCE OWNED BY; Schema: catalog; Owner: -
--

ALTER SEQUENCE catalog.promotion_eligibility_id_seq OWNED BY catalog.promotion_eligibility.id;


--
-- Name: service_specification; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.service_specification (
    id text NOT NULL,
    name text,
    type text,
    parameters jsonb,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: vas_offering; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.vas_offering (
    id text NOT NULL,
    name text,
    price_amount numeric(12,2) NOT NULL,
    currency text DEFAULT 'SGD'::text NOT NULL,
    allowance_type text,
    allowance_quantity bigint,
    allowance_unit text,
    expiry_hours smallint,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: message; Type: TABLE; Schema: cockpit; Owner: -
--

CREATE TABLE cockpit.message (
    id bigint NOT NULL,
    session_id text NOT NULL,
    role text NOT NULL,
    content text NOT NULL,
    tool_calls_json json,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL
);


--
-- Name: message_id_seq; Type: SEQUENCE; Schema: cockpit; Owner: -
--

CREATE SEQUENCE cockpit.message_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: message_id_seq; Type: SEQUENCE OWNED BY; Schema: cockpit; Owner: -
--

ALTER SEQUENCE cockpit.message_id_seq OWNED BY cockpit.message.id;


--
-- Name: pending_destructive; Type: TABLE; Schema: cockpit; Owner: -
--

CREATE TABLE cockpit.pending_destructive (
    session_id text NOT NULL,
    proposed_at timestamp with time zone DEFAULT now() NOT NULL,
    tool_name text NOT NULL,
    tool_args_json json NOT NULL,
    proposal_message_id bigint NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL
);


--
-- Name: session; Type: TABLE; Schema: cockpit; Owner: -
--

CREATE TABLE cockpit.session (
    id text NOT NULL,
    actor text NOT NULL,
    customer_focus text,
    allow_destructive boolean DEFAULT false NOT NULL,
    state text DEFAULT 'active'::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    last_active_at timestamp with time zone DEFAULT now() NOT NULL,
    label text,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL
);


--
-- Name: agent; Type: TABLE; Schema: crm; Owner: -
--

CREATE TABLE crm.agent (
    id text NOT NULL,
    name text NOT NULL,
    email text,
    role text,
    status text DEFAULT 'active'::text NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: case; Type: TABLE; Schema: crm; Owner: -
--

CREATE TABLE crm."case" (
    id text NOT NULL,
    customer_id text NOT NULL,
    subject text NOT NULL,
    description text,
    state text DEFAULT 'open'::text NOT NULL,
    priority text,
    category text,
    resolution_code text,
    opened_by_agent_id text,
    opened_at timestamp with time zone NOT NULL,
    closed_at timestamp with time zone,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    chat_transcript_hash text
);


--
-- Name: case_note; Type: TABLE; Schema: crm; Owner: -
--

CREATE TABLE crm.case_note (
    id text NOT NULL,
    case_id text NOT NULL,
    author_agent_id text,
    body text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL
);


--
-- Name: contact_medium; Type: TABLE; Schema: crm; Owner: -
--

CREATE TABLE crm.contact_medium (
    id text NOT NULL,
    party_id text NOT NULL,
    medium_type text NOT NULL,
    value text NOT NULL,
    is_primary boolean DEFAULT false NOT NULL,
    valid_from timestamp with time zone,
    valid_to timestamp with time zone,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: customer; Type: TABLE; Schema: crm; Owner: -
--

CREATE TABLE crm.customer (
    id text NOT NULL,
    party_id text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    status_reason text,
    customer_since timestamp with time zone,
    kyc_status text DEFAULT 'not_verified'::text NOT NULL,
    kyc_verified_at timestamp with time zone,
    kyc_verification_method text,
    kyc_reference text,
    kyc_expires_at timestamp with time zone,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: customer_identity; Type: TABLE; Schema: crm; Owner: -
--

CREATE TABLE crm.customer_identity (
    customer_id text NOT NULL,
    document_type text NOT NULL,
    document_number_hash text NOT NULL,
    document_country text NOT NULL,
    date_of_birth date NOT NULL,
    nationality text,
    verified_by text,
    attestation_payload jsonb,
    verified_at timestamp with time zone NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    document_number_last4 character varying(4),
    corroboration_id uuid
);


--
-- Name: individual; Type: TABLE; Schema: crm; Owner: -
--

CREATE TABLE crm.individual (
    party_id text NOT NULL,
    given_name text NOT NULL,
    family_name text NOT NULL,
    date_of_birth date,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: interaction; Type: TABLE; Schema: crm; Owner: -
--

CREATE TABLE crm.interaction (
    id text NOT NULL,
    customer_id text NOT NULL,
    channel text,
    direction text,
    summary text NOT NULL,
    body text,
    agent_id text,
    related_case_id text,
    related_ticket_id text,
    occurred_at timestamp with time zone NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: party; Type: TABLE; Schema: crm; Owner: -
--

CREATE TABLE crm.party (
    id text NOT NULL,
    party_type text NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: port_request; Type: TABLE; Schema: crm; Owner: -
--

CREATE TABLE crm.port_request (
    id text NOT NULL,
    direction text NOT NULL,
    donor_carrier text NOT NULL,
    donor_msisdn text NOT NULL,
    target_subscription_id text,
    requested_port_date date NOT NULL,
    state text DEFAULT 'requested'::text NOT NULL,
    rejection_reason text,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_port_request_ck_port_request_direction CHECK ((direction = ANY (ARRAY['port_in'::text, 'port_out'::text]))),
    CONSTRAINT ck_port_request_ck_port_request_state CHECK ((state = ANY (ARRAY['requested'::text, 'validated'::text, 'completed'::text, 'rejected'::text])))
);


--
-- Name: sla_policy; Type: TABLE; Schema: crm; Owner: -
--

CREATE TABLE crm.sla_policy (
    id text NOT NULL,
    ticket_type text NOT NULL,
    priority text NOT NULL,
    target_resolution_minutes bigint NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ticket; Type: TABLE; Schema: crm; Owner: -
--

CREATE TABLE crm.ticket (
    id text NOT NULL,
    case_id text,
    customer_id text NOT NULL,
    ticket_type text,
    subject text NOT NULL,
    description text,
    state text DEFAULT 'open'::text NOT NULL,
    priority text,
    assigned_to_agent_id text,
    related_order_id text,
    related_subscription_id text,
    related_service_id text,
    sla_due_at timestamp with time zone,
    resolution_notes text,
    opened_at timestamp with time zone NOT NULL,
    resolved_at timestamp with time zone,
    closed_at timestamp with time zone,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ticket_state_history; Type: TABLE; Schema: crm; Owner: -
--

CREATE TABLE crm.ticket_state_history (
    id bigint NOT NULL,
    ticket_id text NOT NULL,
    from_state text,
    to_state text,
    changed_by_agent_id text,
    reason text,
    event_time timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL
);


--
-- Name: ticket_state_history_id_seq; Type: SEQUENCE; Schema: crm; Owner: -
--

CREATE SEQUENCE crm.ticket_state_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ticket_state_history_id_seq; Type: SEQUENCE OWNED BY; Schema: crm; Owner: -
--

ALTER SEQUENCE crm.ticket_state_history_id_seq OWNED BY crm.ticket_state_history.id;


--
-- Name: external_call; Type: TABLE; Schema: integrations; Owner: -
--

CREATE TABLE integrations.external_call (
    id bigint NOT NULL,
    provider text NOT NULL,
    operation text NOT NULL,
    aggregate_type text,
    aggregate_id text,
    success boolean NOT NULL,
    latency_ms integer NOT NULL,
    provider_call_id text,
    error_code text,
    error_message text,
    redacted_payload jsonb,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL
);


--
-- Name: external_call_id_seq; Type: SEQUENCE; Schema: integrations; Owner: -
--

CREATE SEQUENCE integrations.external_call_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: external_call_id_seq; Type: SEQUENCE OWNED BY; Schema: integrations; Owner: -
--

ALTER SEQUENCE integrations.external_call_id_seq OWNED BY integrations.external_call.id;


--
-- Name: kyc_webhook_corroboration; Type: TABLE; Schema: integrations; Owner: -
--

CREATE TABLE integrations.kyc_webhook_corroboration (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    provider text NOT NULL,
    provider_session_id text NOT NULL,
    webhook_event_provider text NOT NULL,
    webhook_event_id text NOT NULL,
    decision_status text NOT NULL,
    decision_body_digest text NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: webhook_event; Type: TABLE; Schema: integrations; Owner: -
--

CREATE TABLE integrations.webhook_event (
    provider text NOT NULL,
    event_id text NOT NULL,
    event_type text NOT NULL,
    body jsonb NOT NULL,
    signature_valid boolean NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    processed_at timestamp with time zone,
    process_outcome text,
    process_error text
);


--
-- Name: esim_profile; Type: TABLE; Schema: inventory; Owner: -
--

CREATE TABLE inventory.esim_profile (
    iccid text NOT NULL,
    imsi text NOT NULL,
    ki_ref text NOT NULL,
    profile_state text DEFAULT 'available'::text NOT NULL,
    smdp_server text,
    matching_id text,
    activation_code text,
    assigned_msisdn text,
    assigned_to_subscription_id text,
    reserved_at timestamp with time zone,
    downloaded_at timestamp with time zone,
    activated_at timestamp with time zone,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: msisdn_pool; Type: TABLE; Schema: inventory; Owner: -
--

CREATE TABLE inventory.msisdn_pool (
    msisdn text NOT NULL,
    status text DEFAULT 'available'::text NOT NULL,
    reserved_at timestamp with time zone,
    assigned_to_subscription_id text,
    quarantine_until timestamp with time zone,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: doc_chunk; Type: TABLE; Schema: knowledge; Owner: -
--

CREATE TABLE knowledge.doc_chunk (
    id text NOT NULL,
    source_path text NOT NULL,
    anchor text NOT NULL,
    heading_path text NOT NULL,
    kind text NOT NULL,
    content text NOT NULL,
    content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english'::regconfig, content)) STORED NOT NULL,
    content_hash text NOT NULL,
    source_mtime timestamp with time zone NOT NULL,
    indexed_at timestamp with time zone DEFAULT now() NOT NULL,
    embedding public.vector(1024)
);


--
-- Name: usage_event; Type: TABLE; Schema: mediation; Owner: -
--

CREATE TABLE mediation.usage_event (
    id text NOT NULL,
    msisdn text NOT NULL,
    subscription_id text,
    event_type text NOT NULL,
    event_time timestamp with time zone NOT NULL,
    quantity bigint NOT NULL,
    unit text NOT NULL,
    source text,
    raw_cdr_ref text,
    processed boolean DEFAULT false NOT NULL,
    processing_error text,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    roaming_indicator boolean DEFAULT false NOT NULL
);


--
-- Name: usage_event_id_seq; Type: SEQUENCE; Schema: mediation; Owner: -
--

CREATE SEQUENCE mediation.usage_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: order_item; Type: TABLE; Schema: order_mgmt; Owner: -
--

CREATE TABLE order_mgmt.order_item (
    id text NOT NULL,
    order_id text NOT NULL,
    action text NOT NULL,
    offering_id text NOT NULL,
    state text,
    target_subscription_id text,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    price_amount numeric(10,2),
    price_currency text,
    price_offering_price_id text,
    discount_code text,
    promo_offer_definition_id text,
    discount_type text,
    discount_value numeric(12,2),
    discount_periods_total smallint,
    promo_offer_id text
);


--
-- Name: order_item_id_seq; Type: SEQUENCE; Schema: order_mgmt; Owner: -
--

CREATE SEQUENCE order_mgmt.order_item_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: order_state_history; Type: TABLE; Schema: order_mgmt; Owner: -
--

CREATE TABLE order_mgmt.order_state_history (
    id bigint NOT NULL,
    order_id text NOT NULL,
    from_state text,
    to_state text,
    changed_by text,
    reason text,
    event_time timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL
);


--
-- Name: order_state_history_id_seq; Type: SEQUENCE; Schema: order_mgmt; Owner: -
--

CREATE SEQUENCE order_mgmt.order_state_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: order_state_history_id_seq; Type: SEQUENCE OWNED BY; Schema: order_mgmt; Owner: -
--

ALTER SEQUENCE order_mgmt.order_state_history_id_seq OWNED BY order_mgmt.order_state_history.id;


--
-- Name: processed_event; Type: TABLE; Schema: order_mgmt; Owner: -
--

CREATE TABLE order_mgmt.processed_event (
    event_id uuid NOT NULL,
    consumer text NOT NULL,
    processed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: product_order; Type: TABLE; Schema: order_mgmt; Owner: -
--

CREATE TABLE order_mgmt.product_order (
    id text NOT NULL,
    customer_id text NOT NULL,
    state text DEFAULT 'acknowledged'::text NOT NULL,
    order_date timestamp with time zone,
    requested_completion_date timestamp with time zone,
    completed_date timestamp with time zone,
    msisdn_preference text,
    notes text,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    stuck_flagged_at timestamp with time zone
);


--
-- Name: product_order_id_seq; Type: SEQUENCE; Schema: order_mgmt; Owner: -
--

CREATE SEQUENCE order_mgmt.product_order_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: customer; Type: TABLE; Schema: payment; Owner: -
--

CREATE TABLE payment.customer (
    id text NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    customer_external_ref text,
    customer_external_ref_provider text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: payment_attempt; Type: TABLE; Schema: payment; Owner: -
--

CREATE TABLE payment.payment_attempt (
    id text NOT NULL,
    customer_id text NOT NULL,
    payment_method_id text NOT NULL,
    amount numeric(12,2) NOT NULL,
    currency text DEFAULT 'SGD'::text NOT NULL,
    purpose text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    gateway_ref text,
    decline_reason text,
    attempted_at timestamp with time zone NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    provider_call_id text,
    decline_code text,
    idempotency_key text
);


--
-- Name: payment_attempt_id_seq; Type: SEQUENCE; Schema: payment; Owner: -
--

CREATE SEQUENCE payment.payment_attempt_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: payment_method; Type: TABLE; Schema: payment; Owner: -
--

CREATE TABLE payment.payment_method (
    id text NOT NULL,
    customer_id text NOT NULL,
    type text DEFAULT 'card'::text NOT NULL,
    token text NOT NULL,
    last4 text NOT NULL,
    brand text,
    exp_month smallint NOT NULL,
    exp_year smallint NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    token_provider text DEFAULT 'mock'::text NOT NULL
);


--
-- Name: payment_method_id_seq; Type: SEQUENCE; Schema: payment; Owner: -
--

CREATE SEQUENCE payment.payment_method_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: email_change_pending; Type: TABLE; Schema: portal_auth; Owner: -
--

CREATE TABLE portal_auth.email_change_pending (
    id text NOT NULL,
    identity_id text NOT NULL,
    new_email text NOT NULL,
    code_hash text NOT NULL,
    issued_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    status text DEFAULT 'pending'::text NOT NULL,
    ip text,
    user_agent text,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL
);


--
-- Name: identity; Type: TABLE; Schema: portal_auth; Owner: -
--

CREATE TABLE portal_auth.identity (
    id text NOT NULL,
    email text NOT NULL,
    customer_id text,
    email_verified_at timestamp with time zone,
    status text DEFAULT 'unverified'::text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    last_login_at timestamp with time zone,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL
);


--
-- Name: login_attempt; Type: TABLE; Schema: portal_auth; Owner: -
--

CREATE TABLE portal_auth.login_attempt (
    id bigint NOT NULL,
    email text,
    ip text,
    ts timestamp with time zone NOT NULL,
    outcome text NOT NULL,
    stage text,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL
);


--
-- Name: login_attempt_id_seq; Type: SEQUENCE; Schema: portal_auth; Owner: -
--

CREATE SEQUENCE portal_auth.login_attempt_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: login_attempt_id_seq; Type: SEQUENCE OWNED BY; Schema: portal_auth; Owner: -
--

ALTER SEQUENCE portal_auth.login_attempt_id_seq OWNED BY portal_auth.login_attempt.id;


--
-- Name: login_token; Type: TABLE; Schema: portal_auth; Owner: -
--

CREATE TABLE portal_auth.login_token (
    id text NOT NULL,
    identity_id text NOT NULL,
    kind text NOT NULL,
    code_hash text NOT NULL,
    action_label text,
    issued_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    ip text,
    user_agent text,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL
);


--
-- Name: portal_action; Type: TABLE; Schema: portal_auth; Owner: -
--

CREATE TABLE portal_auth.portal_action (
    id bigint NOT NULL,
    ts timestamp with time zone NOT NULL,
    customer_id text,
    identity_id text,
    action text NOT NULL,
    route text NOT NULL,
    method text NOT NULL,
    success boolean NOT NULL,
    error_rule text,
    step_up_consumed boolean DEFAULT false NOT NULL,
    ip text,
    user_agent text,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL
);


--
-- Name: portal_action_id_seq; Type: SEQUENCE; Schema: portal_auth; Owner: -
--

CREATE SEQUENCE portal_auth.portal_action_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: portal_action_id_seq; Type: SEQUENCE OWNED BY; Schema: portal_auth; Owner: -
--

ALTER SEQUENCE portal_auth.portal_action_id_seq OWNED BY portal_auth.portal_action.id;


--
-- Name: session; Type: TABLE; Schema: portal_auth; Owner: -
--

CREATE TABLE portal_auth.session (
    id text NOT NULL,
    identity_id text NOT NULL,
    issued_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    ip text,
    user_agent text,
    revoked_at timestamp with time zone,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL
);


--
-- Name: step_up_pending_action; Type: TABLE; Schema: portal_auth; Owner: -
--

CREATE TABLE portal_auth.step_up_pending_action (
    id text NOT NULL,
    session_id text NOT NULL,
    action_label text NOT NULL,
    target_url text NOT NULL,
    payload_json jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL
);


--
-- Name: fault_injection; Type: TABLE; Schema: provisioning; Owner: -
--

CREATE TABLE provisioning.fault_injection (
    id text NOT NULL,
    task_type text NOT NULL,
    fault_type text NOT NULL,
    probability numeric(3,2) NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: processed_event; Type: TABLE; Schema: provisioning; Owner: -
--

CREATE TABLE provisioning.processed_event (
    event_id uuid NOT NULL,
    consumer text NOT NULL,
    processed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: provisioning_task; Type: TABLE; Schema: provisioning; Owner: -
--

CREATE TABLE provisioning.provisioning_task (
    id text NOT NULL,
    service_id text NOT NULL,
    task_type text NOT NULL,
    state text DEFAULT 'pending'::text NOT NULL,
    attempts smallint DEFAULT '0'::smallint NOT NULL,
    max_attempts smallint DEFAULT '3'::smallint NOT NULL,
    payload jsonb,
    last_error text,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: task_id_seq; Type: SEQUENCE; Schema: provisioning; Owner: -
--

CREATE SEQUENCE provisioning.task_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: processed_event; Type: TABLE; Schema: service_inventory; Owner: -
--

CREATE TABLE service_inventory.processed_event (
    event_id uuid NOT NULL,
    consumer text NOT NULL,
    processed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: service; Type: TABLE; Schema: service_inventory; Owner: -
--

CREATE TABLE service_inventory.service (
    id text NOT NULL,
    subscription_id text,
    spec_id text NOT NULL,
    type text NOT NULL,
    parent_service_id text,
    state text DEFAULT 'feasibility_checked'::text NOT NULL,
    characteristics jsonb,
    activated_at timestamp with time zone,
    terminated_at timestamp with time zone,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: service_id_seq; Type: SEQUENCE; Schema: service_inventory; Owner: -
--

CREATE SEQUENCE service_inventory.service_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: service_order; Type: TABLE; Schema: service_inventory; Owner: -
--

CREATE TABLE service_inventory.service_order (
    id text NOT NULL,
    commercial_order_id text NOT NULL,
    state text DEFAULT 'acknowledged'::text NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: service_order_id_seq; Type: SEQUENCE; Schema: service_inventory; Owner: -
--

CREATE SEQUENCE service_inventory.service_order_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: service_order_item; Type: TABLE; Schema: service_inventory; Owner: -
--

CREATE TABLE service_inventory.service_order_item (
    id text NOT NULL,
    service_order_id text NOT NULL,
    action text NOT NULL,
    service_spec_id text NOT NULL,
    target_service_id text,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: service_order_item_id_seq; Type: SEQUENCE; Schema: service_inventory; Owner: -
--

CREATE SEQUENCE service_inventory.service_order_item_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: service_state_history; Type: TABLE; Schema: service_inventory; Owner: -
--

CREATE TABLE service_inventory.service_state_history (
    id bigint NOT NULL,
    service_id text NOT NULL,
    from_state text,
    to_state text,
    changed_by text,
    reason text,
    event_time timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL
);


--
-- Name: service_state_history_id_seq; Type: SEQUENCE; Schema: service_inventory; Owner: -
--

CREATE SEQUENCE service_inventory.service_state_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: service_state_history_id_seq; Type: SEQUENCE OWNED BY; Schema: service_inventory; Owner: -
--

ALTER SEQUENCE service_inventory.service_state_history_id_seq OWNED BY service_inventory.service_state_history.id;


--
-- Name: bundle_balance; Type: TABLE; Schema: subscription; Owner: -
--

CREATE TABLE subscription.bundle_balance (
    id text NOT NULL,
    subscription_id text NOT NULL,
    allowance_type text NOT NULL,
    total bigint NOT NULL,
    consumed bigint DEFAULT '0'::bigint NOT NULL,
    remaining bigint GENERATED ALWAYS AS ((total - consumed)) STORED,
    unit text NOT NULL,
    period_start timestamp with time zone,
    period_end timestamp with time zone,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: processed_event; Type: TABLE; Schema: subscription; Owner: -
--

CREATE TABLE subscription.processed_event (
    event_id uuid NOT NULL,
    consumer text NOT NULL,
    processed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: subscription; Type: TABLE; Schema: subscription; Owner: -
--

CREATE TABLE subscription.subscription (
    id text NOT NULL,
    customer_id text NOT NULL,
    offering_id text NOT NULL,
    msisdn text NOT NULL,
    iccid text NOT NULL,
    cfs_service_id text,
    state text DEFAULT 'pending'::text NOT NULL,
    state_reason text,
    activated_at timestamp with time zone,
    current_period_start timestamp with time zone,
    current_period_end timestamp with time zone,
    next_renewal_at timestamp with time zone,
    terminated_at timestamp with time zone,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    price_amount numeric(10,2) NOT NULL,
    price_currency text NOT NULL,
    price_offering_price_id text NOT NULL,
    pending_offering_id text,
    pending_offering_price_id text,
    pending_effective_at timestamp with time zone,
    last_renewal_attempted_at timestamp with time zone,
    renewal_reminder_sent_at timestamp with time zone,
    discount_type text,
    discount_value numeric(12,2),
    discount_periods_remaining smallint DEFAULT '0'::smallint NOT NULL,
    promo_code text,
    promo_offer_definition_id text,
    commercial_order_id text
);


--
-- Name: subscription_id_seq; Type: SEQUENCE; Schema: subscription; Owner: -
--

CREATE SEQUENCE subscription.subscription_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: subscription_state_history; Type: TABLE; Schema: subscription; Owner: -
--

CREATE TABLE subscription.subscription_state_history (
    id bigint NOT NULL,
    subscription_id text NOT NULL,
    from_state text,
    to_state text,
    changed_by text,
    reason text,
    event_time timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL
);


--
-- Name: subscription_state_history_id_seq; Type: SEQUENCE; Schema: subscription; Owner: -
--

CREATE SEQUENCE subscription.subscription_state_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: subscription_state_history_id_seq; Type: SEQUENCE OWNED BY; Schema: subscription; Owner: -
--

ALTER SEQUENCE subscription.subscription_state_history_id_seq OWNED BY subscription.subscription_state_history.id;


--
-- Name: vas_purchase; Type: TABLE; Schema: subscription; Owner: -
--

CREATE TABLE subscription.vas_purchase (
    id text NOT NULL,
    subscription_id text NOT NULL,
    vas_offering_id text NOT NULL,
    payment_attempt_id text,
    applied_at timestamp with time zone,
    expires_at timestamp with time zone,
    allowance_added bigint NOT NULL,
    allowance_type text NOT NULL,
    tenant_id text DEFAULT 'DEFAULT'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: domain_event id; Type: DEFAULT; Schema: audit; Owner: -
--

ALTER TABLE ONLY audit.domain_event ALTER COLUMN id SET DEFAULT nextval('audit.domain_event_id_seq'::regclass);


--
-- Name: product_to_service_mapping id; Type: DEFAULT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_to_service_mapping ALTER COLUMN id SET DEFAULT nextval('catalog.product_to_service_mapping_id_seq'::regclass);


--
-- Name: promotion_eligibility id; Type: DEFAULT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.promotion_eligibility ALTER COLUMN id SET DEFAULT nextval('catalog.promotion_eligibility_id_seq'::regclass);


--
-- Name: message id; Type: DEFAULT; Schema: cockpit; Owner: -
--

ALTER TABLE ONLY cockpit.message ALTER COLUMN id SET DEFAULT nextval('cockpit.message_id_seq'::regclass);


--
-- Name: ticket_state_history id; Type: DEFAULT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.ticket_state_history ALTER COLUMN id SET DEFAULT nextval('crm.ticket_state_history_id_seq'::regclass);


--
-- Name: external_call id; Type: DEFAULT; Schema: integrations; Owner: -
--

ALTER TABLE ONLY integrations.external_call ALTER COLUMN id SET DEFAULT nextval('integrations.external_call_id_seq'::regclass);


--
-- Name: order_state_history id; Type: DEFAULT; Schema: order_mgmt; Owner: -
--

ALTER TABLE ONLY order_mgmt.order_state_history ALTER COLUMN id SET DEFAULT nextval('order_mgmt.order_state_history_id_seq'::regclass);


--
-- Name: login_attempt id; Type: DEFAULT; Schema: portal_auth; Owner: -
--

ALTER TABLE ONLY portal_auth.login_attempt ALTER COLUMN id SET DEFAULT nextval('portal_auth.login_attempt_id_seq'::regclass);


--
-- Name: portal_action id; Type: DEFAULT; Schema: portal_auth; Owner: -
--

ALTER TABLE ONLY portal_auth.portal_action ALTER COLUMN id SET DEFAULT nextval('portal_auth.portal_action_id_seq'::regclass);


--
-- Name: service_state_history id; Type: DEFAULT; Schema: service_inventory; Owner: -
--

ALTER TABLE ONLY service_inventory.service_state_history ALTER COLUMN id SET DEFAULT nextval('service_inventory.service_state_history_id_seq'::regclass);


--
-- Name: subscription_state_history id; Type: DEFAULT; Schema: subscription; Owner: -
--

ALTER TABLE ONLY subscription.subscription_state_history ALTER COLUMN id SET DEFAULT nextval('subscription.subscription_state_history_id_seq'::regclass);


--
-- Name: chat_transcript pk_chat_transcript; Type: CONSTRAINT; Schema: audit; Owner: -
--

ALTER TABLE ONLY audit.chat_transcript
    ADD CONSTRAINT pk_chat_transcript PRIMARY KEY (hash);


--
-- Name: chat_usage pk_chat_usage; Type: CONSTRAINT; Schema: audit; Owner: -
--

ALTER TABLE ONLY audit.chat_usage
    ADD CONSTRAINT pk_chat_usage PRIMARY KEY (customer_id, period_yyyymm);


--
-- Name: domain_event pk_domain_event; Type: CONSTRAINT; Schema: audit; Owner: -
--

ALTER TABLE ONLY audit.domain_event
    ADD CONSTRAINT pk_domain_event PRIMARY KEY (id);


--
-- Name: domain_event uq_domain_event_event_id; Type: CONSTRAINT; Schema: audit; Owner: -
--

ALTER TABLE ONLY audit.domain_event
    ADD CONSTRAINT uq_domain_event_event_id UNIQUE (event_id);


--
-- Name: billing_account pk_billing_account; Type: CONSTRAINT; Schema: billing; Owner: -
--

ALTER TABLE ONLY billing.billing_account
    ADD CONSTRAINT pk_billing_account PRIMARY KEY (id);


--
-- Name: customer_bill pk_customer_bill; Type: CONSTRAINT; Schema: billing; Owner: -
--

ALTER TABLE ONLY billing.customer_bill
    ADD CONSTRAINT pk_customer_bill PRIMARY KEY (id);


--
-- Name: billing_account uq_billing_account_customer_id; Type: CONSTRAINT; Schema: billing; Owner: -
--

ALTER TABLE ONLY billing.billing_account
    ADD CONSTRAINT uq_billing_account_customer_id UNIQUE (customer_id);


--
-- Name: bundle_allowance pk_bundle_allowance; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.bundle_allowance
    ADD CONSTRAINT pk_bundle_allowance PRIMARY KEY (id);


--
-- Name: product_offering pk_product_offering; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_offering
    ADD CONSTRAINT pk_product_offering PRIMARY KEY (id);


--
-- Name: product_offering_price pk_product_offering_price; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_offering_price
    ADD CONSTRAINT pk_product_offering_price PRIMARY KEY (id);


--
-- Name: product_specification pk_product_specification; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_specification
    ADD CONSTRAINT pk_product_specification PRIMARY KEY (id);


--
-- Name: product_to_service_mapping pk_product_to_service_mapping; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_to_service_mapping
    ADD CONSTRAINT pk_product_to_service_mapping PRIMARY KEY (id);


--
-- Name: promotion pk_promotion; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.promotion
    ADD CONSTRAINT pk_promotion PRIMARY KEY (id);


--
-- Name: promotion_eligibility pk_promotion_eligibility; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.promotion_eligibility
    ADD CONSTRAINT pk_promotion_eligibility PRIMARY KEY (id);


--
-- Name: service_specification pk_service_specification; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.service_specification
    ADD CONSTRAINT pk_service_specification PRIMARY KEY (id);


--
-- Name: vas_offering pk_vas_offering; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.vas_offering
    ADD CONSTRAINT pk_vas_offering PRIMARY KEY (id);


--
-- Name: message pk_message; Type: CONSTRAINT; Schema: cockpit; Owner: -
--

ALTER TABLE ONLY cockpit.message
    ADD CONSTRAINT pk_message PRIMARY KEY (id);


--
-- Name: pending_destructive pk_pending_destructive; Type: CONSTRAINT; Schema: cockpit; Owner: -
--

ALTER TABLE ONLY cockpit.pending_destructive
    ADD CONSTRAINT pk_pending_destructive PRIMARY KEY (session_id);


--
-- Name: session pk_session; Type: CONSTRAINT; Schema: cockpit; Owner: -
--

ALTER TABLE ONLY cockpit.session
    ADD CONSTRAINT pk_session PRIMARY KEY (id);


--
-- Name: agent pk_agent; Type: CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.agent
    ADD CONSTRAINT pk_agent PRIMARY KEY (id);


--
-- Name: case pk_case; Type: CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm."case"
    ADD CONSTRAINT pk_case PRIMARY KEY (id);


--
-- Name: case_note pk_case_note; Type: CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.case_note
    ADD CONSTRAINT pk_case_note PRIMARY KEY (id);


--
-- Name: contact_medium pk_contact_medium; Type: CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.contact_medium
    ADD CONSTRAINT pk_contact_medium PRIMARY KEY (id);


--
-- Name: customer pk_customer; Type: CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.customer
    ADD CONSTRAINT pk_customer PRIMARY KEY (id);


--
-- Name: customer_identity pk_customer_identity; Type: CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.customer_identity
    ADD CONSTRAINT pk_customer_identity PRIMARY KEY (customer_id);


--
-- Name: individual pk_individual; Type: CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.individual
    ADD CONSTRAINT pk_individual PRIMARY KEY (party_id);


--
-- Name: interaction pk_interaction; Type: CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.interaction
    ADD CONSTRAINT pk_interaction PRIMARY KEY (id);


--
-- Name: party pk_party; Type: CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.party
    ADD CONSTRAINT pk_party PRIMARY KEY (id);


--
-- Name: port_request pk_port_request; Type: CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.port_request
    ADD CONSTRAINT pk_port_request PRIMARY KEY (id);


--
-- Name: sla_policy pk_sla_policy; Type: CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.sla_policy
    ADD CONSTRAINT pk_sla_policy PRIMARY KEY (id);


--
-- Name: ticket pk_ticket; Type: CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.ticket
    ADD CONSTRAINT pk_ticket PRIMARY KEY (id);


--
-- Name: ticket_state_history pk_ticket_state_history; Type: CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.ticket_state_history
    ADD CONSTRAINT pk_ticket_state_history PRIMARY KEY (id);


--
-- Name: agent uq_agent_email; Type: CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.agent
    ADD CONSTRAINT uq_agent_email UNIQUE (email);


--
-- Name: external_call pk_external_call; Type: CONSTRAINT; Schema: integrations; Owner: -
--

ALTER TABLE ONLY integrations.external_call
    ADD CONSTRAINT pk_external_call PRIMARY KEY (id);


--
-- Name: kyc_webhook_corroboration pk_kyc_webhook_corroboration; Type: CONSTRAINT; Schema: integrations; Owner: -
--

ALTER TABLE ONLY integrations.kyc_webhook_corroboration
    ADD CONSTRAINT pk_kyc_webhook_corroboration PRIMARY KEY (id);


--
-- Name: webhook_event pk_webhook_event; Type: CONSTRAINT; Schema: integrations; Owner: -
--

ALTER TABLE ONLY integrations.webhook_event
    ADD CONSTRAINT pk_webhook_event PRIMARY KEY (provider, event_id);


--
-- Name: esim_profile pk_esim_profile; Type: CONSTRAINT; Schema: inventory; Owner: -
--

ALTER TABLE ONLY inventory.esim_profile
    ADD CONSTRAINT pk_esim_profile PRIMARY KEY (iccid);


--
-- Name: msisdn_pool pk_msisdn_pool; Type: CONSTRAINT; Schema: inventory; Owner: -
--

ALTER TABLE ONLY inventory.msisdn_pool
    ADD CONSTRAINT pk_msisdn_pool PRIMARY KEY (msisdn);


--
-- Name: esim_profile uq_esim_profile_imsi; Type: CONSTRAINT; Schema: inventory; Owner: -
--

ALTER TABLE ONLY inventory.esim_profile
    ADD CONSTRAINT uq_esim_profile_imsi UNIQUE (imsi);


--
-- Name: esim_profile uq_esim_profile_matching_id; Type: CONSTRAINT; Schema: inventory; Owner: -
--

ALTER TABLE ONLY inventory.esim_profile
    ADD CONSTRAINT uq_esim_profile_matching_id UNIQUE (matching_id);


--
-- Name: doc_chunk pk_doc_chunk; Type: CONSTRAINT; Schema: knowledge; Owner: -
--

ALTER TABLE ONLY knowledge.doc_chunk
    ADD CONSTRAINT pk_doc_chunk PRIMARY KEY (id);


--
-- Name: usage_event pk_usage_event; Type: CONSTRAINT; Schema: mediation; Owner: -
--

ALTER TABLE ONLY mediation.usage_event
    ADD CONSTRAINT pk_usage_event PRIMARY KEY (id);


--
-- Name: order_item pk_order_item; Type: CONSTRAINT; Schema: order_mgmt; Owner: -
--

ALTER TABLE ONLY order_mgmt.order_item
    ADD CONSTRAINT pk_order_item PRIMARY KEY (id);


--
-- Name: order_state_history pk_order_state_history; Type: CONSTRAINT; Schema: order_mgmt; Owner: -
--

ALTER TABLE ONLY order_mgmt.order_state_history
    ADD CONSTRAINT pk_order_state_history PRIMARY KEY (id);


--
-- Name: processed_event pk_processed_event; Type: CONSTRAINT; Schema: order_mgmt; Owner: -
--

ALTER TABLE ONLY order_mgmt.processed_event
    ADD CONSTRAINT pk_processed_event PRIMARY KEY (event_id, consumer);


--
-- Name: product_order pk_product_order; Type: CONSTRAINT; Schema: order_mgmt; Owner: -
--

ALTER TABLE ONLY order_mgmt.product_order
    ADD CONSTRAINT pk_product_order PRIMARY KEY (id);


--
-- Name: customer pk_customer; Type: CONSTRAINT; Schema: payment; Owner: -
--

ALTER TABLE ONLY payment.customer
    ADD CONSTRAINT pk_customer PRIMARY KEY (id);


--
-- Name: payment_attempt pk_payment_attempt; Type: CONSTRAINT; Schema: payment; Owner: -
--

ALTER TABLE ONLY payment.payment_attempt
    ADD CONSTRAINT pk_payment_attempt PRIMARY KEY (id);


--
-- Name: payment_method pk_payment_method; Type: CONSTRAINT; Schema: payment; Owner: -
--

ALTER TABLE ONLY payment.payment_method
    ADD CONSTRAINT pk_payment_method PRIMARY KEY (id);


--
-- Name: payment_method uq_payment_method_token; Type: CONSTRAINT; Schema: payment; Owner: -
--

ALTER TABLE ONLY payment.payment_method
    ADD CONSTRAINT uq_payment_method_token UNIQUE (token);


--
-- Name: email_change_pending pk_email_change_pending; Type: CONSTRAINT; Schema: portal_auth; Owner: -
--

ALTER TABLE ONLY portal_auth.email_change_pending
    ADD CONSTRAINT pk_email_change_pending PRIMARY KEY (id);


--
-- Name: identity pk_identity; Type: CONSTRAINT; Schema: portal_auth; Owner: -
--

ALTER TABLE ONLY portal_auth.identity
    ADD CONSTRAINT pk_identity PRIMARY KEY (id);


--
-- Name: login_attempt pk_login_attempt; Type: CONSTRAINT; Schema: portal_auth; Owner: -
--

ALTER TABLE ONLY portal_auth.login_attempt
    ADD CONSTRAINT pk_login_attempt PRIMARY KEY (id);


--
-- Name: login_token pk_login_token; Type: CONSTRAINT; Schema: portal_auth; Owner: -
--

ALTER TABLE ONLY portal_auth.login_token
    ADD CONSTRAINT pk_login_token PRIMARY KEY (id);


--
-- Name: portal_action pk_portal_action; Type: CONSTRAINT; Schema: portal_auth; Owner: -
--

ALTER TABLE ONLY portal_auth.portal_action
    ADD CONSTRAINT pk_portal_action PRIMARY KEY (id);


--
-- Name: session pk_session; Type: CONSTRAINT; Schema: portal_auth; Owner: -
--

ALTER TABLE ONLY portal_auth.session
    ADD CONSTRAINT pk_session PRIMARY KEY (id);


--
-- Name: step_up_pending_action pk_step_up_pending_action; Type: CONSTRAINT; Schema: portal_auth; Owner: -
--

ALTER TABLE ONLY portal_auth.step_up_pending_action
    ADD CONSTRAINT pk_step_up_pending_action PRIMARY KEY (id);


--
-- Name: fault_injection pk_fault_injection; Type: CONSTRAINT; Schema: provisioning; Owner: -
--

ALTER TABLE ONLY provisioning.fault_injection
    ADD CONSTRAINT pk_fault_injection PRIMARY KEY (id);


--
-- Name: provisioning_task pk_provisioning_task; Type: CONSTRAINT; Schema: provisioning; Owner: -
--

ALTER TABLE ONLY provisioning.provisioning_task
    ADD CONSTRAINT pk_provisioning_task PRIMARY KEY (id);


--
-- Name: processed_event processed_event_pkey; Type: CONSTRAINT; Schema: provisioning; Owner: -
--

ALTER TABLE ONLY provisioning.processed_event
    ADD CONSTRAINT processed_event_pkey PRIMARY KEY (event_id, consumer);


--
-- Name: processed_event pk_processed_event; Type: CONSTRAINT; Schema: service_inventory; Owner: -
--

ALTER TABLE ONLY service_inventory.processed_event
    ADD CONSTRAINT pk_processed_event PRIMARY KEY (event_id, consumer);


--
-- Name: service pk_service; Type: CONSTRAINT; Schema: service_inventory; Owner: -
--

ALTER TABLE ONLY service_inventory.service
    ADD CONSTRAINT pk_service PRIMARY KEY (id);


--
-- Name: service_order pk_service_order; Type: CONSTRAINT; Schema: service_inventory; Owner: -
--

ALTER TABLE ONLY service_inventory.service_order
    ADD CONSTRAINT pk_service_order PRIMARY KEY (id);


--
-- Name: service_order_item pk_service_order_item; Type: CONSTRAINT; Schema: service_inventory; Owner: -
--

ALTER TABLE ONLY service_inventory.service_order_item
    ADD CONSTRAINT pk_service_order_item PRIMARY KEY (id);


--
-- Name: service_state_history pk_service_state_history; Type: CONSTRAINT; Schema: service_inventory; Owner: -
--

ALTER TABLE ONLY service_inventory.service_state_history
    ADD CONSTRAINT pk_service_state_history PRIMARY KEY (id);


--
-- Name: bundle_balance pk_bundle_balance; Type: CONSTRAINT; Schema: subscription; Owner: -
--

ALTER TABLE ONLY subscription.bundle_balance
    ADD CONSTRAINT pk_bundle_balance PRIMARY KEY (id);


--
-- Name: processed_event pk_processed_event; Type: CONSTRAINT; Schema: subscription; Owner: -
--

ALTER TABLE ONLY subscription.processed_event
    ADD CONSTRAINT pk_processed_event PRIMARY KEY (event_id, consumer);


--
-- Name: subscription pk_subscription; Type: CONSTRAINT; Schema: subscription; Owner: -
--

ALTER TABLE ONLY subscription.subscription
    ADD CONSTRAINT pk_subscription PRIMARY KEY (id);


--
-- Name: subscription_state_history pk_subscription_state_history; Type: CONSTRAINT; Schema: subscription; Owner: -
--

ALTER TABLE ONLY subscription.subscription_state_history
    ADD CONSTRAINT pk_subscription_state_history PRIMARY KEY (id);


--
-- Name: vas_purchase pk_vas_purchase; Type: CONSTRAINT; Schema: subscription; Owner: -
--

ALTER TABLE ONLY subscription.vas_purchase
    ADD CONSTRAINT pk_vas_purchase PRIMARY KEY (id);


--
-- Name: ix_audit_domain_event_trace_id; Type: INDEX; Schema: audit; Owner: -
--

CREATE INDEX ix_audit_domain_event_trace_id ON audit.domain_event USING btree (trace_id);


--
-- Name: ix_chat_transcript_customer; Type: INDEX; Schema: audit; Owner: -
--

CREATE INDEX ix_chat_transcript_customer ON audit.chat_transcript USING btree (customer_id, recorded_at);


--
-- Name: ix_domain_event_aggregate_replay; Type: INDEX; Schema: audit; Owner: -
--

CREATE INDEX ix_domain_event_aggregate_replay ON audit.domain_event USING btree (aggregate_type, aggregate_id, occurred_at);


--
-- Name: ix_domain_event_service_identity_time; Type: INDEX; Schema: audit; Owner: -
--

CREATE INDEX ix_domain_event_service_identity_time ON audit.domain_event USING btree (service_identity, occurred_at);


--
-- Name: ix_domain_event_type_time; Type: INDEX; Schema: audit; Owner: -
--

CREATE INDEX ix_domain_event_type_time ON audit.domain_event USING btree (event_type, occurred_at);


--
-- Name: ix_domain_event_unpublished; Type: INDEX; Schema: audit; Owner: -
--

CREATE INDEX ix_domain_event_unpublished ON audit.domain_event USING btree (published_to_mq) WHERE (NOT published_to_mq);


--
-- Name: ix_promotion_eligibility_customer; Type: INDEX; Schema: catalog; Owner: -
--

CREATE INDEX ix_promotion_eligibility_customer ON catalog.promotion_eligibility USING btree (customer_id, tenant_id);


--
-- Name: ix_promotion_offer_definition_id; Type: INDEX; Schema: catalog; Owner: -
--

CREATE INDEX ix_promotion_offer_definition_id ON catalog.promotion USING btree (offer_definition_id);


--
-- Name: uq_promotion_code; Type: INDEX; Schema: catalog; Owner: -
--

CREATE UNIQUE INDEX uq_promotion_code ON catalog.promotion USING btree (code, tenant_id) WHERE (code IS NOT NULL);


--
-- Name: uq_promotion_eligibility_promo_customer; Type: INDEX; Schema: catalog; Owner: -
--

CREATE UNIQUE INDEX uq_promotion_eligibility_promo_customer ON catalog.promotion_eligibility USING btree (promotion_id, customer_id, tenant_id);


--
-- Name: ix_cockpit_message_session_created; Type: INDEX; Schema: cockpit; Owner: -
--

CREATE INDEX ix_cockpit_message_session_created ON cockpit.message USING btree (session_id, created_at);


--
-- Name: ix_cockpit_session_actor_active; Type: INDEX; Schema: cockpit; Owner: -
--

CREATE INDEX ix_cockpit_session_actor_active ON cockpit.session USING btree (actor, last_active_at) WHERE (state = 'active'::text);


--
-- Name: ix_port_request_state_direction; Type: INDEX; Schema: crm; Owner: -
--

CREATE INDEX ix_port_request_state_direction ON crm.port_request USING btree (state, direction);


--
-- Name: uq_contact_medium_email_active; Type: INDEX; Schema: crm; Owner: -
--

CREATE UNIQUE INDEX uq_contact_medium_email_active ON crm.contact_medium USING btree (medium_type, value) WHERE ((valid_to IS NULL) AND (medium_type = 'email'::text));


--
-- Name: uq_customer_identity_doc; Type: INDEX; Schema: crm; Owner: -
--

CREATE UNIQUE INDEX uq_customer_identity_doc ON crm.customer_identity USING btree (document_type, document_number_hash, tenant_id);


--
-- Name: uq_port_request_donor_pending; Type: INDEX; Schema: crm; Owner: -
--

CREATE UNIQUE INDEX uq_port_request_donor_pending ON crm.port_request USING btree (donor_msisdn, tenant_id) WHERE (state = ANY (ARRAY['requested'::text, 'validated'::text]));


--
-- Name: ix_external_call_aggregate; Type: INDEX; Schema: integrations; Owner: -
--

CREATE INDEX ix_external_call_aggregate ON integrations.external_call USING btree (aggregate_type, aggregate_id);


--
-- Name: ix_external_call_provider_time; Type: INDEX; Schema: integrations; Owner: -
--

CREATE INDEX ix_external_call_provider_time ON integrations.external_call USING btree (provider, occurred_at);


--
-- Name: ix_webhook_event_received_unprocessed; Type: INDEX; Schema: integrations; Owner: -
--

CREATE INDEX ix_webhook_event_received_unprocessed ON integrations.webhook_event USING btree (received_at) WHERE (processed_at IS NULL);


--
-- Name: uq_kyc_corroboration_provider_session; Type: INDEX; Schema: integrations; Owner: -
--

CREATE UNIQUE INDEX uq_kyc_corroboration_provider_session ON integrations.kyc_webhook_corroboration USING btree (provider, provider_session_id);


--
-- Name: ix_doc_chunk_content_tsv; Type: INDEX; Schema: knowledge; Owner: -
--

CREATE INDEX ix_doc_chunk_content_tsv ON knowledge.doc_chunk USING gin (content_tsv);


--
-- Name: ix_doc_chunk_embedding_hnsw; Type: INDEX; Schema: knowledge; Owner: -
--

CREATE INDEX ix_doc_chunk_embedding_hnsw ON knowledge.doc_chunk USING hnsw (embedding public.vector_cosine_ops) WHERE (embedding IS NOT NULL);


--
-- Name: ix_doc_chunk_source_anchor; Type: INDEX; Schema: knowledge; Owner: -
--

CREATE UNIQUE INDEX ix_doc_chunk_source_anchor ON knowledge.doc_chunk USING btree (source_path, anchor);


--
-- Name: ix_doc_chunk_source_path; Type: INDEX; Schema: knowledge; Owner: -
--

CREATE INDEX ix_doc_chunk_source_path ON knowledge.doc_chunk USING btree (source_path);


--
-- Name: ix_payment_attempt_idempotency_key; Type: INDEX; Schema: payment; Owner: -
--

CREATE INDEX ix_payment_attempt_idempotency_key ON payment.payment_attempt USING btree (idempotency_key) WHERE (idempotency_key IS NOT NULL);


--
-- Name: ix_payment_customer_external_ref; Type: INDEX; Schema: payment; Owner: -
--

CREATE INDEX ix_payment_customer_external_ref ON payment.customer USING btree (customer_external_ref_provider, customer_external_ref);


--
-- Name: ix_email_change_pending_expires; Type: INDEX; Schema: portal_auth; Owner: -
--

CREATE INDEX ix_email_change_pending_expires ON portal_auth.email_change_pending USING btree (expires_at) WHERE (status = 'pending'::text);


--
-- Name: ix_login_attempt_email_ts; Type: INDEX; Schema: portal_auth; Owner: -
--

CREATE INDEX ix_login_attempt_email_ts ON portal_auth.login_attempt USING btree (email, ts);


--
-- Name: ix_login_attempt_ip_ts; Type: INDEX; Schema: portal_auth; Owner: -
--

CREATE INDEX ix_login_attempt_ip_ts ON portal_auth.login_attempt USING btree (ip, ts);


--
-- Name: ix_login_token_identity_kind_unconsumed; Type: INDEX; Schema: portal_auth; Owner: -
--

CREATE INDEX ix_login_token_identity_kind_unconsumed ON portal_auth.login_token USING btree (identity_id, kind, consumed_at);


--
-- Name: ix_portal_action_action_ts; Type: INDEX; Schema: portal_auth; Owner: -
--

CREATE INDEX ix_portal_action_action_ts ON portal_auth.portal_action USING btree (action, ts DESC);


--
-- Name: ix_portal_action_customer_ts; Type: INDEX; Schema: portal_auth; Owner: -
--

CREATE INDEX ix_portal_action_customer_ts ON portal_auth.portal_action USING btree (customer_id, ts DESC);


--
-- Name: ix_portal_action_unknown_rule; Type: INDEX; Schema: portal_auth; Owner: -
--

CREATE INDEX ix_portal_action_unknown_rule ON portal_auth.portal_action USING btree (error_rule, ts DESC) WHERE (error_rule IS NOT NULL);


--
-- Name: ix_session_identity_active; Type: INDEX; Schema: portal_auth; Owner: -
--

CREATE INDEX ix_session_identity_active ON portal_auth.session USING btree (identity_id, revoked_at);


--
-- Name: ix_step_up_pending_action_expires; Type: INDEX; Schema: portal_auth; Owner: -
--

CREATE INDEX ix_step_up_pending_action_expires ON portal_auth.step_up_pending_action USING btree (expires_at) WHERE (consumed_at IS NULL);


--
-- Name: uq_email_change_pending_identity_active; Type: INDEX; Schema: portal_auth; Owner: -
--

CREATE UNIQUE INDEX uq_email_change_pending_identity_active ON portal_auth.email_change_pending USING btree (identity_id) WHERE (status = 'pending'::text);


--
-- Name: uq_identity_email_active; Type: INDEX; Schema: portal_auth; Owner: -
--

CREATE UNIQUE INDEX uq_identity_email_active ON portal_auth.identity USING btree (email) WHERE (status <> 'deleted'::text);


--
-- Name: uq_step_up_pending_action_active; Type: INDEX; Schema: portal_auth; Owner: -
--

CREATE UNIQUE INDEX uq_step_up_pending_action_active ON portal_auth.step_up_pending_action USING btree (session_id, action_label) WHERE (consumed_at IS NULL);


--
-- Name: ix_subscription_due_for_reminder; Type: INDEX; Schema: subscription; Owner: -
--

CREATE INDEX ix_subscription_due_for_reminder ON subscription.subscription USING btree (state, next_renewal_at) WHERE (state = 'active'::text);


--
-- Name: ix_subscription_due_for_renewal; Type: INDEX; Schema: subscription; Owner: -
--

CREATE INDEX ix_subscription_due_for_renewal ON subscription.subscription USING btree (state, next_renewal_at) WHERE (state = ANY (ARRAY['active'::text, 'blocked'::text]));


--
-- Name: uq_subscription_commercial_order; Type: INDEX; Schema: subscription; Owner: -
--

CREATE UNIQUE INDEX uq_subscription_commercial_order ON subscription.subscription USING btree (commercial_order_id) WHERE (commercial_order_id IS NOT NULL);


--
-- Name: uq_subscription_iccid; Type: INDEX; Schema: subscription; Owner: -
--

CREATE UNIQUE INDEX uq_subscription_iccid ON subscription.subscription USING btree (iccid) WHERE (state <> 'terminated'::text);


--
-- Name: uq_subscription_msisdn; Type: INDEX; Schema: subscription; Owner: -
--

CREATE UNIQUE INDEX uq_subscription_msisdn ON subscription.subscription USING btree (msisdn) WHERE (state <> 'terminated'::text);


--
-- Name: customer_bill fk_customer_bill_billing_account_id_billing_account; Type: FK CONSTRAINT; Schema: billing; Owner: -
--

ALTER TABLE ONLY billing.customer_bill
    ADD CONSTRAINT fk_customer_bill_billing_account_id_billing_account FOREIGN KEY (billing_account_id) REFERENCES billing.billing_account(id);


--
-- Name: bundle_allowance fk_bundle_allowance_offering_id_product_offering; Type: FK CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.bundle_allowance
    ADD CONSTRAINT fk_bundle_allowance_offering_id_product_offering FOREIGN KEY (offering_id) REFERENCES catalog.product_offering(id);


--
-- Name: product_offering_price fk_product_offering_price_offering_id_product_offering; Type: FK CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_offering_price
    ADD CONSTRAINT fk_product_offering_price_offering_id_product_offering FOREIGN KEY (offering_id) REFERENCES catalog.product_offering(id);


--
-- Name: product_offering fk_product_offering_spec_id_product_specification; Type: FK CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_offering
    ADD CONSTRAINT fk_product_offering_spec_id_product_specification FOREIGN KEY (spec_id) REFERENCES catalog.product_specification(id);


--
-- Name: product_to_service_mapping fk_product_to_service_mapping_cfs_spec_id_service_specification; Type: FK CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_to_service_mapping
    ADD CONSTRAINT fk_product_to_service_mapping_cfs_spec_id_service_specification FOREIGN KEY (cfs_spec_id) REFERENCES catalog.service_specification(id);


--
-- Name: product_to_service_mapping fk_product_to_service_mapping_offering_id_product_offering; Type: FK CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.product_to_service_mapping
    ADD CONSTRAINT fk_product_to_service_mapping_offering_id_product_offering FOREIGN KEY (offering_id) REFERENCES catalog.product_offering(id);


--
-- Name: promotion_eligibility fk_promotion_eligibility_promotion_id_promotion; Type: FK CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.promotion_eligibility
    ADD CONSTRAINT fk_promotion_eligibility_promotion_id_promotion FOREIGN KEY (promotion_id) REFERENCES catalog.promotion(id);


--
-- Name: message fk_message_session_id_session; Type: FK CONSTRAINT; Schema: cockpit; Owner: -
--

ALTER TABLE ONLY cockpit.message
    ADD CONSTRAINT fk_message_session_id_session FOREIGN KEY (session_id) REFERENCES cockpit.session(id) ON DELETE CASCADE;


--
-- Name: pending_destructive fk_pending_destructive_proposal_message_id_message; Type: FK CONSTRAINT; Schema: cockpit; Owner: -
--

ALTER TABLE ONLY cockpit.pending_destructive
    ADD CONSTRAINT fk_pending_destructive_proposal_message_id_message FOREIGN KEY (proposal_message_id) REFERENCES cockpit.message(id) ON DELETE CASCADE;


--
-- Name: pending_destructive fk_pending_destructive_session_id_session; Type: FK CONSTRAINT; Schema: cockpit; Owner: -
--

ALTER TABLE ONLY cockpit.pending_destructive
    ADD CONSTRAINT fk_pending_destructive_session_id_session FOREIGN KEY (session_id) REFERENCES cockpit.session(id) ON DELETE CASCADE;


--
-- Name: case fk_case_customer_id_customer; Type: FK CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm."case"
    ADD CONSTRAINT fk_case_customer_id_customer FOREIGN KEY (customer_id) REFERENCES crm.customer(id);


--
-- Name: case_note fk_case_note_author_agent_id_agent; Type: FK CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.case_note
    ADD CONSTRAINT fk_case_note_author_agent_id_agent FOREIGN KEY (author_agent_id) REFERENCES crm.agent(id);


--
-- Name: case_note fk_case_note_case_id_case; Type: FK CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.case_note
    ADD CONSTRAINT fk_case_note_case_id_case FOREIGN KEY (case_id) REFERENCES crm."case"(id);


--
-- Name: case fk_case_opened_by_agent_id_agent; Type: FK CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm."case"
    ADD CONSTRAINT fk_case_opened_by_agent_id_agent FOREIGN KEY (opened_by_agent_id) REFERENCES crm.agent(id);


--
-- Name: contact_medium fk_contact_medium_party_id_party; Type: FK CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.contact_medium
    ADD CONSTRAINT fk_contact_medium_party_id_party FOREIGN KEY (party_id) REFERENCES crm.party(id);


--
-- Name: customer_identity fk_customer_identity_corroboration_id_kyc_webhook_corroboration; Type: FK CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.customer_identity
    ADD CONSTRAINT fk_customer_identity_corroboration_id_kyc_webhook_corroboration FOREIGN KEY (corroboration_id) REFERENCES integrations.kyc_webhook_corroboration(id);


--
-- Name: customer_identity fk_customer_identity_customer_id_customer; Type: FK CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.customer_identity
    ADD CONSTRAINT fk_customer_identity_customer_id_customer FOREIGN KEY (customer_id) REFERENCES crm.customer(id);


--
-- Name: customer fk_customer_party_id_party; Type: FK CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.customer
    ADD CONSTRAINT fk_customer_party_id_party FOREIGN KEY (party_id) REFERENCES crm.party(id);


--
-- Name: individual fk_individual_party_id_party; Type: FK CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.individual
    ADD CONSTRAINT fk_individual_party_id_party FOREIGN KEY (party_id) REFERENCES crm.party(id);


--
-- Name: interaction fk_interaction_agent_id_agent; Type: FK CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.interaction
    ADD CONSTRAINT fk_interaction_agent_id_agent FOREIGN KEY (agent_id) REFERENCES crm.agent(id);


--
-- Name: interaction fk_interaction_customer_id_customer; Type: FK CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.interaction
    ADD CONSTRAINT fk_interaction_customer_id_customer FOREIGN KEY (customer_id) REFERENCES crm.customer(id);


--
-- Name: interaction fk_interaction_related_case_id_case; Type: FK CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.interaction
    ADD CONSTRAINT fk_interaction_related_case_id_case FOREIGN KEY (related_case_id) REFERENCES crm."case"(id);


--
-- Name: interaction fk_interaction_related_ticket_id_ticket; Type: FK CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.interaction
    ADD CONSTRAINT fk_interaction_related_ticket_id_ticket FOREIGN KEY (related_ticket_id) REFERENCES crm.ticket(id);


--
-- Name: ticket fk_ticket_assigned_to_agent_id_agent; Type: FK CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.ticket
    ADD CONSTRAINT fk_ticket_assigned_to_agent_id_agent FOREIGN KEY (assigned_to_agent_id) REFERENCES crm.agent(id);


--
-- Name: ticket fk_ticket_case_id_case; Type: FK CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.ticket
    ADD CONSTRAINT fk_ticket_case_id_case FOREIGN KEY (case_id) REFERENCES crm."case"(id);


--
-- Name: ticket fk_ticket_customer_id_customer; Type: FK CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.ticket
    ADD CONSTRAINT fk_ticket_customer_id_customer FOREIGN KEY (customer_id) REFERENCES crm.customer(id);


--
-- Name: ticket_state_history fk_ticket_state_history_changed_by_agent_id_agent; Type: FK CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.ticket_state_history
    ADD CONSTRAINT fk_ticket_state_history_changed_by_agent_id_agent FOREIGN KEY (changed_by_agent_id) REFERENCES crm.agent(id);


--
-- Name: ticket_state_history fk_ticket_state_history_ticket_id_ticket; Type: FK CONSTRAINT; Schema: crm; Owner: -
--

ALTER TABLE ONLY crm.ticket_state_history
    ADD CONSTRAINT fk_ticket_state_history_ticket_id_ticket FOREIGN KEY (ticket_id) REFERENCES crm.ticket(id);


--
-- Name: kyc_webhook_corroboration fk_kyc_webhook_corroboration_webhook_event_provider_web_25a2; Type: FK CONSTRAINT; Schema: integrations; Owner: -
--

ALTER TABLE ONLY integrations.kyc_webhook_corroboration
    ADD CONSTRAINT fk_kyc_webhook_corroboration_webhook_event_provider_web_25a2 FOREIGN KEY (webhook_event_provider, webhook_event_id) REFERENCES integrations.webhook_event(provider, event_id);


--
-- Name: esim_profile fk_esim_profile_assigned_msisdn_msisdn_pool; Type: FK CONSTRAINT; Schema: inventory; Owner: -
--

ALTER TABLE ONLY inventory.esim_profile
    ADD CONSTRAINT fk_esim_profile_assigned_msisdn_msisdn_pool FOREIGN KEY (assigned_msisdn) REFERENCES inventory.msisdn_pool(msisdn);


--
-- Name: order_item fk_order_item_order_id_product_order; Type: FK CONSTRAINT; Schema: order_mgmt; Owner: -
--

ALTER TABLE ONLY order_mgmt.order_item
    ADD CONSTRAINT fk_order_item_order_id_product_order FOREIGN KEY (order_id) REFERENCES order_mgmt.product_order(id);


--
-- Name: order_state_history fk_order_state_history_order_id_product_order; Type: FK CONSTRAINT; Schema: order_mgmt; Owner: -
--

ALTER TABLE ONLY order_mgmt.order_state_history
    ADD CONSTRAINT fk_order_state_history_order_id_product_order FOREIGN KEY (order_id) REFERENCES order_mgmt.product_order(id);


--
-- Name: payment_attempt fk_payment_attempt_payment_method_id_payment_method; Type: FK CONSTRAINT; Schema: payment; Owner: -
--

ALTER TABLE ONLY payment.payment_attempt
    ADD CONSTRAINT fk_payment_attempt_payment_method_id_payment_method FOREIGN KEY (payment_method_id) REFERENCES payment.payment_method(id);


--
-- Name: email_change_pending fk_email_change_pending_identity_id_identity; Type: FK CONSTRAINT; Schema: portal_auth; Owner: -
--

ALTER TABLE ONLY portal_auth.email_change_pending
    ADD CONSTRAINT fk_email_change_pending_identity_id_identity FOREIGN KEY (identity_id) REFERENCES portal_auth.identity(id);


--
-- Name: login_token fk_login_token_identity_id_identity; Type: FK CONSTRAINT; Schema: portal_auth; Owner: -
--

ALTER TABLE ONLY portal_auth.login_token
    ADD CONSTRAINT fk_login_token_identity_id_identity FOREIGN KEY (identity_id) REFERENCES portal_auth.identity(id);


--
-- Name: session fk_session_identity_id_identity; Type: FK CONSTRAINT; Schema: portal_auth; Owner: -
--

ALTER TABLE ONLY portal_auth.session
    ADD CONSTRAINT fk_session_identity_id_identity FOREIGN KEY (identity_id) REFERENCES portal_auth.identity(id);


--
-- Name: step_up_pending_action fk_step_up_pending_action_session_id_session; Type: FK CONSTRAINT; Schema: portal_auth; Owner: -
--

ALTER TABLE ONLY portal_auth.step_up_pending_action
    ADD CONSTRAINT fk_step_up_pending_action_session_id_session FOREIGN KEY (session_id) REFERENCES portal_auth.session(id);


--
-- Name: service_order_item fk_service_order_item_service_order_id_service_order; Type: FK CONSTRAINT; Schema: service_inventory; Owner: -
--

ALTER TABLE ONLY service_inventory.service_order_item
    ADD CONSTRAINT fk_service_order_item_service_order_id_service_order FOREIGN KEY (service_order_id) REFERENCES service_inventory.service_order(id);


--
-- Name: service fk_service_parent_service_id_service; Type: FK CONSTRAINT; Schema: service_inventory; Owner: -
--

ALTER TABLE ONLY service_inventory.service
    ADD CONSTRAINT fk_service_parent_service_id_service FOREIGN KEY (parent_service_id) REFERENCES service_inventory.service(id);


--
-- Name: service_state_history fk_service_state_history_service_id_service; Type: FK CONSTRAINT; Schema: service_inventory; Owner: -
--

ALTER TABLE ONLY service_inventory.service_state_history
    ADD CONSTRAINT fk_service_state_history_service_id_service FOREIGN KEY (service_id) REFERENCES service_inventory.service(id);


--
-- Name: bundle_balance fk_bundle_balance_subscription_id_subscription; Type: FK CONSTRAINT; Schema: subscription; Owner: -
--

ALTER TABLE ONLY subscription.bundle_balance
    ADD CONSTRAINT fk_bundle_balance_subscription_id_subscription FOREIGN KEY (subscription_id) REFERENCES subscription.subscription(id);


--
-- Name: subscription fk_subscription_price_offering_price_id_product_offering_price; Type: FK CONSTRAINT; Schema: subscription; Owner: -
--

ALTER TABLE ONLY subscription.subscription
    ADD CONSTRAINT fk_subscription_price_offering_price_id_product_offering_price FOREIGN KEY (price_offering_price_id) REFERENCES catalog.product_offering_price(id);


--
-- Name: subscription_state_history fk_subscription_state_history_subscription_id_subscription; Type: FK CONSTRAINT; Schema: subscription; Owner: -
--

ALTER TABLE ONLY subscription.subscription_state_history
    ADD CONSTRAINT fk_subscription_state_history_subscription_id_subscription FOREIGN KEY (subscription_id) REFERENCES subscription.subscription(id);


--
-- Name: vas_purchase fk_vas_purchase_subscription_id_subscription; Type: FK CONSTRAINT; Schema: subscription; Owner: -
--

ALTER TABLE ONLY subscription.vas_purchase
    ADD CONSTRAINT fk_vas_purchase_subscription_id_subscription FOREIGN KEY (subscription_id) REFERENCES subscription.subscription(id);


--
-- PostgreSQL database dump complete
--


