-- Public-data module: UCI Online Retail II (real historical retail transactions)
-- Chen, D. (2012). Online Retail II [Dataset]. UCI Machine Learning Repository.
-- https://doi.org/10.24432/C5CG6D  — licensed CC BY 4.0.
--
-- Layers
--   retail_raw_lines   every source row exactly as read (text), keyed by sheet + row number
--   retail_lines       parsed, classified, flagged; one row per raw row (nothing is dropped)
--   retail_invoices    invoice-level aggregation of the canonical lines
-- Source duplicates are flagged, never deleted: the same item can legitimately
-- appear twice on one invoice. Only rows duplicated by the *file layout*
-- (an invoice repeated identically in two sheets) are excluded from totals.

CREATE TABLE IF NOT EXISTS retail_load_runs (
    load_id           TEXT PRIMARY KEY,
    source_file       TEXT NOT NULL,
    source_sha256     TEXT NOT NULL,
    started_at        TIMESTAMPTZ NOT NULL,
    finished_at       TIMESTAMPTZ,
    status            TEXT NOT NULL,
    chunk_size        INTEGER NOT NULL,
    rows_read         INTEGER,
    rows_loaded       INTEGER,
    seconds_read_parse DOUBLE PRECISION,
    seconds_load      DOUBLE PRECISION,
    seconds_validate  DOUBLE PRECISION,
    rows_per_second   DOUBLE PRECISION,
    detail            TEXT
);

CREATE TABLE IF NOT EXISTS retail_raw_lines (
    source_row_id     TEXT PRIMARY KEY,      -- "<sheet>#<row number>"
    load_id           TEXT NOT NULL,
    sheet             TEXT NOT NULL,
    sheet_order       INTEGER NOT NULL,
    sheet_row         INTEGER NOT NULL,
    invoice_raw       TEXT,
    stock_code_raw    TEXT,
    description_raw   TEXT,
    quantity_raw      TEXT,
    invoice_date_raw  TEXT,
    price_raw         TEXT,
    customer_id_raw   TEXT,
    country_raw       TEXT,
    row_hash          TEXT NOT NULL          -- hash of all business fields (not sheet/row)
);

CREATE TABLE IF NOT EXISTS retail_lines (
    source_row_id        TEXT PRIMARY KEY REFERENCES retail_raw_lines(source_row_id),
    sheet                TEXT NOT NULL,
    sheet_order          INTEGER NOT NULL,
    invoice              TEXT,
    invoice_type         TEXT NOT NULL,      -- SALE | CANCELLATION | ADJUSTMENT | UNKNOWN
    stock_code           TEXT,
    is_product           BOOLEAN NOT NULL,
    description          TEXT,
    quantity             INTEGER,
    invoice_ts           TIMESTAMP,          -- as recorded (UK local time)
    unit_price           TEXT,               -- exact decimal text
    line_amount_cents    BIGINT,             -- pence, rounded half-up
    currency             TEXT NOT NULL,
    customer_id          TEXT,
    country              TEXT,
    line_class           TEXT NOT NULL CHECK (line_class IN
                           ('VALID_SALE','CANCELLATION','ADJUSTMENT','NON_PRODUCT','STOCK_ADJUSTMENT','INVALID')),
    invalid_reason       TEXT,
    missing_customer     BOOLEAN NOT NULL,
    missing_description  BOOLEAN NOT NULL,
    zero_price           BOOLEAN NOT NULL,
    sub_penny            BOOLEAN NOT NULL,
    possible_duplicate   BOOLEAN NOT NULL,
    overlap_excluded     BOOLEAN NOT NULL,   -- identical copy of an invoice already read from an earlier sheet
    overlap_conflict     BOOLEAN NOT NULL,   -- invoice appears in several sheets with different lines
    load_id              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retail_invoices (
    invoice             TEXT PRIMARY KEY,
    invoice_type        TEXT NOT NULL,
    customer_id         TEXT,
    customer_count      INTEGER NOT NULL,
    country             TEXT,
    first_ts            TIMESTAMP,
    line_count          INTEGER NOT NULL,
    product_lines       INTEGER NOT NULL,
    amount_cents        BIGINT NOT NULL,
    currency            TEXT NOT NULL,
    load_id             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retail_cancellation_links (
    source_row_id        TEXT PRIMARY KEY REFERENCES retail_lines(source_row_id),
    status               TEXT NOT NULL CHECK (status IN ('LINKED','NO_PRIOR_SALE','MISSING_CUSTOMER')),
    load_id              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retail_quality_results (
    load_id        TEXT NOT NULL,
    check_name     TEXT NOT NULL,
    rows_affected  BIGINT,
    amount_cents   BIGINT,
    share          DOUBLE PRECISION,
    passed         BOOLEAN NOT NULL,
    severity       TEXT NOT NULL,
    detail         TEXT,
    PRIMARY KEY (load_id, check_name)
);

CREATE INDEX IF NOT EXISTS ix_retail_raw_hash ON retail_raw_lines (sheet, row_hash);
CREATE INDEX IF NOT EXISTS ix_retail_raw_invoice ON retail_raw_lines (invoice_raw, sheet_order);
CREATE INDEX IF NOT EXISTS ix_retail_lines_invoice ON retail_lines (invoice);
CREATE INDEX IF NOT EXISTS ix_retail_lines_cust_stock ON retail_lines (customer_id, stock_code, invoice_ts);
