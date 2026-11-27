-- Multi-Location Finance Reconciliation Pipeline — schema v1
-- Written for PostgreSQL 16. The same file also runs on SQLite 3.39+ (used for
-- the offline test suite), so it avoids server-side defaults and SERIAL.
--
-- Conventions
--   * Internal primary keys are UUIDs derived from stable business keys.
--   * Every external record keeps (source, source_*_id) with a UNIQUE constraint,
--     which is what makes upserts idempotent.
--   * Money is BIGINT minor units (cents) + an explicit currency column.
--   * Timestamps are UTC (TIMESTAMPTZ); business dates are DATE.

-- ---------------------------------------------------------------- reference
CREATE TABLE IF NOT EXISTS clients (
    client_id      UUID PRIMARY KEY,
    client_key     TEXT NOT NULL UNIQUE,
    name           TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS locations (
    location_id    UUID PRIMARY KEY,
    client_id      UUID NOT NULL REFERENCES clients(client_id),
    location_key   TEXT NOT NULL,
    name           TEXT NOT NULL,
    opened_on      DATE,
    created_at     TIMESTAMPTZ NOT NULL,
    UNIQUE (client_id, location_key)
);

-- One row per external identifier. internal_type says whether the external
-- object represents a client or a location (e.g. a QBO sub-customer per
-- location, a Stripe customer per client).
CREATE TABLE IF NOT EXISTS source_id_map (
    source         TEXT NOT NULL,          -- qbo | stripe | ad_platform
    entity_type    TEXT NOT NULL,          -- customer | client | location ...
    external_id    TEXT NOT NULL,
    internal_type  TEXT NOT NULL CHECK (internal_type IN ('client', 'location')),
    internal_id    UUID NOT NULL,
    PRIMARY KEY (source, entity_type, external_id)
);

CREATE TABLE IF NOT EXISTS billing_rules (
    client_id      UUID PRIMARY KEY REFERENCES clients(client_id),
    mgmt_fee_bps   INTEGER NOT NULL CHECK (mgmt_fee_bps >= 0),
    min_fee_cents  BIGINT  NOT NULL CHECK (min_fee_cents >= 0),
    discount_bps   INTEGER NOT NULL CHECK (discount_bps >= 0),
    tax_bps        INTEGER NOT NULL CHECK (tax_bps >= 0),
    currency       TEXT    NOT NULL,
    description    TEXT    NOT NULL
);

-- ---------------------------------------------------------------- QuickBooks
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id         UUID PRIMARY KEY,
    source             TEXT NOT NULL,
    source_invoice_id  TEXT NOT NULL,
    doc_number         TEXT,
    client_id          UUID REFERENCES clients(client_id),
    location_id        UUID REFERENCES locations(location_id),
    qbo_customer_id    TEXT NOT NULL,
    billing_period     TEXT,                 -- YYYY-MM
    txn_date           DATE NOT NULL,
    due_date           DATE,
    total_cents        BIGINT NOT NULL,
    balance_cents      BIGINT NOT NULL,
    currency           TEXT NOT NULL,
    private_note       TEXT,
    scenario_id        TEXT,
    run_id             TEXT NOT NULL,
    ingested_at        TIMESTAMPTZ NOT NULL,
    UNIQUE (source, source_invoice_id)
);

CREATE TABLE IF NOT EXISTS qbo_payments (
    payment_id            UUID PRIMARY KEY,
    source_payment_id     TEXT NOT NULL UNIQUE,
    client_id             UUID REFERENCES clients(client_id),
    location_id           UUID REFERENCES locations(location_id),
    qbo_customer_id       TEXT NOT NULL,
    txn_date              DATE NOT NULL,
    total_cents           BIGINT NOT NULL,
    unapplied_cents       BIGINT NOT NULL,
    currency              TEXT NOT NULL,
    payment_method_id     TEXT,
    payment_method_name   TEXT,
    payment_method_type   TEXT,              -- CREDIT_CARD | NON_CREDIT_CARD | NULL
    payment_ref_num       TEXT,
    private_note          TEXT,
    referenced_stripe_id  TEXT,              -- parsed from the memo written by the sync
    scenario_id           TEXT,
    run_id                TEXT NOT NULL,
    ingested_at           TIMESTAMPTZ NOT NULL
);

-- Which invoice(s) each QBO payment was applied to (QBO Line.LinkedTxn).
CREATE TABLE IF NOT EXISTS payment_allocations (
    qbo_payment_id  UUID NOT NULL REFERENCES qbo_payments(payment_id),
    invoice_id      UUID NOT NULL REFERENCES invoices(invoice_id),
    amount_cents    BIGINT NOT NULL CHECK (amount_cents > 0),
    PRIMARY KEY (qbo_payment_id, invoice_id)
);

CREATE TABLE IF NOT EXISTS qbo_refunds (
    refund_id             UUID PRIMARY KEY,
    source_refund_id      TEXT NOT NULL UNIQUE,
    client_id             UUID REFERENCES clients(client_id),
    location_id           UUID REFERENCES locations(location_id),
    qbo_customer_id       TEXT NOT NULL,
    txn_date              DATE NOT NULL,
    total_cents           BIGINT NOT NULL,
    currency              TEXT NOT NULL,
    private_note          TEXT,
    referenced_stripe_id  TEXT,
    scenario_id           TEXT,
    run_id                TEXT NOT NULL,
    ingested_at           TIMESTAMPTZ NOT NULL
);

-- ---------------------------------------------------------------- Stripe
CREATE TABLE IF NOT EXISTS stripe_payments (
    payment_id              UUID PRIMARY KEY,
    source_payment_id       TEXT NOT NULL UNIQUE,   -- PaymentIntent id (pi_...)
    charge_id               TEXT,
    balance_transaction_id  TEXT,
    stripe_customer_id      TEXT,
    client_id               UUID REFERENCES clients(client_id),
    location_id             UUID REFERENCES locations(location_id),
    amount_cents            BIGINT NOT NULL,          -- gross
    amount_received_cents   BIGINT NOT NULL,
    currency                TEXT NOT NULL,
    status                  TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL,
    effective_date          DATE NOT NULL,
    description             TEXT,
    qbo_invoice_ref         TEXT,
    scenario_id             TEXT,
    run_id                  TEXT NOT NULL,
    ingested_at             TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS stripe_refunds (
    refund_id               UUID PRIMARY KEY,
    source_refund_id        TEXT NOT NULL UNIQUE,
    source_payment_id       TEXT,
    charge_id               TEXT,
    client_id               UUID REFERENCES clients(client_id),
    location_id             UUID REFERENCES locations(location_id),
    amount_cents            BIGINT NOT NULL,
    currency                TEXT NOT NULL,
    status                  TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL,
    effective_date          DATE NOT NULL,
    balance_transaction_id  TEXT,
    scenario_id             TEXT,
    run_id                  TEXT NOT NULL,
    ingested_at             TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS stripe_balance_transactions (
    bt_id             UUID PRIMARY KEY,
    source_bt_id      TEXT NOT NULL UNIQUE,
    type              TEXT NOT NULL,            -- charge | refund | payout ...
    source_object_id  TEXT,
    amount_cents      BIGINT NOT NULL,          -- gross
    fee_cents         BIGINT NOT NULL,
    net_cents         BIGINT NOT NULL,
    currency          TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL,
    available_on      DATE,
    payout_source_id  TEXT,                     -- payout this transaction was paid out in
    run_id            TEXT NOT NULL,
    ingested_at       TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS stripe_payouts (
    payout_id         UUID PRIMARY KEY,
    source_payout_id  TEXT NOT NULL UNIQUE,
    amount_cents      BIGINT NOT NULL,
    currency          TEXT NOT NULL,
    status            TEXT NOT NULL,
    arrival_date      DATE NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL,
    simulated         BOOLEAN NOT NULL,
    run_id            TEXT NOT NULL,
    ingested_at       TIMESTAMPTZ NOT NULL
);

-- ---------------------------------------------------------------- ad platform
CREATE TABLE IF NOT EXISTS ad_spend (
    spend_id         UUID PRIMARY KEY,
    source_spend_id  TEXT NOT NULL UNIQUE,
    client_id        UUID NOT NULL REFERENCES clients(client_id),
    location_id      UUID NOT NULL REFERENCES locations(location_id),
    spend_date       DATE NOT NULL,
    campaign         TEXT NOT NULL,
    amount_cents     BIGINT NOT NULL CHECK (amount_cents >= 0),
    currency         TEXT NOT NULL,
    run_id           TEXT NOT NULL,
    ingested_at      TIMESTAMPTZ NOT NULL
);

-- ---------------------------------------------------------------- pipeline ops
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id       TEXT PRIMARY KEY,
    run_no       INTEGER NOT NULL,
    mode         TEXT NOT NULL,
    as_of        DATE NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL,
    finished_at  TIMESTAMPTZ,
    status       TEXT NOT NULL
);

-- One row per source x scope (e.g. ad_platform x one location) per run.
CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id                TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    source                TEXT NOT NULL,
    scope                 TEXT NOT NULL,
    client_id             UUID,
    location_id           UUID,
    window_start          DATE,
    window_end            DATE,
    started_at            TIMESTAMPTZ NOT NULL,
    finished_at           TIMESTAMPTZ,
    status                TEXT NOT NULL,      -- succeeded | incomplete | failed
    records_expected      INTEGER,
    records_received      INTEGER,
    retry_count           INTEGER NOT NULL,
    error_details         TEXT,
    last_successful_sync  TIMESTAMPTZ,
    PRIMARY KEY (run_id, source, scope)
);

CREATE TABLE IF NOT EXISTS ingestion_checkpoints (
    source                   TEXT NOT NULL,
    scope                    TEXT NOT NULL,
    last_successful_run_id   TEXT NOT NULL,
    last_successful_sync     TIMESTAMPTZ NOT NULL,
    validated_through        DATE NOT NULL,
    PRIMARY KEY (source, scope)
);

CREATE TABLE IF NOT EXISTS raw_data_manifest (
    run_id       TEXT NOT NULL,
    source       TEXT NOT NULL,
    request_key  TEXT NOT NULL,
    path         TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    bytes        INTEGER NOT NULL,
    fetched_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (run_id, source, request_key)
);

CREATE TABLE IF NOT EXISTS data_quality_results (
    run_id      TEXT NOT NULL,
    source      TEXT NOT NULL,
    scope       TEXT NOT NULL,
    check_name  TEXT NOT NULL,
    passed      BOOLEAN NOT NULL,
    severity    TEXT NOT NULL,      -- error | warning | info
    detail      TEXT,
    PRIMARY KEY (run_id, source, scope, check_name)
);

-- Current data status per source x scope (history lives in ingestion_runs).
CREATE TABLE IF NOT EXISTS data_status (
    source                TEXT NOT NULL,
    scope                 TEXT NOT NULL,
    client_id             UUID,
    location_id           UUID,
    status                TEXT NOT NULL CHECK (status IN
                            ('VALIDATED','ZERO_TRANSACTIONS','NO_DATA','INCOMPLETE','STALE')),
    run_id                TEXT NOT NULL,
    as_of                 DATE NOT NULL,
    last_successful_sync  TIMESTAMPTZ,
    validated_through     DATE,
    detail                TEXT,
    PRIMARY KEY (source, scope)
);

-- ---------------------------------------------------------------- reconciliation
CREATE TABLE IF NOT EXISTS payment_matches (
    run_id             TEXT NOT NULL,
    stripe_payment_id  UUID NOT NULL REFERENCES stripe_payments(payment_id),
    qbo_payment_id     UUID NOT NULL REFERENCES qbo_payments(payment_id),
    method             TEXT NOT NULL CHECK (method IN ('exact_id', 'candidate')),
    amount_diff_cents  BIGINT NOT NULL,
    PRIMARY KEY (run_id, stripe_payment_id, qbo_payment_id)
);

CREATE TABLE IF NOT EXISTS reconciliation_results (
    run_id                    TEXT NOT NULL,
    record_type               TEXT NOT NULL,   -- invoice | stripe_payment | qbo_payment | stripe_refund | qbo_refund
    record_id                 UUID NOT NULL,
    source                    TEXT NOT NULL,
    source_record_id          TEXT NOT NULL,
    scenario_id               TEXT,
    client_id                 UUID,
    location_id               UUID,
    status                    TEXT NOT NULL CHECK (status IN
                                ('MATCHED','PARTIAL','OPEN_AR','PENDING_SYNC','MISSING_IN_QBO',
                                 'MISSING_IN_STRIPE','NON_STRIPE_PAYMENT','AMOUNT_MISMATCH',
                                 'DUPLICATE','UNRECORDED_REFUND','NEEDS_REVIEW')),
    is_exception              BOOLEAN NOT NULL,
    match_method              TEXT,
    counterpart_source_ids    TEXT,
    amount_cents              BIGINT NOT NULL,
    counterpart_amount_cents  BIGINT,
    diff_cents                BIGINT,
    currency                  TEXT NOT NULL,
    record_date               DATE,
    reason                    TEXT NOT NULL,
    PRIMARY KEY (run_id, record_type, record_id)
);

-- The reconciliation_exceptions view is created by finrecon/db.py because
-- PostgreSQL and SQLite disagree on view DDL (OR REPLACE vs IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS billing_checks (
    run_id              TEXT NOT NULL,
    client_id           UUID NOT NULL,
    location_id         UUID NOT NULL,
    billing_period      TEXT NOT NULL,
    check_key           TEXT NOT NULL,
    spend_cents         BIGINT,
    fee_cents           BIGINT,
    discount_cents      BIGINT,
    tax_cents           BIGINT,
    expected_cents      BIGINT,
    invoiced_cents      BIGINT,
    diff_cents          BIGINT,
    invoice_source_id   TEXT,
    data_status         TEXT,
    status              TEXT NOT NULL CHECK (status IN
                          ('BILLING_MATCHED','BILLING_MISMATCH','NOT_INVOICED',
                           'NO_INVOICE_EXPECTED','INVOICE_WITHOUT_SPEND','BILLING_DATA_UNAVAILABLE')),
    reason              TEXT NOT NULL,
    PRIMARY KEY (run_id, location_id, billing_period)
);

CREATE TABLE IF NOT EXISTS accounting_checks (
    run_id      TEXT NOT NULL,
    check_name  TEXT NOT NULL,
    ref         TEXT NOT NULL,
    passed      BOOLEAN NOT NULL,
    expected_cents BIGINT,
    actual_cents   BIGINT,
    detail      TEXT,
    PRIMARY KEY (run_id, check_name, ref)
);

CREATE TABLE IF NOT EXISTS evaluation_results (
    run_id     TEXT NOT NULL,
    dimension  TEXT NOT NULL,     -- transaction | data_status | billing
    item_key   TEXT NOT NULL,
    scenario   TEXT,
    expected   TEXT,
    actual     TEXT,
    outcome    TEXT NOT NULL CHECK (outcome IN ('correct','wrong','missed','unexpected')),
    PRIMARY KEY (run_id, dimension, item_key)
);

CREATE INDEX IF NOT EXISTS ix_invoices_client ON invoices (client_id, location_id);
CREATE INDEX IF NOT EXISTS ix_qbo_payments_client ON qbo_payments (client_id, txn_date);
CREATE INDEX IF NOT EXISTS ix_stripe_payments_client ON stripe_payments (client_id, effective_date);
CREATE INDEX IF NOT EXISTS ix_ad_spend_loc_date ON ad_spend (location_id, spend_date);
CREATE INDEX IF NOT EXISTS ix_recon_status ON reconciliation_results (run_id, status);
