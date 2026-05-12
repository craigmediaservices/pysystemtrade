from sysdata.data_blob import dataBlob

from syscore.constants import arg_not_supplied
from sysproduction.data.broker import dataBroker
from sysproduction.data.currency_data import dataCurrency
from sysproduction.reporting.api import reportingApi
from sysproduction.reporting.reporting_functions import table, body_text
from sysproduction.reporting.data.bond_holdings import (
    get_bond_holdings_df,
    get_near_cash_etf_df,
    ladder_buckets_from_bond_df,
    maturing_soon_df,
    get_maturing_soon_days,
)


LADDER_PLAN_TEXT = body_text(
    "Target structure: a 'standard' 6-month T-bill ladder - buy one ~6-month "
    "bill each month so that after ~6 months a rung matures every month. "
    "Average duration ~3 months, essentially zero rate risk, captures roughly "
    "the 6-month bill rate. This report is informational only - it never "
    "trades; placing / rolling bills stays a manual monthly task."
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
    currency_data = dataCurrency(data)
    base_currency = currency_data.get_base_currency()
    maturing_soon_days = get_maturing_soon_days(data)

    bond_df = get_bond_holdings_df(data)
    etf_df = get_near_cash_etf_df(data)

    formatted_output = []
    formatted_output.append(reporting_api.terse_header("T-bill ladder report"))
    formatted_output.append(LADDER_PLAN_TEXT)

    try:
        balances = dataBroker(data).broker_fx_balances()
        base_cash = float(balances.get(base_currency, 0.0))
        formatted_output.append(
            body_text(
                "Cash balance (%s): %s. Idle %s cash above your futures-margin "
                "buffer is what funds the next ladder rung (target ~one rung / "
                "month)."
                % (base_currency, format(round(base_cash), ","), base_currency)
            )
        )
    except BaseException:
        pass

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

    if len(etf_df) > 0:
        formatted_output.append(
            body_text(
                "Short-term Treasury ETF holdings (near-cash; you may want to rotate "
                "these into the bill ladder over time):"
            )
        )
        formatted_output.append(table("Short-term Treasury ETFs", etf_df))

    formatted_output.append(reporting_api.footer())

    return formatted_output


if __name__ == "__main__":
    bond_ladder_report()
