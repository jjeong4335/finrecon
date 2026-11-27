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
python -m finrecon.pipeline reconcile  # evaluation should again be 27/27
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

## 4. Re-verifying the deployed site against the live APIs

`python -m finrecon.verify_live` (with `MODE=live` and the sandbox credentials loaded) re-captures both sandboxes,
diffs them object by object against `fixtures/live_snapshot` (what the deployed site serves), reconciles both
offline and compares every result, and checks each Stripe payout against the net of its balance transactions.
Nothing on the server is touched; exit code 1 means a difference.

Results on 27 Nov 2026:

- **No drift.** All 11 QBO and Stripe entities were identical to the deployed snapshot; both reconciled to
  27 / 27 scenarios and 226 / 226 items, and all 151 reconciliation results matched in status and amount.
- **Detection of a new gap.** A $123.47 test charge was then created in Stripe for a registered client with no QBO
  payment. The re-capture showed exactly one new PaymentIntent, Charge and balance transaction; reconciled as of
  27 Nov it was `PENDING_SYNC` (inside the 3-day sync window) and as of 30 Nov `MISSING_IN_QBO`, while the 151 existing
  results were unchanged. The charge is left in the sandbox, so later runs of `verify_live` report it as a difference
  until the snapshot is re-captured.
- **Payouts not yet verified.** The sandbox has made no payout. `python -m seed.live_seed payouts` adds charges to the
  available balance under a separate, unregistered customer; once Stripe's automatic payout runs, `verify_live`
  reports `payout_equals_sum_of_net` for each payout.
