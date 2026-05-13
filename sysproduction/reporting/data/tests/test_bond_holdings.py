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
