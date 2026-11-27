-- QBO payments whose memo names a Stripe payment that does not exist in Stripe.
-- (e.g. a declined card payment recorded as received)
SELECT q.payment_id, q.source_payment_id, q.referenced_stripe_id
FROM qbo_payments q
LEFT JOIN stripe_payments s
  ON s.source_payment_id = q.referenced_stripe_id
WHERE q.referenced_stripe_id IS NOT NULL
  AND q.client_id IS NOT NULL
  AND s.payment_id IS NULL
