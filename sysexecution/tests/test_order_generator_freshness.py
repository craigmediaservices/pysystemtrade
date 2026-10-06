"""
Order generator freshness guard (2026-10-06): the generator runs on its own
timer and must not trade on raw optimal positions that run_systems is still
writing (a mix of runs), or on none / very old ones. A complete set from the
previous run is still traded on, with a WARNING: skipping would leave ~16h
without trading.
"""
import datetime
from types import SimpleNamespace
from unittest import mock

import pandas as pd

from sysexecution.strategies import dynamic_optimised_positions as dop
from sysobjects.production.optimal_positions import optimalPositionWithReference

NOW = datetime.datetime(2026, 10, 6, 11, 30)
CLS = dop.orderGeneratorForDynamicPositions


def _entry(hours_old, now=NOW):
    return optimalPositionWithReference(
        date=now - datetime.timedelta(hours=hours_old),
        optimal_position=1.0,
        reference_price=100.0,
        reference_contract="20261200",
        reference_date=now - datetime.timedelta(days=1),
    )


# the 10:05 run has written everything (within a minute)
FRESH = {"SP500": _entry(1.20), "GOLD": _entry(1.19), "US10": _entry(1.195)}
# the 10:05 run is half-way through its writes: some from it, some from 02:05
MIXED = {"SP500": _entry(0.2), "GOLD": _entry(8.6), "US10": _entry(8.6)}
# the 10:05 run has not written yet: a complete, uniform set from 02:05
PREVIOUS_RUN = {"SP500": _entry(8.6), "GOLD": _entry(8.59), "US10": _entry(8.6)}
# nothing written for more than a day
ANCIENT = {"SP500": _entry(26.0), "GOLD": _entry(26.0), "US10": _entry(26.0)}


def _relative_to_wall_clock(raw):
    # the generator checks against datetime.now(); rebuild the same ages
    now = datetime.datetime.now()
    return {
        code: _entry((NOW - entry.date).total_seconds() / 3600.0, now=now)
        for code, entry in raw.items()
    }


def _generate(raw):
    raw = _relative_to_wall_clock(raw)
    gen = object.__new__(CLS)
    gen._strategy_name = "dynamic_system"
    gen._data = SimpleNamespace(
        config=SimpleNamespace(get_element_or_default=lambda name, default: default)
    )
    gen._log = mock.Mock()
    with mock.patch.object(
        CLS, "get_raw_optimal_position_data", return_value=raw
    ), mock.patch.object(
        CLS, "calculate_write_and_return_optimised_positions_data", return_value={}
    ) as calc, mock.patch.object(
        CLS, "get_actual_positions_for_strategy", return_value={}
    ), mock.patch.object(
        dop, "list_of_trades_given_optimised_and_actual_positions", return_value=[]
    ) as trades:
        result = gen.get_required_orders()
    return gen, calc, trades, result


def _assert_blocked(gen, calc, trades, result):
    calc.assert_not_called()  # optimised positions are not even written
    trades.assert_not_called()
    assert len(result) == 0
    gen._log.critical.assert_called_once()
    assert "NOT generating orders" in gen._log.critical.call_args.args[0]


def _assert_traded(gen, calc, trades):
    calc.assert_called_once()
    trades.assert_called_once()
    gen._log.critical.assert_not_called()


# --- the generator itself ----------------------------------------------------


def test_mixed_set_mid_write_generates_no_orders():
    _assert_blocked(*_generate(MIXED))


def test_no_positions_at_all_generates_no_orders():
    _assert_blocked(*_generate({}))


def test_positions_over_20_hours_old_generate_no_orders():
    _assert_blocked(*_generate(ANCIENT))


def test_complete_previous_run_is_traded_on_with_a_warning():
    gen, calc, trades, _ = _generate(PREVIOUS_RUN)
    _assert_traded(gen, calc, trades)
    gen._log.warning.assert_called_once()
    assert "hours old" in gen._log.warning.call_args.args[0]


def test_fresh_positions_are_traded_on_without_a_second_read():
    gen, calc, trades, _ = _generate(FRESH)
    _assert_traded(gen, calc, trades)
    gen._log.warning.assert_not_called()
    assert set(calc.call_args.kwargs["raw_optimal_position_data"]) == set(FRESH)


# --- the pure check -------------------------------------------------------------


def _check(raw, **kwargs):
    return dop.check_raw_optimal_positions(raw, NOW, **kwargs)


def test_fresh_is_clean():
    assert _check(FRESH) == ("", "")


def test_mixed_names_the_instruments_behind():
    block, _ = _check(MIXED)
    assert "mix of runs" in block and "GOLD" in block and "US10" in block
    assert "SP500" not in block.split("e.g.")[1]


def test_previous_run_warns_but_does_not_block():
    block, warning = _check(PREVIOUS_RUN)
    assert block == "" and "8.6 hours old" in warning


def test_ancient_blocks():
    assert "has not completed" in _check(ANCIENT)[0]


def test_empty_blocks():
    assert _check({})[0]


def test_undated_entry_blocks():
    block, _ = _check(dict(FRESH, ODD=SimpleNamespace()))
    assert "ODD" in block


def test_spread_just_inside_the_limit_is_one_run():
    raw = {"SP500": _entry(1.0), "GOLD": _entry(1.0 + 29.0 / 60)}
    assert _check(raw)[0] == ""


def test_limits_are_configurable():
    assert _check(MIXED, max_spread_minutes=24 * 60)[0] == ""
    assert _check(PREVIOUS_RUN, max_age_hours=8.0)[0]


def test_pandas_timestamp_dates_from_mongo_work():
    raw = {
        code: SimpleNamespace(date=pd.Timestamp(entry.date))
        for code, entry in MIXED.items()
    }
    assert "mix of runs" in _check(raw)[0]
    raw = {
        code: SimpleNamespace(date=pd.Timestamp(entry.date))
        for code, entry in FRESH.items()
    }
    assert _check(raw) == ("", "")


def test_config_names_are_read():
    gen = object.__new__(CLS)
    asked = {}

    def get(name, default):
        asked[name] = default
        return default

    gen._data = SimpleNamespace(config=SimpleNamespace(get_element_or_default=get))
    gen._strategy_name = "dynamic_system"
    gen._log = mock.Mock()
    with mock.patch.object(CLS, "get_raw_optimal_position_data", return_value={}):
        gen.get_required_orders()
    assert asked == {
        "max_age_hours_raw_optimal_positions": 20.0,
        "max_spread_minutes_raw_optimal_positions": 30.0,
    }


# --- critic 2026-10-06: a missing date from parquet is NaT/NaN, not None -----


def test_nat_date_blocks():
    raw = {
        code: SimpleNamespace(date=pd.Timestamp(e.date)) for code, e in FRESH.items()
    }
    raw["ODD"] = SimpleNamespace(date=pd.NaT)
    block, _ = _check(raw)
    assert "without a date" in block and "ODD" in block


def test_nan_date_blocks():
    raw = dict(FRESH, ODD=SimpleNamespace(date=float("nan")))
    block, _ = _check(raw)
    assert "without a date" in block and "ODD" in block
