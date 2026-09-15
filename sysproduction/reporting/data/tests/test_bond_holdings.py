import datetime

import numpy as np
import pandas as pd

from sysproduction.reporting.data.bond_holdings import (
    parse_ib_date,
    days_to_maturity,
    is_zero_coupon,
    approx_bill_yield_pct,
    ladder_buckets,
    ladder_buckets_from_bond_df,
    maturing_soon_df,
    get_maturing_soon_days,
    DEFAULT_MATURING_SOON_DAYS,
)


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


def test_parse_ib_date():
    assert parse_ib_date("20260815") == datetime.date(2026, 8, 15)
    assert parse_ib_date("20260815 14:30:00") == datetime.date(2026, 8, 15)
    assert parse_ib_date("") is None
    assert parse_ib_date(None) is None
    assert parse_ib_date("garbage") is None


def test_days_to_maturity():
    asof = datetime.date(2026, 1, 1)
    assert days_to_maturity(datetime.date(2026, 1, 31), asof) == 30
    assert np.isnan(days_to_maturity(None, asof))


def test_is_zero_coupon():
    assert is_zero_coupon(0.0)
    assert is_zero_coupon(0)
    assert is_zero_coupon(None)
    assert is_zero_coupon(np.nan)
    assert not is_zero_coupon(4.25)


def test_approx_bill_yield_pct():
    # 182-day bill at 98.20 -> ~3.7% bond-equivalent
    y = approx_bill_yield_pct(98.20, 182)
    assert 3.5 < y < 4.0
    # at par or zero days -> NaN
    assert np.isnan(approx_bill_yield_pct(0.0, 182))
    assert np.isnan(approx_bill_yield_pct(98.2, 0))
    assert np.isnan(approx_bill_yield_pct(None, 100))


def test_ladder_buckets():
    pairs = [
        (datetime.date(2026, 6, 15), 100000.0),
        (datetime.date(2026, 6, 28), 50000.0),
        (datetime.date(2026, 7, 10), 100000.0),
        (None, 25000.0),
    ]
    out = ladder_buckets(pairs)
    assert list(out.index) == ["2026-06", "2026-07", "unknown"]
    assert out.loc["2026-06", "face"] == 150000.0
    assert out.loc["2026-07", "face"] == 100000.0
    assert out.loc["unknown", "face"] == 25000.0


def test_ladder_buckets_empty():
    assert len(ladder_buckets([])) == 0


def test_ladder_buckets_from_bond_df():
    bond_df = pd.DataFrame(
        {
            "maturity": ["2026-06-15", "2026-07-10", ""],
            "face": [100000.0, 100000.0, 50000.0],
        }
    )
    out = ladder_buckets_from_bond_df(bond_df)
    assert out.loc["2026-06", "face"] == 100000.0
    assert out.loc["unknown", "face"] == 50000.0


def test_maturing_soon_df():
    bond_df = pd.DataFrame(
        {"days_to_maturity": [10, 40, np.nan, 35], "face": [1, 2, 3, 4]},
        index=["a", "b", "c", "d"],
    )
    out = maturing_soon_df(bond_df, 35)
    assert list(out.index) == ["a", "d"]
    # empty in, empty out
    assert len(maturing_soon_df(pd.DataFrame(), 35)) == 0


def test_get_maturing_soon_days():
    assert get_maturing_soon_days(_FakeData()) == DEFAULT_MATURING_SOON_DAYS
    assert get_maturing_soon_days(_FakeData(28)) == 28
    assert get_maturing_soon_days(_FakeData("21")) == 21


# ---------------------------------------------------------------------------
# Deployable-cash / suggestion logic
# ---------------------------------------------------------------------------

from sysproduction.reporting.data.bond_holdings import (  # noqa: E402
    daily_pnl_sd_from_capital_series,
    derived_cash_buffer,
    cash_buffer_with_explanation,
    deployable_cash,
    cash_available_for_bills,
    collateral_check,
    ladder_gaps,
    maturity_dates_from_bond_df,
    round_down_to,
    target_rung_size,
    suggested_action,
    get_cash_buffer_settings,
    DEFAULT_CASH_BUFFER_FLOOR,
    DEFAULT_CASH_BUFFER_SIGMA,
)


class _DictConfig:
    def __init__(self, values):
        self._values = values

    def get_element(self, key):
        if key not in self._values:
            raise Exception("missing %s" % key)
        return self._values[key]


class _DictData:
    def __init__(self, values):
        self.config = _DictConfig(values)


def test_cash_buffer_settings_defaults_and_overrides():
    s = get_cash_buffer_settings(_DictData({}))
    assert s["fixed"] is None
    assert s["sigma"] == DEFAULT_CASH_BUFFER_SIGMA
    assert s["floor"] == DEFAULT_CASH_BUFFER_FLOOR

    s = get_cash_buffer_settings(
        _DictData({"tbill_cash_buffer": "250000", "tbill_ladder_months": 9})
    )
    assert s["fixed"] == 250000.0
    assert s["ladder_months"] == 9


def test_daily_pnl_sd_from_capital_series():
    idx = pd.bdate_range("2026-01-01", periods=60)
    # deterministic +/- 1000 alternating -> sd ~ 1000
    vals = 1e6 + np.cumsum(np.where(np.arange(60) % 2 == 0, 1000.0, -1000.0))
    s = pd.Series(vals, index=idx)
    sd = daily_pnl_sd_from_capital_series(s, 120)
    assert 900 < sd < 2100
    # too short -> NaN
    assert np.isnan(daily_pnl_sd_from_capital_series(s[:5], 120))
    assert np.isnan(daily_pnl_sd_from_capital_series(pd.Series(dtype=float), 120))
    # intraday duplicates collapse to one business-day point
    s2 = pd.concat([s, s.shift(1, freq="h")]).sort_index()
    assert abs(daily_pnl_sd_from_capital_series(s2, 120) - sd) < 1e-6


def test_derived_cash_buffer():
    # 3 sigma x 15,000 x sqrt(10) ~ 142,302 > floor
    b = derived_cash_buffer(15000.0, 3.0, 10, 100000.0)
    assert abs(b - 3 * 15000 * np.sqrt(10)) < 1.0
    # floor dominates small sd
    assert derived_cash_buffer(1000.0, 3.0, 10, 100000.0) == 100000.0
    # unusable sd -> floor
    assert derived_cash_buffer(np.nan, 3.0, 10, 100000.0) == 100000.0
    assert derived_cash_buffer(None, 3.0, 10, 100000.0) == 100000.0


def test_cash_buffer_with_explanation():
    fixed = dict(fixed=200000.0, sigma=3.0, days=10, floor=100000.0)
    b, text = cash_buffer_with_explanation(fixed, 15000.0)
    assert b == 200000.0 and "fixed" in text
    derived = dict(fixed=None, sigma=3.0, days=10, floor=100000.0)
    b, text = cash_buffer_with_explanation(derived, 15000.0)
    assert b > 100000.0 and "sigma" in text
    b, text = cash_buffer_with_explanation(derived, np.nan)
    assert b == 100000.0 and "no usable" in text


def test_deployable_cash_and_rounding():
    assert deployable_cash(492000.0, 220000.0) == 272000.0
    assert round_down_to(272345.0, 10000.0) == 270000.0
    assert round_down_to(5000.0, 10000.0) == 0.0
    assert round_down_to(5000.0, 0) == 5000.0


def test_collateral_check():
    # bills fully counted: excess == nlv - maint
    c = collateral_check(1126344.0, 495536.0, 630808.0)
    assert c["usable"] and c["fully_counted"]
    # 100k unexplained haircut on a 1.1M account -> not fully counted
    c = collateral_check(1126344.0, 495536.0, 530808.0)
    assert c["usable"] and not c["fully_counted"]
    assert abs(c["haircut"] - 100000.0) < 1.0
    # bad inputs
    assert not collateral_check(np.nan, 1.0, 1.0)["usable"]
    assert not collateral_check(None, 1.0, 1.0)["usable"]


def test_ladder_gaps():
    asof = datetime.date(2026, 9, 3)
    held = [
        datetime.date(2026, 10, 29),
        datetime.date(2026, 11, 27),
        datetime.date(2026, 12, 24),
        datetime.date(2027, 1, 21),
        datetime.date(2027, 2, 18),
        datetime.date(2027, 4, 15),
        None,
    ]
    # window is Oct-2026 .. Mar-2027: only March is missing
    assert ladder_gaps(held, asof, 6) == ["2027-03"]
    # 7-month window: March missing, April held
    assert ladder_gaps(held, asof, 7) == ["2027-03"]
    # empty ladder -> every month is a gap, year rollover handled
    assert ladder_gaps([], asof, 6) == [
        "2026-10",
        "2026-11",
        "2026-12",
        "2027-01",
        "2027-02",
        "2027-03",
    ]


def test_maturity_dates_from_bond_df():
    df = pd.DataFrame(dict(maturity=["2026-10-29", ""], face=[100.0, 50.0]))
    dates = maturity_dates_from_bond_df(df)
    assert dates == [datetime.date(2026, 10, 29), None]
    assert maturity_dates_from_bond_df(pd.DataFrame()) == []


def test_target_rung_size():
    assert abs(target_rung_size(1_084_000.0, 220000.0, 6) - 144000.0) < 1.0
    assert target_rung_size(100.0, 500.0, 6) == 0.0
    assert np.isnan(target_rung_size(100.0, 0.0, 0))


def test_suggested_action_buy_fills_gap():
    asof = datetime.date(2026, 9, 3)
    text = suggested_action(492311.0, 220000.0, ["2027-03"], asof, 6, 10000.0)
    assert text.startswith("ACTION: buy ~270,000")
    assert "2027-03" in text and "gap" in text


def test_suggested_action_buy_extends_when_no_gap():
    asof = datetime.date(2026, 9, 3)
    text = suggested_action(492311.0, 220000.0, [], asof, 6, 10000.0)
    assert text.startswith("ACTION: buy ~270,000")
    assert "2027-03" in text and "extends" in text


def test_suggested_action_none_cases():
    asof = datetime.date(2026, 9, 3)
    # below buffer -> let maturity land
    text = suggested_action(150000.0, 220000.0, [], asof, 6, 10000.0)
    assert text.startswith("ACTION: none") and "below the buffer" in text
    # above buffer but under rounding
    text = suggested_action(225000.0, 220000.0, [], asof, 6, 10000.0)
    assert text.startswith("ACTION: none") and "rounding" in text
    # negative cash -> explicit warning, overrides everything
    text = suggested_action(-5000.0, 220000.0, ["2027-03"], asof, 6, 10000.0)
    assert "NEGATIVE" in text
    # collateral not fully counted -> no buy suggestion
    text = suggested_action(
        492311.0, 220000.0, [], asof, 6, 10000.0, collateral_ok=False
    )
    assert text.startswith("ACTION: none") and "credited" in text


def test_suggested_action_extends_past_last_rung():
    from sysproduction.reporting.data.bond_holdings import next_rung_month

    asof = datetime.date(2026, 9, 8)
    held = [datetime.date(2027, 3, 4), datetime.date(2027, 4, 15)]
    assert next_rung_month(held, asof, 6, gaps=[])[0] == "2027-05"
    assert next_rung_month(held, asof, 6, gaps=["2026-11"])[0] == "2026-11"
    assert next_rung_month([], asof, 6, gaps=[])[0] == "2027-03"
    text = suggested_action(
        492311.0, 220000.0, [], asof, 6, 10000.0, maturity_dates=held
    )
    assert "2027-05" in text and "extends" in text


def test_cash_available_for_bills_nets_negative_balances():
    # +USD / -JPY after a delivered yen future: total cash is the binding figure
    assert cash_available_for_bills(304019.0, 221581.0) == 221581.0
    # no negative balances: total >= base, base cash is used
    assert cash_available_for_bills(277398.0, 288933.0) == 277398.0
    # unusable total -> base cash
    assert cash_available_for_bills(100.0, np.nan) == 100.0
    assert cash_available_for_bills(100.0, None) == 100.0
    # netted figure below buffer -> no buy suggested
    asof = datetime.date(2026, 9, 15)
    text = suggested_action(
        cash_available_for_bills(304019.0, 221581.0), 198319.0, [], asof, 6, 10000.0
    )
    assert text.startswith("ACTION: buy ~20,000") or text.startswith("ACTION: none")
