# Design notes: reconciliation (`finrecon/recon/`)

**What it does.** Classifies every invoice, Stripe payment, QBO payment and refund; checks billing against ad spend;
checks gross/fee/net and payouts; evaluates against ground truth.

**Key decisions**
- *Match on a verified identifier first.* The payment sync writes the Stripe PaymentIntent id into the QBO memo. A
  shared reference beats any heuristic.
- *The record that agrees is the match.* If several QBO records cite the same Stripe id, the one whose amount
  agrees with Stripe is the match, otherwise the earliest; the rest are duplicates. A wrong first import followed by
  a correct one must not leave the correct record flagged.
- *Candidates must be corroborated, not just unique.* If both sides name a location and they differ, or Stripe
  names an invoice that QBO did not apply the payment to, the pair goes to `NEEDS_REVIEW` even when it is the only one.
- *Candidate matching must be mutually unique.* Same client, currency and amount within ±3 days. If a QBO entry has
  two candidates, or a Stripe payment is a candidate for two entries, both sides go to `NEEDS_REVIEW`. Wrong
  auto-matches are worse than open items.
- *Payment method matters.* A QBO payment by check is `NON_STRIPE_PAYMENT`, not "missing in Stripe".
- *Time windows.* A Stripe payment from yesterday is `PENDING_SYNC`, not an exception. All time logic reads
  `RECON_AS_OF`.
- *Invoices from allocations.* Fully paid / partial / open, with aging and a hint when an unsynced Stripe payment
  references the invoice.
- *Billing is a separate check.* Spend → expected invoice uses an explicit rule table; spend is never compared to cash.
- *SQL for set logic, Python for decisions.* Exact matches, duplicates (`ROW_NUMBER`), orphans, candidate pairs and
  invoice totals are SQL files; uniqueness and window rules are Python, where they are easier to read and test.
- *Every result carries a reason, counterpart IDs, amounts and the run ID.*

**Evaluator.** Ground truth is written from the scenario plan, keyed by `seed_key`, never from engine output. Tests
deliberately corrupt results and confirm the evaluator reports wrong, missed and false-positive items.
