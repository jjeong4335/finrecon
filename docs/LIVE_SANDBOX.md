# Live sandbox mode (QuickBooks Online + Stripe)

The offline demo needs no accounts. To run the same scenarios against the real sandbox APIs:

## 1. QuickBooks Online sandbox

1. Create a free Intuit Developer account and an app with the **Accounting** scope. A sandbox company is provisioned.
2. Copy the app's development **Client ID** and **Client Secret**, and the sandbox company's **Realm ID**.
3. Get a refresh token (e.g. with Intuit's OAuth 2.0 Playground) and save it:
   `mkdir -p .secrets && echo '{"refresh_token": "AB11..."}' > .secrets/qbo_tokens.json && chmod 600 .secrets/qbo_tokens.json`
   Refresh tokens rotate: the adapter writes the newest one back to this file on every refresh.

## 2. Stripe sandbox

Create a Stripe account, open a sandbox, copy its secret key (`sk_test_...`). The code refuses any non-test key.

## 3. Seed, ingest, capture

```bash
export MODE=live QBO_CLIENT_ID=... QBO_CLIENT_SECRET=... QBO_REALM_ID=... STRIPE_SECRET_KEY=sk_test_...
python -m seed.live_seed seed          # idempotent; re-running skips existing seed_keys
python -m finrecon.pipeline ingest --run-no 1 --as-of 2026-11-25
python -m finrecon.pipeline ingest --run-no 2 --as-of 2026-11-30
python -m finrecon.pipeline reconcile  # evaluation should again be 24/24
python -m seed.live_seed capture       # writes fixtures/live_snapshot/
```

Then deploy the snapshot so the public demo shows real sandbox data without live tokens:
`MODE=offline FIXTURE_DIR=fixtures/live_snapshot python -m finrecon.pipeline demo`.

## Notes and known differences

- **Dates.** QBO `TxnDate` is set to each scenario's date. Stripe objects cannot be back-dated, so the business date is
  stored in `metadata.event_date` and used as the payment's effective date.
- **Fees** come from Stripe's actual test-mode balance transactions, so amounts differ from the offline fixtures. No
  ground-truth item depends on fee amounts.
- **Payouts** may not occur in a sandbox; offline payouts are simulated and labelled as such.
- **QBO memo length.** `PaymentRefNum` is short, so the Stripe id is written to `PrivateNote`.
- **Status.** Run end to end against a QBO sandbox company and a Stripe sandbox: 27 / 27 scenarios, 226 / 226 items
  correct. QBO sandboxes ship with sample company data; those records map to no registered client and are counted
  as out of scope.
- **Lessons from the first live run.** Stripe rejects `payment_method_types` on PaymentIntents (use
  `automatic_payment_methods` with `allow_redirects=never`); Stripe keeps an idempotency key even when the request
  was rejected, so keys include a hash of the parameters; an HTTP 401 on token refresh means the client ID/secret
  is wrong (400 `invalid_grant` means the refresh token is).
