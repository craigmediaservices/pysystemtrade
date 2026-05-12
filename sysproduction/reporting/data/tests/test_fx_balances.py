import numpy as np
import pandas as pd

from sysproduction.reporting.data.fx_balances import (
    get_fx_sweep_suggestions,
    get_fx_balance_alert_threshold,
    DEFAULT_FX_BALANCE_ALERT_THRESHOLD,
)


def _balances_df(rows):
    # rows: list of (currency, balance, fx_rate_to_base, base_value)
    df = pd.DataFrame(
        rows, columns=["currency", "balance", "fx_rate_to_base", "base_value"]
    ).set_index("currency")
    return df


class _FakeConfig:
    def __init__(self, value=None):
        self._value = value

    def get_element(self, key):
        if self._value is None:
            raise Exception("missing %s" % key)
        return self._value


class _FakeData:
    def __init__(self, config_value=None):
        self.config = _FakeConfig(config_value)


def test_threshold_default_when_absent():
    assert (
        get_fx_balance_alert_threshold(_FakeData())
        == DEFAULT_FX_BALANCE_ALERT_THRESHOLD
    )


def test_threshold_from_config():
    assert get_fx_balance_alert_threshold(_FakeData(25000)) == 25000.0
    assert get_fx_balance_alert_threshold(_FakeData("5000")) == 5000.0


def test_no_suggestions_when_under_threshold():
    df = _balances_df(
        [
            ("USD", 50000.0, 1.0, 50000.0),
            ("EUR", 5000.0, 1.1, 5500.0),
        ]
    )
    out = get_fx_sweep_suggestions(df, base_currency="USD", threshold=10000.0)
    assert len(out) == 0


def test_base_currency_never_suggested():
    df = _balances_df([("USD", 999999.0, 1.0, 999999.0)])
    out = get_fx_sweep_suggestions(df, base_currency="USD", threshold=10000.0)
    assert len(out) == 0


def test_positive_balance_gives_sell():
    df = _balances_df([("EUR", 20000.0, 1.1, 22000.0)])
    out = get_fx_sweep_suggestions(df, base_currency="USD", threshold=10000.0)
    assert list(out.index) == ["EUR"]
    row = out.loc["EUR"]
    assert row["action"] == "SELL"
    assert row["pair"] == "EURUSD"
    assert row["approx_trade_qty"] == -20000


def test_negative_balance_gives_buy():
    df = _balances_df([("GBP", -15000.0, 1.25, -18750.0)])
    out = get_fx_sweep_suggestions(df, base_currency="USD", threshold=10000.0)
    row = out.loc["GBP"]
    assert row["action"] == "BUY"
    assert row["approx_trade_qty"] == 15000


def test_nan_base_value_skipped():
    df = _balances_df([("XYZ", 100000.0, np.nan, np.nan)])
    out = get_fx_sweep_suggestions(df, base_currency="USD", threshold=10000.0)
    assert len(out) == 0
