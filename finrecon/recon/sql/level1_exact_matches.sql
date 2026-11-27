-- Level 1: exact matches on a verified cross-system identifier.
-- The payment sync writes the Stripe PaymentIntent id into the QBO payment memo;
-- ingestion parses it into qbo_payments.referenced_stripe_id.
-- When several QBO payments reference the same Stripe payment, the earliest is
-- the match (rn = 1) and the rest are duplicate imports (rn > 1).
SELECT
    q.payment_id                AS qbo_payment_id,
    q.source_payment_id         AS qbo_source_id,
    s.payment_id                AS stripe_payment_id,
    s.source_payment_id         AS stripe_source_id,
    q.total_cents               AS qbo_amount_cents,
    s.amount_received_cents     AS stripe_amount_cents,
    q.client_id                 AS qbo_client_id,
    s.client_id                 AS stripe_client_id,
    q.currency                  AS qbo_currency,
    s.currency                  AS stripe_currency,
    ROW_NUMBER() OVER (
        PARTITION BY s.payment_id
        ORDER BY q.txn_date, CAST(q.source_payment_id AS BIGINT)
    )                           AS rn
FROM qbo_payments q
JOIN stripe_payments s
  ON s.source_payment_id = q.referenced_stripe_id
WHERE s.status = 'succeeded'
