-- Level 1: exact matches on a verified cross-system identifier.
-- The payment sync writes the Stripe PaymentIntent id into the QBO payment memo;
-- ingestion parses it into qbo_payments.referenced_stripe_id.
-- When several QBO payments reference the same Stripe payment, one is the match
-- (rn = 1) and the rest are extra records (rn > 1). Ranking: first a record of the
-- same client and currency (a reference recorded under another client must never
-- take the match from a valid one), then the record whose amount agrees with
-- Stripe, then the earliest.
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
        ORDER BY CASE WHEN q.client_id = s.client_id AND q.currency = s.currency THEN 0 ELSE 1 END,
                 CASE WHEN q.total_cents = s.amount_received_cents THEN 0 ELSE 1 END,
                 q.txn_date, CAST(q.source_payment_id AS BIGINT)
    )                           AS rn
FROM qbo_payments q
JOIN stripe_payments s
  ON s.source_payment_id = q.referenced_stripe_id
WHERE s.status = 'succeeded'
  AND q.client_id IS NOT NULL          -- in scope: mapped to a registered client
  AND s.client_id IS NOT NULL
