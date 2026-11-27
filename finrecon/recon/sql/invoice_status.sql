-- Invoice settlement from QBO payment allocations (Line.LinkedTxn).
-- Allocations are summed per invoice, so a payment covering two invoices is
-- counted once per invoice at its allocated amount, never at its full total.
SELECT
    i.invoice_id,
    i.source_invoice_id,
    i.doc_number,
    i.client_id,
    i.location_id,
    i.total_cents,
    i.balance_cents,
    i.currency,
    i.txn_date,
    i.due_date,
    i.scenario_id,
    COALESCE(a.applied_cents, 0)   AS applied_cents,
    COALESCE(a.payment_count, 0)   AS payment_count,
    a.payment_ids
FROM invoices i
LEFT JOIN (
    SELECT pa.invoice_id,
           SUM(pa.amount_cents)                         AS applied_cents,
           COUNT(*)                                     AS payment_count,
           STRING_AGG(q.source_payment_id, ',' ORDER BY q.source_payment_id) AS payment_ids
    FROM payment_allocations pa
    JOIN qbo_payments q ON q.payment_id = pa.qbo_payment_id
    GROUP BY pa.invoice_id
) a ON a.invoice_id = i.invoice_id
