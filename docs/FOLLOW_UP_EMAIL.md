# Follow-up email draft

**Subject:** Follow-up: a small reconciliation prototype based on our conversation

Hi [Name],

Thank you again for the conversation last week. Your questions about pulling reports for many clients and
locations, and about finding transactions that exist in one system but not another, stayed with me, so I built a
small working version to think them through properly.

It ingests QuickBooks Online and Stripe sandbox data plus a mock ad-platform API, maps everything to internal
client and location IDs, and reconciles the systems: exact matches on the Stripe ID, cautious candidate matching,
and typed exceptions such as missing syncs, duplicate imports, unrecorded refunds and invoices billed incorrectly.
Failed or partial API pulls are flagged as missing data rather than shown as zero.

- Live demo: [https://your-domain]
- Code and write-up: [GitHub link]

The quickest thing to look at is the Reconciliation page: filter by "MISSING_IN_QBO" and select the row to see why
it was flagged. All data is synthetic.

I'd be glad to walk through any part of it, or to hear how your team approaches this today.

Best regards,
[Your name]
