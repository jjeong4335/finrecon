-- Cross-system view: every Stripe payment and every QBO payment on one row
-- each side, joined on the verified reference. Rows with NULL on one side are
-- the "exists in one system, missing from the other" candidates (interview Q4).
-- Final classification (sync window, payment method, candidates) happens in
-- the engine; this query is the starting point and is shown on the dashboard.
SELECT
    COALESCE(s.client_id, q.client_id)   AS client_id,
    s.source_payment_id                  AS stripe_payment_id,
    s.amount_received_cents              AS stripe_amount_cents,
    s.effective_date                     AS stripe_date,
    q.source_payment_id                  AS qbo_payment_id,
    q.total_cents                        AS qbo_amount_cents,
    q.txn_date                           AS qbo_date,
    q.payment_method_name                AS qbo_method,
    CASE
        WHEN q.payment_id IS NULL THEN 'only_in_stripe'
        WHEN s.payment_id IS NULL THEN 'only_in_qbo'
        WHEN s.amount_received_cents <> q.total_cents THEN 'amount_differs'
        ELSE 'both'
    END                                  AS presence
FROM stripe_payments s
FULL OUTER JOIN qbo_payments q
  ON q.referenced_stripe_id = s.source_payment_id
 AND q.client_id = s.client_id
WHERE COALESCE(s.client_id, q.client_id) IS NOT NULL
ORDER BY client_id, COALESCE(s.effective_date, q.txn_date), COALESCE(s.source_payment_id, ''), COALESCE(q.source_payment_id, '')
