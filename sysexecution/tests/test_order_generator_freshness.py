"""
Order generator freshness guard (2026-10-06): the generator runs on its own
timer and must not trade on raw optimal positions that run_systems has not
(fully) rewritten for its latest run.
"""
import datetime
from types import SimpleNamespace
from unittest import mock

from sysexecution.strategies import dynamic_optimised_positions as dop
from sysobjects.production.optimal_positions import optimalPositionWithReference

NOW = datetime.datetime(2026, 10, 6, 3, 30)
CLS = dop.orderGeneratorForDynamicPositions


def _entry(hours_old, now=NOW):
    return optimalPositionWithReference(
        date=now - datetime.timedelta(hours=hours_old),
        optimal_position=1.0,
        reference_price=100.0,
        reference_contract="20261200",
        reference_date=NOW - datetime.timedelta(days=1),
    )


FRESH = {"SP500": _entry(1.2), "GOLD": _entry(0.6), "US10": _entry(1.0)}
# run_systems half-way through its writes: some fresh, some from the last run
PARTIAL = {"SP500": _entry(0.5), "GOLD": _entry(16.5), "US10": _entry(16.4)}
# run_systems has not written anything yet
ALL_OLD = {"SP500": _entry(8.4), "GOLD": _entry(8.3), "US10": _entry(8.5)}


def _generator():
    gen = object.__new__(CLS)
    gen._strategy_name = "dynamic_system"
    gen._data = SimpleNamespace(
        config=SimpleNamespace(get_element_or_default=lambda name, default: default)
    )
    gen._log = mock.Mock()
    return gen


def _relative_to_wall_clock(raw):
    # the generator checks against datetime.now(); rebuild the same ages
    now = datetime.datetime.now()
    return {
        code: _entry((NOW - entry.date).total_seconds() / 3600.0, now=now)
        for code, entry in raw.items()
    }


def _generate(raw):
    raw = _relative_to_wall_clock(raw)
    gen = _generator()
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


# --- the generator itself ----------------------------------------------------


def test_half_written_positions_generate_no_orders():
    gen, calc, trades, result = _generate(PARTIAL)
    calc.assert_not_called()  # optimised positions are not even written
    trades.assert_not_called()
    assert len(result) == 0
    gen._log.critical.assert_called_once()
    assert "NOT generating orders" in gen._log.critical.call_args.args[0]


def test_positions_from_the_previous_run_generate_no_orders():
    gen, calc, trades, _ = _generate(ALL_OLD)
    calc.assert_not_called()
    trades.assert_not_called()
    gen._log.critical.assert_called_once()


def test_fresh_positions_are_traded_on_without_a_second_read():
    gen, calc, trades, _ = _generate(FRESH)
    calc.assert_called_once()
    assert set(calc.call_args.kwargs["raw_optimal_position_data"]) == set(FRESH)
    trades.assert_called_once()
    gen._log.critical.assert_not_called()


# --- the pure check -------------------------------------------------------------


def test_all_fresh_is_not_stale():
    assert dop.why_raw_optimal_positions_are_stale(FRESH, NOW, 6.0) == ""


def test_partial_write_is_stale_and_names_instruments():
    why = dop.why_raw_optimal_positions_are_stale(PARTIAL, NOW, 6.0)
    assert "2 of 3" in why and "GOLD" in why and "US10" in why


def test_no_positions_at_all_is_stale():
    assert dop.why_raw_optimal_positions_are_stale({}, NOW, 6.0)


def test_undated_entry_is_stale():
    raw = dict(FRESH, ODD=SimpleNamespace())
    assert "ODD" in dop.why_raw_optimal_positions_are_stale(raw, NOW, 6.0)


def test_threshold_sits_between_schedule_gaps():
    # 03:30 / 11:30 generator vs 02:05 / 10:05 run_systems, 480 min timers:
    # a completed run is < ~1.5h old, the previous run's >= ~8h old
    assert 1.5 < dop.MAX_AGE_HOURS_RAW_OPTIMAL_POSITIONS < 8.0


def test_pandas_timestamp_dates_from_mongo_work():
    import pandas as pd

    raw = {
        "SP500": SimpleNamespace(date=pd.Timestamp(NOW - datetime.timedelta(hours=1)))
    }
    assert dop.why_raw_optimal_positions_are_stale(raw, NOW, 6.0) == ""
