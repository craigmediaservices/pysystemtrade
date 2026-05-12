"""
Helpers for the FX balance report and the interactive FX sweep tool.

Pure-ish data functions live here so the sweep arithmetic can be unit tested
without a broker connection.
"""

import numpy as np
import pandas as pd

from sysdata.data_blob import dataBlob
from sysproduction.data.broker import dataBroker
from sysproduction.data.currency_data import dataCurrency

DEFAULT_FX_BALANCE_ALERT_THRESHOLD = 10000.0

# IB reports a pseudo-currency "BASE" in TotalCashBalance holding the account
# total in base currency - it isn't a real currency balance, so drop it.
_PSEUDO_CURRENCIES = ("BASE", "")


def get_fx_balance_alert_threshold(data: dataBlob) -> float:
    """
    Threshold (in base currency units) above which a non-base balance is flagged.

    Reads ``fx_balance_alert_threshold`` from config (private_config.yaml),
    falling back to DEFAULT_FX_BALANCE_ALERT_THRESHOLD.
    """
    try:
        threshold = float(data.config.get_element("fx_balance_alert_threshold"))
    except BaseException:
        threshold = DEFAULT_FX_BALANCE_ALERT_THRESHOLD

    return threshold


def get_fx_balances_as_df(data: dataBlob) -> pd.DataFrame:
    """
    DataFrame indexed by currency with columns: balance, fx_rate_to_base, base_value.
    Sorted by absolute base value, largest first.
    """
    data_broker = dataBroker(data)
    currency_data = dataCurrency(data)
    base_currency = currency_data.get_base_currency()

    raw_balances = data_broker.broker_fx_balances()

    rows = []
    for currency, value in raw_balances.items():
        if currency in _PSEUDO_CURRENCIES:
            continue
        value = float(value)
        if currency == base_currency:
            fx_rate = 1.0
        else:
            try:
                fx_rate = float(currency_data.get_last_fx_rate_to_base(currency))
            except BaseException:
                fx_rate = np.nan
        rows.append(
            dict(
                currency=currency,
                balance=value,
                fx_rate_to_base=fx_rate,
                base_value=value * fx_rate,
            )
        )

    df = pd.DataFrame(
        rows, columns=["currency", "balance", "fx_rate_to_base", "base_value"]
    )
    if len(df) > 0:
        df = df.set_index("currency")
        df = df.reindex(df["base_value"].abs().sort_values(ascending=False).index)

    return df


def get_fx_sweep_suggestions(
    balances_df: pd.DataFrame, base_currency: str, threshold: float
) -> pd.DataFrame:
    """
    From a balances DataFrame (see get_fx_balances_as_df) work out which
    non-base balances exceed `threshold` (in base ccy) and the trade that would
    flatten each one.

    Trade convention matches data_broker.broker_fx_market_order / the
    interactive_order_stack FX trade: a negative qty sells ccy1 and buys ccy2
    (the base currency). So to flatten a positive foreign balance we propose a
    negative trade.

    Returns columns: balance, base_value, action, pair, approx_trade_qty.
    """
    rows = []
    for currency, row in balances_df.iterrows():
        if currency == base_currency:
            continue
        base_value = row["base_value"]
        if pd.isna(base_value) or abs(base_value) <= threshold:
            continue
        balance = row["balance"]
        trade_qty = -balance
        rows.append(
            dict(
                currency=currency,
                balance=balance,
                base_value=base_value,
                action="SELL" if trade_qty < 0 else "BUY",
                pair="%s%s" % (currency, base_currency),
                approx_trade_qty=int(round(trade_qty)),
            )
        )

    return (
        pd.DataFrame(
            rows,
            columns=[
                "currency",
                "balance",
                "base_value",
                "action",
                "pair",
                "approx_trade_qty",
            ],
        ).set_index("currency")
        if rows
        else pd.DataFrame(
            columns=["balance", "base_value", "action", "pair", "approx_trade_qty"]
        )
    )
