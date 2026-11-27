-- Level 2: candidate pairs for card payments recorded in QBO without a Stripe id.
-- Same internal client, same currency, same amount, neither side already
-- matched at Level 1. The date tolerance, the uniqueness rule and the
-- corroboration checks (location, invoice reference) are applied in Python
-- (recon/engine.py) so ambiguity is decided explicitly, never silently.
SELECT
    q.payment_id             AS qbo_payment_id,
    q.source_payment_id      AS qbo_source_id,
    q.txn_date               AS qbo_date,
    s.payment_id             AS stripe_payment_id,
    s.source_payment_id      AS stripe_source_id,
    s.effective_date         AS stripe_date,
    q.total_cents            AS amount_cents,
    q.location_id            AS qbo_location_id,
    s.location_id            AS stripe_location_id,
    s.qbo_invoice_ref        AS stripe_invoice_ref
FROM qbo_payments q
JOIN stripe_payments s
  ON s.client_id = q.client_id
 AND s.currency = q.currency
 AND s.amount_received_cents = q.total_cents
WHERE q.referenced_stripe_id IS NULL
  AND q.client_id IS NOT NULL
  AND q.payment_method_type = 'CREDIT_CARD'
  AND s.status = 'succeeded'
  AND NOT EXISTS (SELECT 1 FROM payment_matches m
                   WHERE m.run_id = ? AND m.stripe_payment_id = s.payment_id)
  AND NOT EXISTS (SELECT 1 FROM qbo_payments q2
                   WHERE q2.referenced_stripe_id = s.source_payment_id)
