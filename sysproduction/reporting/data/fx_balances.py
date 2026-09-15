"""
Helpers for the FX balance report and the interactive FX sweep tool.

Pure-ish data functions live here so the sweep arithmetic can be unit tested
without a broker connection.
"""

import numpy as np
import pandas as pd

from sysdata.data_blob import dataBlob
from sysproduction.data.broker import dataBroker
from sysproduction.data.instruments import diagInstruments
from sysproduction.data.positions import diagPositions
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


def get_fx_balance_buffers(data: dataBlob) -> dict:
    """
    Per-currency margin buffer in base-currency units. A long balance worth up
    to its buffer is considered intentional (held to cover variation margin on
    that-currency futures) and won't be flagged for sweeping. A sweep is only
    suggested for the *excess* over the buffer.

    Reads ``fx_balance_buffers`` from config as a {ccy: base_value} dict; e.g.
    ``fx_balance_buffers: {EUR: 15000, GBP: 5000}``. Missing/unknown ccys
    default to 0. Negative balances are not buffer-adjusted (you want to flatten
    them - IB charges debit interest on negatives).
    """
    try:
        raw = data.config.get_element("fx_balance_buffers") or {}
    except BaseException:
        raw = {}
    out = {}
    for ccy, buf in raw.items():
        try:
            out[str(ccy).upper()] = float(buf)
        except BaseException:
            continue
    return out


def positions_by_currency_from_df(positions_df: pd.DataFrame, currency_lookup) -> dict:
    """
    Pure helper (unit-tested): collapse a contract-positions frame with columns
    instrument_code / position into
        {currency: dict(contracts=<sum of abs positions>,
                        positions="BUND -3, OAT +2")}
    currency_lookup(instrument_code) -> currency; instruments it raises on are
    skipped. Instruments whose contracts net to zero are ignored.
    """
    out = {}
    if positions_df is None or len(positions_df) == 0:
        return out
    per_instrument = positions_df.groupby("instrument_code")["position"].sum()
    for instrument_code, position in per_instrument.items():
        position = int(round(position))
        if position == 0:
            continue
        try:
            currency = currency_lookup(instrument_code)
        except BaseException:
            continue
        entry = out.setdefault(currency, dict(contracts=0, positions=[]))
        entry["contracts"] += abs(position)
        entry["positions"].append("%s %+d" % (instrument_code, position))
    for entry in out.values():
        entry["positions"] = ", ".join(sorted(entry["positions"]))
    return out


def get_futures_positions_by_currency(data: dataBlob) -> dict:
    """
    Live futures positions grouped by the currency the instrument is
    denominated in, so a cash balance can be read next to what it is backing.
    Never raises: a DB/config problem just gives an empty dict.
    """
    try:
        positions_df = (
            diagPositions(data).get_all_current_contract_positions().as_pd_df()
        )
        get_currency = diagInstruments(data).get_currency
        return positions_by_currency_from_df(positions_df, get_currency)
    except BaseException:
        return {}


def add_positions_to_balances_df(
    balances_df: pd.DataFrame, positions_by_currency: dict
) -> pd.DataFrame:
    """Add 'contracts' (count) and 'positions' (text) columns, indexed by currency."""
    balances_df = balances_df.copy()
    balances_df["contracts"] = [
        int(positions_by_currency.get(ccy, {}).get("contracts", 0))
        for ccy in balances_df.index
    ]
    balances_df["positions"] = [
        positions_by_currency.get(ccy, {}).get("positions", "")
        for ccy in balances_df.index
    ]
    return balances_df


def get_fx_balances_as_df(data: dataBlob) -> pd.DataFrame:
    """
    DataFrame indexed by currency with columns: balance, fx_rate_to_base,
    base_value, contracts (number of futures contracts held in instruments
    denominated in that currency) and positions (text, e.g. "BUND -3, OAT +2").
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
        df = add_positions_to_balances_df(df, get_futures_positions_by_currency(data))

    return df


def get_fx_sweep_suggestions(
    balances_df: pd.DataFrame,
    base_currency: str,
    threshold: float,
    buffers: dict = None,
) -> pd.DataFrame:
    """
    Work out which non-base balances are large enough to sweep back to base,
    respecting a per-currency margin buffer.

    For a positive (long) foreign balance with base_value V and buffer B (both
    in base ccy), the *excess* is V - B; we only flag it when excess > threshold,
    and the suggested trade size is just the excess (so the buffer stays
    intact). For a negative balance the buffer is ignored - we want to flatten
    it because IB charges debit interest on negatives.

    Trade convention matches data_broker.broker_fx_market_order: a negative qty
    sells ccy1 and buys the base currency (ccy2).

    Returns DataFrame indexed by currency with columns:
      balance, base_value, buffer_base, excess_base, action, pair,
      approx_trade_qty, contracts, positions (the last two are copied from
      balances_df when present, so you can see what the cash is backing).
    """
    buffers = buffers or {}
    rows = []
    for currency, row in balances_df.iterrows():
        if currency == base_currency:
            continue
        base_value = row["base_value"]
        if pd.isna(base_value):
            continue
        balance = row["balance"]
        fx_rate = row.get("fx_rate_to_base", float("nan"))

        if base_value >= 0:
            buffer_base = float(buffers.get(currency, 0.0))
            excess_base = base_value - buffer_base
            if excess_base <= threshold:
                continue
            # excess in ccy units
            if pd.isna(fx_rate) or fx_rate == 0:
                continue
            excess_ccy = excess_base / fx_rate
            trade_qty = -excess_ccy
        else:
            # short balance: ignore buffer, flag if abs over threshold,
            # buy back the full amount to flatten
            buffer_base = 0.0
            excess_base = abs(base_value)
            if excess_base <= threshold:
                continue
            trade_qty = -balance  # positive => BUY

        rows.append(
            dict(
                currency=currency,
                balance=balance,
                base_value=base_value,
                buffer_base=buffer_base,
                excess_base=excess_base,
                action="SELL" if trade_qty < 0 else "BUY",
                pair="%s%s" % (currency, base_currency),
                approx_trade_qty=int(round(trade_qty)),
                contracts=int(row.get("contracts", 0)),
                positions=row.get("positions", ""),
            )
        )

    cols = [
        "currency",
        "balance",
        "base_value",
        "buffer_base",
        "excess_base",
        "action",
        "pair",
        "approx_trade_qty",
        "contracts",
        "positions",
    ]
    if rows:
        return pd.DataFrame(rows, columns=cols).set_index("currency")
    return pd.DataFrame(columns=cols[1:])
