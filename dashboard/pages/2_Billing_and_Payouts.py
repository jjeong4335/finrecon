"""Ad spend → invoice reconciliation, gross-to-net, payouts."""
import pandas as pd
import streamlit as st

from common import dd, money_cols, money_config, setup, styled

db, run = setup("Billing & Payouts")
run_id = run["run_id"]

st.subheader("Ad spend → invoice")
st.caption("Expected invoice = ad spend + management fee (with minimum) − discount + tax, per location-month. "
           "The billing rules are synthetic. Ad spend is never compared with cash directly.")
rules = pd.DataFrame(dd.billing_rules(db))
rules["min_fee"] = rules.pop("min_fee_cents") / 100
for col in ("mgmt_fee_bps", "discount_bps", "tax_bps"):
    rules[col.replace("_bps", " %")] = rules.pop(col) / 100
st.dataframe(rules, hide_index=True, use_container_width=True,
             column_config={"min_fee": st.column_config.NumberColumn("Minimum fee", format="$%.2f")})

bill = money_cols(pd.DataFrame(dd.billing(db, run_id)),
                  ["spend_cents", "fee_cents", "discount_cents", "tax_cents", "expected_cents", "invoiced_cents",
                   "diff_cents"])
only_issues = st.toggle("Only checks that are not BILLING_MATCHED", value=True)
if only_issues:
    bill = bill[bill["status"] != "BILLING_MATCHED"]
st.dataframe(styled(bill, ("status", "data_status")), hide_index=True, use_container_width=True,
             column_config=money_config(bill))

st.subheader("Stripe gross → fees → net → payouts")
po = money_cols(pd.DataFrame(dd.payouts(db)), ["amount_cents", "gross_cents", "fee_cents", "refund_cents", "net_cents"])
po = po.rename(columns={"amount": "payout"})
st.dataframe(po, hide_index=True, use_container_width=True, column_config=money_config(po))
if not po.empty and po["simulated"].any():
    st.caption("Payouts marked simulated were derived from balance transactions in offline mode.")

st.subheader("Accounting checks")
acct = pd.DataFrame(dd.accounting_summary(db, run_id))
acct["all passed"] = acct["n_passed"] == acct["total"]
st.dataframe(acct, hide_index=True)
st.caption("gross − fee = net for every balance transaction · charge gross = PaymentIntent amount · "
           "payout = Σ net of the transactions Stripe attached to it. Journal-entry previews are on the "
           "Reconciliation page (select a Stripe payment).")
