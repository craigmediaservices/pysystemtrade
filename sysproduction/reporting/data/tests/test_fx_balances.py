import numpy as np
import pandas as pd

from sysproduction.reporting.data import fx_balances
from sysproduction.reporting.data.fx_balances import (
    get_fx_sweep_suggestions,
    get_fx_balance_alert_threshold,
    get_fx_balance_buffers,
    get_fx_balances_as_df,
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


def test_buffer_suppresses_alert_when_balance_within_buffer():
    # EUR 14k base value, 15k buffer -> excess -1k, below threshold, no alert
    df = _balances_df([("EUR", 12278.5, 1.1756, 14434.7)])
    out = get_fx_sweep_suggestions(
        df, base_currency="USD", threshold=10000.0, buffers={"EUR": 15000.0}
    )
    assert len(out) == 0


def test_buffer_only_excess_is_swept():
    # EUR 30k base value, 15k buffer -> excess 15k. Sweep the excess only.
    df = _balances_df([("EUR", 25517.0, 1.1756, 30000.0)])
    out = get_fx_sweep_suggestions(
        df, base_currency="USD", threshold=10000.0, buffers={"EUR": 15000.0}
    )
    assert list(out.index) == ["EUR"]
    row = out.loc["EUR"]
    assert row["buffer_base"] == 15000.0
    assert abs(row["excess_base"] - 15000.0) < 1e-6
    # excess_ccy = 15000 / 1.1756 ~= 12759 ; trade_qty = -12759
    assert abs(row["approx_trade_qty"] - (-12759)) <= 1


def test_buffer_ignored_for_negative_balance():
    # short balance is flagged even if you've set a buffer for that ccy
    df = _balances_df([("EUR", -15000.0, 1.1, -16500.0)])
    out = get_fx_sweep_suggestions(
        df, base_currency="USD", threshold=10000.0, buffers={"EUR": 20000.0}
    )
    assert list(out.index) == ["EUR"]
    row = out.loc["EUR"]
    assert row["action"] == "BUY"
    assert row["approx_trade_qty"] == 15000


def test_get_fx_balance_buffers_default_empty():
    assert get_fx_balance_buffers(_FakeData()) == {}


def test_get_fx_balance_buffers_from_config():
    data = _FakeData({"eur": 15000, "GBP": "5000", "bad": "not a number"})
    out = get_fx_balance_buffers(data)
    assert out == {"EUR": 15000.0, "GBP": 5000.0}


def test_get_fx_balances_as_df_drops_pseudo_currencies(monkeypatch):
    # IB returns a "BASE" pseudo-currency holding the account total - drop it
    class _FakeBroker:
        def __init__(self, data):
            pass

        def broker_fx_balances(self):
            return {"USD": "1000", "EUR": "500", "BASE": "1588", "": "0"}

    class _FakeCurrency:
        def __init__(self, data):
            pass

        def get_base_currency(self):
            return "USD"

        def get_last_fx_rate_to_base(self, currency):
            return {"EUR": 1.1}[currency]

    monkeypatch.setattr(fx_balances, "dataBroker", _FakeBroker)
    monkeypatch.setattr(fx_balances, "dataCurrency", _FakeCurrency)

    df = get_fx_balances_as_df(data=None)
    assert "BASE" not in df.index
    assert "" not in df.index
    assert set(df.index) == {"USD", "EUR"}
    assert df.loc["EUR", "base_value"] == 550.0
