"""
Propose, confirm and place the next T-bill ladder rung.

Reads the same state as the daily T-bill ladder report (cash, buffer,
deployable cash, gaps), finds the best bill for the target month from
TreasuryDirect + IB, prices it (IB ask capped by a yield floor), previews
commission / margin impact with IB's what-if, and only then asks:

    Place this order? y/n

Usage:
    python3 sysproduction/interactive_tbill_ladder.py            # live
    python3 sysproduction/interactive_tbill_ladder.py --dry-run  # never places

The guards in the report's ACTION logic apply here too: negative cash,
failed collateral check or deployable cash under the minimum purchase all
stop before anything is proposed.
"""

import sys

import numpy as np
import pandas as pd

from syscore.interactive.input import (
    get_input_from_user_and_convert_to_type,
    true_if_answer_is_yes,
)
from syscore.interactive.display import print_with_landing_strips_around
from syslogdiag.email_via_db_interface import send_production_mail_msg

from sysdata.data_blob import dataBlob
from sysproduction.reporting.data.bond_holdings import (
    compute_ladder_state,
    ib_units_from_face,
    maturity_dates_from_bond_df,
)
from sysproduction.tbill_ladder import (
    get_purchase_settings,
    target_month_and_reason,
    purchase_face,
    fetch_treasurydirect_bills,
    fetch_ib_bill_universe,
    build_candidate_table,
    candidates_for_month,
    get_quote,
    limit_price_with_floor,
    implied_yield_for_row,
    choose_bill,
    build_proposal,
    proposal_text,
    what_if_buy,
    place_limit_buy,
    wait_for_fill,
)

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", None)


def interactive_tbill_ladder(dry_run: bool = False):
    with dataBlob(log_name="Interactive-Tbill-Ladder") as data:
        if dry_run:
            print_with_landing_strips_around("DRY RUN - no order will be placed")

        state = compute_ladder_state(data)
        _print_state(state)

        if not _passes_guards(state):
            return

        settings = state["settings"]
        purchase_settings = get_purchase_settings(data)
        target_month, target_reason = target_month_and_reason(
            state["gaps"],
            state["today"],
            settings["ladder_months"],
            maturity_dates=maturity_dates_from_bond_df(state["bond_df"]),
        )
        face = purchase_face(state["spare"], settings["rounding"])
        print(
            "\nTarget month %s (%s); deployable %s -> face %s"
            % (
                target_month,
                target_reason,
                format(round(state["spare"]), ","),
                format(round(face), ","),
            )
        )

        cands = _find_candidates(data, state, target_month, purchase_settings)
        if cands is None:
            return

        chosen_idx = choose_bill(cands)
        chosen_idx = _let_user_pick(cands, chosen_idx)
        chosen = cands.loc[chosen_idx]

        limit_price, limit_reason = limit_price_with_floor(
            chosen["ask"],
            chosen["auction_yield_pct"],
            chosen["days"],
            purchase_settings["yield_tolerance_pct"],
            pad=purchase_settings["limit_pad"],
        )
        if limit_price != limit_price:
            print(
                "Cannot price %s: %s. Nothing placed." % (chosen["cusip"], limit_reason)
            )
            return
        if chosen["ask"] != chosen["ask"]:
            print(
                "WARNING: no IB quote for this bill (market closed or no bond data). "
                "Limit is derived from the auction yield; a DAY order may be rejected "
                "outside trading hours."
            )

        face = get_input_from_user_and_convert_to_type(
            "Face value to buy (multiples of 1,000)?",
            type_expected=float,
            allow_default=True,
            default_value=face,
        )
        if ib_units_from_face(face) <= 0:
            print("Face under 1,000: nothing to buy.")
            return

        proposal = build_proposal(
            face, chosen, limit_price, limit_reason, target_month, target_reason
        )
        print("")
        print_with_landing_strips_around("PROPOSED ORDER")
        print(proposal_text(proposal, state["base_cash"], state["buffer"]))

        ib = data.ib_conn.ib
        contract = _ib_contract_for(ib, proposal["conId"])
        _print_what_if(ib, contract, proposal, state["account_id"])

        if dry_run:
            print_with_landing_strips_around("DRY RUN - order NOT placed")
            return

        if not true_if_answer_is_yes("Place this order? y/n: "):
            print("Not placed.")
            return

        _place_and_report(data, ib, contract, proposal, state, purchase_settings)


def _print_state(state: dict):
    def _f(v):
        return "n/a" if v is None or v != v else format(round(float(v)), ",")

    print_with_landing_strips_around("T-BILL LADDER - current state")
    bond_df = state["bond_df"]
    if len(bond_df):
        print(
            bond_df[
                [
                    "maturity",
                    "days_to_maturity",
                    "face",
                    "mark_price",
                    "approx_yield_pct",
                ]
            ]
        )
    else:
        print("No bills held.")
    print(
        "\n%s cash %s | buffer %s (%s) | deployable %s"
        % (
            state["base_currency"],
            _f(state["base_cash"]),
            _f(state["buffer"]),
            state["buffer_text"],
            _f(state["spare"]),
        )
    )
    print(
        "Gaps over next %d months: %s"
        % (state["ladder_months"], ", ".join(state["gaps"]) or "none")
    )
    if state["negative_ccys"]:
        print("NEGATIVE non-base balances: %s" % ", ".join(state["negative_ccys"]))
    print("Report says: %s" % state["action"])


def _passes_guards(state: dict) -> bool:
    if not state["balances_ok"]:
        print("Cannot read broker balances - stopping.")
        return False
    if state["base_cash"] < 0:
        print("Base cash is negative - stopping (let the next rung land as cash).")
        return False
    if not state["collateral_ok"]:
        print("Collateral check failed - stopping (see report).")
        return False
    rounding = state["settings"]["rounding"]
    floor = max(rounding, float(state["settings"].get("min_purchase", rounding)))
    face = purchase_face(state["spare"], rounding)
    if face < floor:
        print(
            "Deployable cash gives only %s, under the minimum purchase of %s "
            "- nothing to buy (set tbill_min_purchase in private config to "
            "change)." % (format(round(face), ","), format(round(floor), ","))
        )
        return False
    return True


def _find_candidates(data: dataBlob, state: dict, target_month: str, purchase_settings):
    print("\nFetching bill list from TreasuryDirect and IB...")
    try:
        td_bills = fetch_treasurydirect_bills()
    except BaseException as e:
        print("TreasuryDirect fetch failed: %s" % e)
        return None
    ib = data.ib_conn.ib
    ib_universe = fetch_ib_bill_universe(ib)
    table = build_candidate_table(td_bills, ib_universe, state["today"])
    print(
        "%d bills at TreasuryDirect, %d tradeable at IB, %d unmatured candidates"
        % (len(td_bills), len(ib_universe), len(table))
    )
    cands = candidates_for_month(
        table, target_month, purchase_settings["month_slack_days"]
    )
    if len(cands) == 0:
        print("No bill matures in or near %s - nothing to propose." % target_month)
        return None

    print("Quoting %d candidate(s)..." % len(cands))
    asks, bids = [], []
    for _, row in cands.iterrows():
        q = get_quote(
            ib, ib_universe[row["cusip"]], purchase_settings["quote_wait_seconds"]
        )
        asks.append(q["ask"])
        bids.append(q["bid"])
    cands = cands.assign(bid=bids, ask=asks)
    cands = cands.assign(
        ask_yield_pct=[
            implied_yield_for_row(a, d) for a, d in zip(cands["ask"], cands["days"])
        ]
    )
    print("\nCandidates for %s:" % target_month)
    print(
        cands[
            [
                "cusip",
                "maturity",
                "days",
                "term",
                "auction_yield_pct",
                "bid",
                "ask",
                "ask_yield_pct",
            ]
        ]
    )
    return cands


def _let_user_pick(cands: pd.DataFrame, default_idx):
    print(
        "\nBest by yield: row %s (%s)" % (default_idx, cands.loc[default_idx, "cusip"])
    )
    idx = get_input_from_user_and_convert_to_type(
        "Row number to buy?",
        type_expected=int,
        allow_default=True,
        default_value=int(default_idx),
    )
    if idx not in cands.index:
        print("Row %s not in table, using %s" % (idx, default_idx))
        return default_idx
    return idx


def _ib_contract_for(ib, con_id: int):
    from ib_async import Contract

    cds = ib.reqContractDetails(Contract(conId=int(con_id)))
    if not cds:
        raise Exception("IB returned no contract for conId %s" % con_id)
    return cds[0].contract


def _fmt_ib_value(v):
    """IB uses float-max (1.79e308) as 'not available' in what-if results."""
    try:
        v = float(v)
    except BaseException:
        return "n/a"
    if v != v or abs(v) > 1e300:
        return "n/a"
    return format(round(v, 2), ",")


def _print_what_if(ib, contract, proposal: dict, account: str):
    try:
        w = what_if_buy(
            ib, contract, proposal["units"], proposal["limit_price"], account
        )
    except BaseException as e:
        print("What-if check failed: %s" % e)
        return
    print(
        "IB what-if: commission %s, init margin change %s, maint margin change %s%s"
        % (
            _fmt_ib_value(w.get("commission")),
            w.get("initMarginChange"),
            w.get("maintMarginChange"),
            (" WARNING: %s" % w["warningText"]) if w.get("warningText") else "",
        )
    )


def _place_and_report(data, ib, contract, proposal, state, purchase_settings):
    trade = place_limit_buy(
        ib,
        contract,
        proposal["units"],
        proposal["limit_price"],
        state["account_id"],
    )
    data.log.warning(
        "Placed T-bill order: BUY %d units CUSIP %s at %.5f (order id %s)"
        % (
            proposal["units"],
            proposal["cusip"],
            proposal["limit_price"],
            trade.order.orderId,
        )
    )
    print(
        "Order placed (id %s); waiting up to %ds for a fill..."
        % (trade.order.orderId, purchase_settings["fill_wait_seconds"])
    )
    result = wait_for_fill(ib, trade, purchase_settings["fill_wait_seconds"])
    print("Order status: %s" % result)
    if not result["done"]:
        print(
            "Order still working (DAY limit). Check TWS; it will expire at the "
            "close if unfilled. Re-run tomorrow if needed."
        )

    body = "%s\n\nResult: %s\n\nPlaced by interactive_tbill_ladder." % (
        proposal_text(proposal, state["base_cash"], state["buffer"]),
        result,
    )
    try:
        send_production_mail_msg(
            data,
            body,
            "T-bill ladder order %s: %s" % (proposal["cusip"], result["status"]),
        )
    except BaseException as e:
        print("Could not send email: %s" % e)


def interactive_tbill_ladder_live():
    """Propose the next T-bill rung, confirm with y/n, then place it."""
    interactive_tbill_ladder(dry_run=False)


def interactive_tbill_ladder_dry_run():
    """Propose and price the next T-bill rung; never places an order."""
    interactive_tbill_ladder(dry_run=True)


if __name__ == "__main__":
    interactive_tbill_ladder(dry_run="--dry-run" in sys.argv)
