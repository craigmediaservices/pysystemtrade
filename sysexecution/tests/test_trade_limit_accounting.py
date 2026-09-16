"""
Trade limits (2026-09-16): charged on fills as they land in the database,
not on submission; roll orders and manual fills are never charged; roll
orders are capped leg by leg against the instrument limit's headroom (so a
limit of 0 still stops a roll) without the float ratio of the proportional
resize.
"""
from unittest import mock

from sysexecution.orders.broker_orders import brokerOrder
from sysexecution.orders.contract_orders import contractOrder
from sysexecution.stack_handler.create_broker_orders_from_contract_orders import (
    stackHandlerCreateBrokerOrders,
    cap_each_leg_to_instrument_limit,
)
from sysexecution.stack_handler.fills import stackHandlerForFills
from sysexecution.trade_qty import tradeQuantity
from sysproduction.data.controls import (
    dataTradeLimits,
    is_roll_order,
    limit_size_of_quantity,
)


def _spread(strategy="strategy", roll=False, trade=(-13, 13)):
    return brokerOrder(
        strategy, "INSTR", ["20260900", "20261200"], list(trade), roll_order=roll
    )


# --- sizing and flags ----------------------------------------------------------


def test_sizing_is_total_absolute_quantity():
    assert limit_size_of_quantity(tradeQuantity([4])) == 4
    assert limit_size_of_quantity(tradeQuantity([-2, 2])) == 4
    assert limit_size_of_quantity(tradeQuantity([0, 0])) == 0


def test_roll_flag_is_read_from_broker_orders():
    assert is_roll_order(_spread(roll=True))
    assert not is_roll_order(_spread())


# --- charging on fills ---------------------------------------------------------


def _fills_handler():
    handler = object.__new__(stackHandlerForFills)
    handler._data = mock.MagicMock()
    handler._log = mock.MagicMock()
    return handler


def _charge(before, after):
    with mock.patch("sysexecution.stack_handler.fills.dataTradeLimits") as limits_class:
        _fills_handler().charge_new_fills_to_trade_limits(before, after)
        return limits_class.return_value.add_trade_quantity


def test_new_fills_are_charged_as_a_delta():
    before = _spread()
    before._fill = tradeQuantity([-1, 1])
    after = _spread()
    after._fill = tradeQuantity([-3, 3])
    add = _charge(before, after)
    add.assert_called_once()
    assert add.call_args[0][1] == 4


def test_unfilled_orders_are_never_charged():
    _charge(_spread(), _spread()).assert_not_called()


def test_roll_fills_are_not_charged():
    after = _spread(roll=True)
    after._fill = tradeQuantity([-13, 13])
    _charge(_spread(roll=True), after).assert_not_called()


def test_manual_fills_are_not_charged():
    after = _spread()
    after._fill = tradeQuantity([-13, 13])
    after.manual_fill = True
    _charge(_spread(), after).assert_not_called()


def test_add_trade_quantity_ignores_nothing_to_add():
    limits = object.__new__(dataTradeLimits)
    with mock.patch.object(
        dataTradeLimits, "db_trade_limit_data", new_callable=mock.PropertyMock
    ) as prop:
        limits.add_trade_quantity(mock.MagicMock(), 0)
        prop.return_value.add_trade.assert_not_called()


# --- roll orders: leg-by-leg cap against instrument headroom -----------------


def _roll(trade):
    return contractOrder(
        "_ROLL_PSEUDO_STRATEGY",
        "INSTR",
        ["20260900", "20261200"] if len(trade) == 2 else "20260900",
        list(trade),
        roll_order=True,
    )


def _limits(headroom):
    limits = mock.MagicMock()
    limits.what_trade_qty_possible_for_instrument_code.side_effect = lambda ic, q: min(
        q, headroom
    )
    return limits


def test_roll_within_headroom_passes_whole():
    assert cap_each_leg_to_instrument_limit(_roll([-13, 13]), _limits(13)).trade == (
        tradeQuantity([-13, 13])
    )


def test_roll_is_capped_leg_by_leg_without_rounding_loss():
    # the proportional resize floors [-22, 22] with headroom 15 to [-14, 14]
    assert cap_each_leg_to_instrument_limit(_roll([-22, 22]), _limits(15)).trade == (
        tradeQuantity([-15, 15])
    )
    assert cap_each_leg_to_instrument_limit(_roll([-49, 49]), _limits(1)).trade == (
        tradeQuantity([-1, 1])
    )


def test_limit_zero_still_stops_a_roll():
    assert cap_each_leg_to_instrument_limit(_roll([-13, 13]), _limits(0)).trade == (
        tradeQuantity([0, 0])
    )


def test_outright_roll_leg_is_capped_the_same_way():
    assert cap_each_leg_to_instrument_limit(_roll([-13]), _limits(4)).trade == (
        tradeQuantity([-4])
    )


def test_strategy_orders_are_limited_as_before():
    handler = object.__new__(stackHandlerCreateBrokerOrders)
    handler._data = mock.MagicMock()
    handler._log = mock.MagicMock()
    order = contractOrder("strategy", "INSTR", "20261200", 10)
    with mock.patch(
        "sysexecution.stack_handler.create_broker_orders_from_contract_orders.dataTradeLimits"
    ) as limits_class:
        limits_class.return_value.what_trade_is_possible_for_strategy_instrument.return_value = (
            4
        )
        result = handler.apply_trade_limits_to_contract_order(order)
    assert result.trade == tradeQuantity([4])


def test_roll_orders_go_through_the_leg_cap():
    handler = object.__new__(stackHandlerCreateBrokerOrders)
    handler._data = mock.MagicMock()
    handler._log = mock.MagicMock()
    with mock.patch(
        "sysexecution.stack_handler.create_broker_orders_from_contract_orders.dataTradeLimits"
    ) as limits_class:
        limits_class.return_value.what_trade_qty_possible_for_instrument_code.side_effect = lambda ic, q: min(
            q, 4
        )
        result = handler.apply_trade_limits_to_contract_order(_roll([-13, 13]))
        limits_class.return_value.what_trade_is_possible_for_strategy_instrument.assert_not_called()
    assert result.trade == tradeQuantity([-4, 4])
