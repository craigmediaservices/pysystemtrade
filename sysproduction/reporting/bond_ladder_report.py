from sysdata.data_blob import dataBlob

from syscore.constants import arg_not_supplied
import datetime

import pandas as pd

from sysproduction.reporting.api import reportingApi
from sysproduction.reporting.reporting_functions import table, body_text
from sysproduction.reporting.data.bond_holdings import (
    ladder_buckets_from_bond_df,
    maturing_soon_df,
    get_maturing_soon_days,
    compute_ladder_state,
)


LADDER_PLAN_TEXT = body_text(
    "Target structure: a 'standard' 6-month T-bill ladder - buy one ~6-month "
    "bill each month so that after ~6 months a rung matures every month. "
    "Average duration ~3 months, essentially zero rate risk, captures roughly "
    "the 6-month bill rate. This report is informational only - it never "
    "trades. To act on the ACTION line run "
    "sysproduction/interactive_tbill_ladder.py (propose, confirm, place)."
)


def bond_ladder_report(data: dataBlob = arg_not_supplied):
    """
    Read-only report on Treasury bill/bond holdings: each rung with days to
    maturity and approximate yield, total face, idle base-currency cash, a
    'maturing soon' flag, and the ladder shape by maturity month. Also lists
    short-term Treasury ETF holdings (e.g. VGSH) as near-cash. Trades nothing.
    """
    if data is arg_not_supplied:
        data = dataBlob()

    reporting_api = reportingApi(data)
    maturing_soon_days = get_maturing_soon_days(data)

    state = compute_ladder_state(data)
    bond_df = state["bond_df"]
    etf_df = state["etf_df"]

    formatted_output = []
    formatted_output.append(reporting_api.terse_header("T-bill ladder report"))
    formatted_output.append(LADDER_PLAN_TEXT)

    deployable_block, spare_state = _deployable_cash_block(state)
    formatted_output.extend(deployable_block)

    if len(bond_df) == 0:
        formatted_output.append(
            body_text(
                "No Treasury bills/bonds currently held. Once you place bills they "
                "will be listed here with days-to-maturity and approximate yield, "
                "and the ladder shape below will populate."
            )
        )
    else:
        total_face = bond_df["face"].sum()
        total_mv = bond_df["market_value"].sum()
        formatted_output.append(
            body_text(
                "Total bills/bonds: face %s, market value %s, %d rung(s)."
                % (
                    format(round(total_face), ","),
                    format(round(total_mv), ","),
                    len(bond_df),
                )
            )
        )
        formatted_output.append(
            table("Treasury holdings (sorted by days to maturity)", bond_df)
        )

        soon_df = maturing_soon_df(bond_df, maturing_soon_days)
        if len(soon_df) > 0:
            formatted_output.append(
                body_text(
                    "MATURING SOON (within %d days) - proceeds about to land, plan "
                    "the replacement rung:" % maturing_soon_days
                )
            )
            formatted_output.append(
                table("Maturing within %d days" % maturing_soon_days, soon_df)
            )
        else:
            formatted_output.append(
                body_text("Nothing maturing within %d days." % maturing_soon_days)
            )

        formatted_output.append(
            table(
                "Ladder shape - face by maturity month",
                ladder_buckets_from_bond_df(bond_df),
            )
        )
        gaps = spare_state["gaps"]
        if gaps:
            formatted_output.append(
                body_text(
                    "LADDER GAP(S): no rung maturing in %s (next %d months). "
                    "Point the next purchase at the earliest gap."
                    % (", ".join(gaps), spare_state["ladder_months"])
                )
            )
        else:
            formatted_output.append(
                body_text(
                    "Ladder has no gaps over the next %d months."
                    % spare_state["ladder_months"]
                )
            )

    if len(etf_df) > 0:
        formatted_output.append(
            body_text(
                "Short-term Treasury ETF holdings (near-cash; you may want to rotate "
                "these into the bill ladder over time):"
            )
        )
        formatted_output.append(table("Short-term Treasury ETFs", etf_df))

    formatted_output.append(body_text(spare_state["action"]))
    formatted_output.append(reporting_api.footer())

    return formatted_output


def _deployable_cash_block(state: dict):
    """
    Format the cash / margin / buffer section from the shared ladder state
    (see bond_holdings.compute_ladder_state). Returns (list of report
    elements, state) - the state carries 'action', 'gaps', 'ladder_months'.
    """
    out = []
    base_currency = state["base_currency"]
    bond_df = state["bond_df"]
    settings = state["settings"]

    if not state["balances_ok"]:
        out.append(body_text("Could not read broker cash balances."))
        return out, state

    def _fmt(v):
        return "n/a" if v is None or v != v else format(round(float(v)), ",")

    summary_df = pd.DataFrame(
        dict(
            value=[
                _fmt(state["nlv"]),
                _fmt(state["total_cash"]),
                _fmt(state["base_cash"]),
                _fmt(state["bills_mv"]),
                _fmt(state["etf_mv"]),
                _fmt(state["maint"]),
                _fmt(state["init"]),
                _fmt(state["excess"]),
                _fmt(state["buffer"]),
                _fmt(state["spare"]),
            ]
        ),
        index=[
            "Net liquidation",
            "Total cash (all ccys)",
            "%s cash" % base_currency,
            "Bills market value",
            "Near-cash ETFs",
            "Maintenance margin",
            "Initial margin",
            "Excess liquidity",
            "Cash buffer",
            "Deployable cash",
        ],
    )
    out.append(
        body_text(
            "Deployable cash (account %s). IB credits short Treasuries toward "
            "futures margin, so cash is only needed to settle daily variation "
            "margin: the buffer is sized to P&L swings, not to the margin "
            "requirement. Buffer = %s." % (state["account_id"], state["buffer_text"])
        )
    )
    out.append(table("Cash, margin and buffer (%s)" % base_currency, summary_df))

    check = state["check"]
    if check["usable"]:
        if check["fully_counted"]:
            out.append(
                body_text(
                    "Collateral check OK: excess liquidity = net liquidation - "
                    "maintenance margin (bills fully counted; implied haircut %s)."
                    % _fmt(check["haircut"])
                )
            )
        else:
            out.append(
                body_text(
                    "COLLATERAL WARNING: excess liquidity is %s below net "
                    "liquidation - maintenance margin. IB may be haircutting the "
                    "bills; the deployable-cash logic assumes it is not."
                    % _fmt(check["haircut"])
                )
            )
    else:
        out.append(
            body_text("Could not read margin tags from IB - collateral check skipped.")
        )

    if state["negative_ccys"]:
        out.append(
            body_text(
                "NEGATIVE non-%s balance(s): %s. IB debit interest exceeds bill "
                "yield - see FX balance report / interactive_fx_sweep."
                % (base_currency, ", ".join(state["negative_ccys"]))
            )
        )

    rung = state["rung"]
    if rung == rung and len(bond_df):
        out.append(
            body_text(
                "Even-ladder rung size: (cash %s + bills %s + ETFs %s - buffer %s) / %d "
                "= ~%s per rung (current rungs average %s). Grow undersized rungs as "
                "they roll rather than in one lump."
                % (
                    _fmt(state["base_cash"]),
                    _fmt(state["bills_mv"]),
                    _fmt(state["etf_mv"]),
                    _fmt(state["buffer"]),
                    settings["ladder_months"],
                    _fmt(rung),
                    _fmt(state["bills_mv"] / len(bond_df)),
                )
            )
        )

    return out, state


if __name__ == "__main__":
    bond_ladder_report()
