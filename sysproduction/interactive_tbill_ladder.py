"""
Propose, confirm and place the next T-bill ladder rung.

Reads the same state as the daily T-bill ladder report (cash, buffer,
deployable cash, gaps), finds the best bill for the target month from
TreasuryDirect + IB, prices it (IB ask capped by a yield floor), previews
commission / margin impact with IB's what-if, and only then asks:

    Place this order? y/n

If IB's what-if rejects the preview (e.g. no trading permission) it stops
there instead of asking. If the order has not filled after the wait, it asks
what to do: cancel and resend it as a fresh order (the default), switch to
the next-best bill for the same month, raise the limit one step (never past
the yield floor), leave it working until the close, or cancel.

Why resend is the default (seen 2026-10-06, 7 orders): odd-lot bill orders
either fill within seconds of arriving at IB or not at all. A resting order
never filled, re-pricing a resting order did not help in 6 minutes, and a
fresh order for the very bill and size that had hung filled in 5 seconds.

Usage:
    python3 sysproduction/interactive_tbill_ladder.py            # live
    python3 sysproduction/interactive_tbill_ladder.py --dry-run  # never places
    python3 sysproduction/interactive_tbill_ladder.py --cancel   # cancel a working
                                                                  # bill order

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
    BILL_FACE_PER_UNIT,
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
    choose_bill_with_reason,
    spread_bp,
    next_limit_step,
    what_if_problem,
    build_proposal,
    proposal_text,
    what_if_buy,
    place_limit_buy,
    modify_limit,
    cancel_and_wait,
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

        if not _no_working_bill_orders(data.ib_conn.ib, dry_run):
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

        chosen_idx, pick_reason = choose_bill_with_reason(cands)
        chosen_idx = _let_user_pick(cands, chosen_idx, pick_reason)
        chosen = cands.loc[chosen_idx]

        limit_price, limit_reason = _limit_for(chosen, purchase_settings)
        if limit_price != limit_price:
            return

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
        if not _cost_within_deployable(proposal, state):
            return
        ib = data.ib_conn.ib
        contract = _show_proposal_and_preview(ib, proposal, state)
        if contract is None:
            return

        if dry_run:
            print_with_landing_strips_around("DRY RUN - order NOT placed")
            return

        if not true_if_answer_is_yes("Place this order? y/n: "):
            print("Not placed.")
            return

        _place_and_report(data, ib, contract, proposal, state, purchase_settings, cands)


def _no_working_bill_orders(ib, dry_run: bool) -> bool:
    """
    The ladder state counts filled bills and cash only: a bill order still
    working at IB is neither, so its month still looks like a gap and its cash
    still looks deployable. Proposing on top of it would double-buy.
    """
    try:
        working = [
            t
            for t in ib.reqAllOpenOrders()
            if t.contract.secType in ("BILL", "BOND") and not t.isDone()
        ]
    except BaseException as e:
        # fail closed: without the open-order list we cannot rule out a
        # working bill order, and proposing on top of one would double-buy
        print(
            "Could not list open orders (%s) - STOPPING. Check TWS for working "
            "bill orders and re-run." % e
        )
        return False
    if not working:
        return True
    print("")
    print_with_landing_strips_around("BILL ORDERS STILL WORKING AT IB")
    for t in working:
        print(
            "  order %s: %s %s units, conId %s, limit %s, status %s, filled %s"
            % (
                t.order.orderId,
                t.order.action,
                format(t.order.totalQuantity, "g"),
                t.contract.conId,
                t.order.lmtPrice,
                t.orderStatus.status,
                t.orderStatus.filled,
            )
        )
    print(
        "Their months still show as gaps and their cash as deployable, so a new "
        "proposal could buy the same rung twice. Let them fill or cancel them first."
    )
    if dry_run:
        return True
    return true_if_answer_is_yes("Propose another order anyway? y/n: ")


def _cost_within_deployable(proposal: dict, state: dict) -> bool:
    """
    The face prompt accepts any number: refuse an order whose cost is more
    than the deployable cash (cash net of negative balances, minus the buffer),
    e.g. a typo with an extra zero. Fails closed on a missing number.
    """
    cost, spare = proposal["cost"], state["spare"]
    try:
        ok = float(cost) <= float(spare)  # False on NaN
    except BaseException:
        ok = False
    if not ok:
        print(
            "Cost ~%s is more than the deployable cash %s - nothing placed. "
            "Enter a smaller face value." % (_fmt_ib_value(cost), _fmt_ib_value(spare))
        )
    return ok


def _limit_for(chosen: pd.Series, purchase_settings: dict):
    """(price, reason) for a candidate row; prints why and returns NaN if unpriceable."""
    limit_price, limit_reason = limit_price_with_floor(
        chosen["ask"],
        chosen["auction_yield_pct"],
        chosen["days"],
        purchase_settings["yield_tolerance_pct"],
        pad=purchase_settings["limit_pad"],
    )
    if limit_price != limit_price:
        print("Cannot price %s: %s. Nothing placed." % (chosen["cusip"], limit_reason))
        return limit_price, limit_reason
    if chosen["ask"] != chosen["ask"]:
        print(
            "WARNING: no IB quote for this bill (market closed or no bond data). "
            "Limit is derived from the auction yield; a DAY order may be rejected "
            "outside trading hours."
        )
    return limit_price, limit_reason


def _show_proposal_and_preview(ib, proposal: dict, state: dict):
    """
    Prints the proposal and IB's what-if. Returns the IB contract, or None if
    the what-if says the order cannot go through (nothing to confirm then).
    """
    print("")
    print_with_landing_strips_around("PROPOSED ORDER")
    print(proposal_text(proposal, state["base_cash"], state["buffer"]))
    contract = _ib_contract_for(ib, proposal["conId"])
    problem = _print_what_if(ib, contract, proposal, state["account_id"])
    if problem:
        print("STOPPING - %s. Nothing placed." % problem)
        print(
            "If this is a new account, check Client Portal > Settings > Trading "
            "Permissions > Bonds (United States)."
        )
        return None
    return contract


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
    if state.get("collateral_unverified", True):
        print(
            "WARNING: could not read margin tags from IB, so the collateral check "
            "did not run - it is unknown whether IB fully credits bills toward "
            "margin."
        )
        if not true_if_answer_is_yes("Continue without the collateral check? y/n: "):
            print("Stopping.")
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
        ],
        spread_bp=[
            spread_bp(b, a, d)
            for b, a, d in zip(cands["bid"], cands["ask"], cands["days"])
        ],
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
                "spread_bp",
            ]
        ]
    )
    print("(spread_bp = bid-ask spread in bp of yield; tighter = easier to fill)")
    return cands


def _let_user_pick(cands: pd.DataFrame, default_idx, reason: str = "best yield"):
    print(
        "\nSuggested: row %s (%s) - %s"
        % (default_idx, cands.loc[default_idx, "cusip"], reason)
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


def _print_what_if(ib, contract, proposal: dict, account: str) -> str:
    """Prints IB's what-if; returns a reason the order cannot go through, or ""."""
    try:
        w = what_if_buy(
            ib, contract, proposal["units"], proposal["limit_price"], account
        )
    except BaseException as e:
        print("What-if check failed: %s" % e)
        return "the what-if check itself failed (%s)" % e
    print(
        "IB what-if: commission %s, init margin change %s, maint margin change %s%s"
        % (
            _fmt_ib_value(w.get("commission")),
            w.get("initMarginChange"),
            w.get("maintMarginChange"),
            (" WARNING: %s" % w["warningText"]) if w.get("warningText") else "",
        )
    )
    return what_if_problem(w)


def _place_and_report(data, ib, contract, proposal, state, purchase_settings, cands):
    trade = _place(data, ib, contract, proposal, state["account_id"])
    result = _wait(ib, trade, purchase_settings)
    if not result["done"]:
        trade, proposal, result = _handle_unfilled(
            data, ib, trade, proposal, state, purchase_settings, cands
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


def _place(data, ib, contract, proposal, account_id: str):
    trade = place_limit_buy(
        ib,
        contract,
        proposal["units"],
        proposal["limit_price"],
        account_id,
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
    return trade


def _wait(ib, trade, purchase_settings) -> dict:
    print(
        "Order id %s working; waiting up to %ds for a fill..."
        % (trade.order.orderId, purchase_settings["fill_wait_seconds"])
    )
    result = wait_for_fill(ib, trade, purchase_settings["fill_wait_seconds"])
    print("Order status: %s" % result)
    return result


UNFILLED_MENU_DEFAULT = "n"


def _handle_unfilled(data, ib, trade, proposal, state, purchase_settings, cands):
    """
    The order is still working after the wait. Ask what to do, repeatedly,
    until it fills, is cancelled, or the user leaves it working.
    Returns (trade, proposal, result) for whatever order is current at the end.
    """
    tried = {proposal["cusip"]}
    while True:
        result = wait_for_fill(ib, trade, 0)
        if result["done"]:
            return trade, proposal, result

        step_price, step_reason = next_limit_step(
            trade.order.lmtPrice,
            proposal["auction_yield_pct"],
            proposal["days"],
            purchase_settings["yield_tolerance_pct"],
            purchase_settings["reprice_step"],
        )
        alt_idx = _next_best_idx(cands, tried)
        lines, options = _unfilled_options(
            trade, proposal, result, step_price, step_reason, cands, alt_idx
        )
        print("")
        print_with_landing_strips_around("ORDER NOT FILLED YET")
        print("\n".join(lines))
        choice = (
            get_input_from_user_and_convert_to_type(
                "Choose %s?" % "/".join(options.keys()),
                type_expected=str,
                allow_default=True,
                default_value=UNFILLED_MENU_DEFAULT,
            )
            .strip()
            .lower()[:1]
        )

        if choice == "n":
            replaced = _replace_order(
                data,
                ib,
                trade,
                proposal,
                state,
                purchase_settings,
                _row_for_cusip(cands, proposal["cusip"]),
            )
            if replaced is not None:
                trade, proposal = replaced
                _wait(ib, trade, purchase_settings)
        elif choice == "r" and "r" in options:
            old = trade.order.lmtPrice
            modify_limit(ib, trade, step_price)
            data.log.warning(
                "Re-priced T-bill order %s (%s): limit %.5f -> %.5f"
                % (trade.order.orderId, proposal["cusip"], old, step_price)
            )
            proposal = dict(
                proposal,
                limit_price=step_price,
                limit_reason="re-priced: %s" % step_reason,
                implied_yield_pct=implied_yield_for_row(step_price, proposal["days"]),
            )
            _wait(ib, trade, purchase_settings)
        elif choice == "s" and "s" in options:
            switched = _replace_order(
                data,
                ib,
                trade,
                proposal,
                state,
                purchase_settings,
                cands.loc[alt_idx],
            )
            tried.add(cands.loc[alt_idx, "cusip"])
            if switched is not None:
                trade, proposal = switched
                _wait(ib, trade, purchase_settings)
        elif choice == "c":
            result = cancel_and_wait(ib, trade)
            data.log.warning(
                "Cancelled T-bill order %s (%s): filled %s of %s"
                % (
                    trade.order.orderId,
                    proposal["cusip"],
                    result["filled"],
                    proposal["units"],
                )
            )
            print("Cancelled. Final status: %s" % result)
            return trade, proposal, result
        elif choice == "w":
            print(
                "Left working (DAY limit at %.5f): it expires at the close if "
                "unfilled. Check TWS or re-run tomorrow." % trade.order.lmtPrice
            )
            return trade, proposal, result
        else:
            print("Not an option: %r" % choice)


def _unfilled_options(
    trade, proposal, result, step_price, step_reason, cands, alt_idx
) -> tuple:
    """(lines to print, {key: meaning}) for the unfilled-order menu."""
    filled = float(result.get("filled") or 0)
    lines = [
        "  %s: %s of %d units filled, limit %.5f"
        % (
            proposal["cusip"],
            format(filled, "g"),
            proposal["units"],
            trade.order.lmtPrice,
        )
    ]
    keys = {
        "n": "cancel and resend as a FRESH order, same bill, re-quoted (default; "
        "bill odd lots fill on arrival or not at all) - you confirm first"
    }
    if alt_idx is not None:
        alt = cands.loc[alt_idx]
        keys["s"] = (
            "cancel and switch the unfilled units to %s maturing %s "
            "(ask yield %s, spread %s bp) - you confirm first"
            % (
                alt["cusip"],
                alt["maturity"],
                _fmt_pct(alt.get("ask_yield_pct")),
                _fmt_num(alt.get("spread_bp")),
            )
        )
    if step_price == step_price:
        keys["r"] = (
            "raise the limit on THIS order to %.5f (yield %.3f%%) and wait again "
            "(rarely helps: a resting order is not re-matched)"
            % (step_price, implied_yield_for_row(step_price, proposal["days"]))
        )
    else:
        lines.append("  (cannot raise the limit: %s)" % step_reason)
    keys["w"] = "leave it working until the close"
    keys["c"] = "cancel what is unfilled"
    lines += ["  %s = %s" % (k, v) for k, v in keys.items()]
    return lines, keys


def _fmt_pct(v):
    try:
        v = float(v)
    except BaseException:
        return "n/a"
    return "n/a" if v != v else "%.3f%%" % v


def _fmt_num(v):
    try:
        v = float(v)
    except BaseException:
        return "n/a"
    return "n/a" if v != v else "%.2f" % v


def _next_best_idx(cands: pd.DataFrame, tried: set):
    rest = cands[~cands["cusip"].isin(tried)]
    if len(rest) == 0:
        return None
    return choose_bill_with_reason(rest)[0]


def _replace_order(data, ib, trade, proposal, state, purchase_settings, row):
    """
    Resend (row = the same bill) or switch (row = another bill) as a FRESH
    order: re-quote the bill, show the new order for the units not yet filled
    and ask y/n FIRST; only then cancel the working order and place the new
    one, for whatever is still unfilled once IB confirms the cancel (never more
    than was confirmed). Returns (trade, proposal) for the new order, or None
    if nothing new was placed - in which case the original order is still
    working unless IB had already finished it.
    """
    alt = _requoted(ib, row, purchase_settings)
    limit_price, limit_reason = _limit_for(alt, purchase_settings)
    if limit_price != limit_price:
        return None
    unfilled_units = _unfilled_units(proposal, wait_for_fill(ib, trade, 0))
    if unfilled_units <= 0:
        return None

    new_proposal = _alt_proposal(
        proposal, alt, unfilled_units, limit_price, limit_reason
    )
    contract = _show_proposal_and_preview(ib, new_proposal, state)
    if contract is None:
        print("Original order left working.")
        return None
    if not true_if_answer_is_yes(
        "Cancel order %s and place this instead? y/n: " % trade.order.orderId
    ):
        print("Original order left working.")
        return None

    result = cancel_and_wait(ib, trade)
    data.log.warning(
        "Cancelled T-bill order %s (%s) to replace it with a fresh order for %s: "
        "filled %s"
        % (trade.order.orderId, proposal["cusip"], alt["cusip"], result["filled"])
    )
    if not result["done"]:
        print("IB has not confirmed the cancel yet - not placing a second order.")
        return None
    still_unfilled = min(unfilled_units, _unfilled_units(proposal, result))
    if still_unfilled <= 0:
        print("The original order filled in full while cancelling - nothing to switch.")
        return None
    if still_unfilled < unfilled_units:
        print(
            "%d more units filled while cancelling: buying the remaining %d."
            % (unfilled_units - still_unfilled, still_unfilled)
        )
        new_proposal = _alt_proposal(
            proposal, alt, still_unfilled, limit_price, limit_reason
        )
    return _place(data, ib, contract, new_proposal, state["account_id"]), new_proposal


def _row_for_cusip(cands: pd.DataFrame, cusip: str) -> pd.Series:
    return cands[cands["cusip"] == cusip].iloc[0]


def _requoted(ib, row: pd.Series, purchase_settings) -> pd.Series:
    """The candidate row with a fresh IB bid/ask (keeps the old one if IB has none)."""
    try:
        q = get_quote(
            ib,
            _ib_contract_for(ib, row["conId"]),
            purchase_settings["quote_wait_seconds"],
        )
    except BaseException as e:
        print(
            "Could not re-quote %s (%s): using the earlier quote." % (row["cusip"], e)
        )
        return row
    row = row.copy()
    if q["ask"] == q["ask"]:
        row["ask"] = q["ask"]
    if q["bid"] == q["bid"]:
        row["bid"] = q["bid"]
    return row


def _unfilled_units(proposal: dict, result: dict) -> int:
    return int(proposal["units"] - round(float(result.get("filled") or 0)))


def _alt_proposal(proposal, alt, units, limit_price, limit_reason) -> dict:
    return build_proposal(
        units * BILL_FACE_PER_UNIT,
        alt,
        limit_price,
        limit_reason,
        proposal["target_month"],
        proposal["target_reason"],
    )


def cancel_working_bill_order():
    """
    List bill orders still working at IB and cancel the one picked. IB only
    lets the API client that placed an order cancel it; this tool takes the
    lowest free client id, which is normally the one the ladder tool used,
    so close any other copy of the tool first. Anything else: cancel in TWS.
    """
    with dataBlob(log_name="Interactive-Tbill-Ladder") as data:
        ib = data.ib_conn.ib
        me = ib.client.clientId
        ib.reqOpenOrders()  # binds this client's own orders so they can be cancelled
        ib.sleep(2)
        working = [
            t
            for t in ib.reqAllOpenOrders()
            if t.contract.secType in ("BILL", "BOND") and not t.isDone()
        ]
        if not working:
            print("No bill orders working at IB.")
            return
        print_with_landing_strips_around("BILL ORDERS WORKING AT IB")
        for i, t in enumerate(working):
            print(
                "  %d: order %s %s %s units, conId %s, limit %s, %s, filled %s%s"
                % (
                    i,
                    t.order.orderId,
                    t.order.action,
                    format(t.order.totalQuantity, "g"),
                    t.contract.conId,
                    t.order.lmtPrice,
                    t.orderStatus.status,
                    t.orderStatus.filled,
                    ""
                    if t.order.clientId == me
                    else "  (placed by client %s: cancel in TWS or close that tool)"
                    % t.order.clientId,
                )
            )
        pick = get_input_from_user_and_convert_to_type(
            "Row to cancel? (-1 = none)",
            type_expected=int,
            allow_default=True,
            default_value=-1,
        )
        if pick < 0 or pick >= len(working):
            print("Nothing cancelled.")
            return
        trade = working[pick]
        if trade.order.clientId != me:
            print(
                "Order %s was placed by API client %s and this tool is client %s; "
                "IB only lets the placing client cancel it. Close the other copy "
                "of the tool and re-run, or cancel it in TWS."
                % (trade.order.orderId, trade.order.clientId, me)
            )
            return
        if not true_if_answer_is_yes("Cancel order %s? y/n: " % trade.order.orderId):
            print("Nothing cancelled.")
            return
        result = cancel_and_wait(ib, trade)
        data.log.warning(
            "Cancelled T-bill order %s (conId %s): filled %s of %s"
            % (
                trade.order.orderId,
                trade.contract.conId,
                result["filled"],
                format(trade.order.totalQuantity, "g"),
            )
        )
        print("Final status: %s" % result)


def interactive_tbill_ladder_live():
    """Propose the next T-bill rung, confirm with y/n, then place it."""
    interactive_tbill_ladder(dry_run=False)


def interactive_tbill_ladder_dry_run():
    """Propose and price the next T-bill rung; never places an order."""
    interactive_tbill_ladder(dry_run=True)


if __name__ == "__main__":
    if "--cancel" in sys.argv:
        cancel_working_bill_order()
    else:
        interactive_tbill_ladder(dry_run="--dry-run" in sys.argv)
