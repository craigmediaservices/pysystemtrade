"""
Trade limits (2026-09-16): charged on fills as they land, sized by the
position for roll orders (largest leg) and by total quantity otherwise;
rolls still obey the instrument limit (limit 0 still stops them); and a
contract order cannot spawn more than a fixed number of broker orders.
"""
from unittest import mock

from sysexecution.orders.broker_orders import brokerOrder
from sysexecution.orders.contract_orders import contractOrder
from sysexecution.stack_handler.create_broker_orders_from_contract_orders import (
    stackHandlerCreateBrokerOrders,
    MAX_BROKER_ORDERS_PER_CONTRACT_ORDER,
)
from sysexecution.stack_handler.fills import stackHandlerForFills
from sysexecution.trade_qty import tradeQuantity
from sysproduction.data.controls import dataTradeLimits, limit_size_of_quantity


def _spread(strategy="strategy", roll=False, trade=(-13, 13)):
    return brokerOrder(
        strategy, "INSTR", ["20260900", "20261200"], list(trade), roll_order=roll
    )


# --- sizing ------------------------------------------------------------------


def test_strategy_orders_count_total_quantity():
    order = brokerOrder("strategy", "INSTR", "20261200", 4)
    assert limit_size_of_quantity(order, tradeQuantity([4])) == 4
    assert limit_size_of_quantity(_spread(), tradeQuantity([-2, 2])) == 4


def test_roll_orders_count_the_position_being_rolled():
    assert limit_size_of_quantity(_spread(roll=True), tradeQuantity([-13, 13])) == 13
    leg = brokerOrder(
        "_ROLL_PSEUDO_STRATEGY", "INSTR", "20260900", -13, roll_order=True
    )
    assert limit_size_of_quantity(leg, tradeQuantity([-13])) == 13


def test_zero_quantity_costs_nothing():
    assert limit_size_of_quantity(_spread(), tradeQuantity([0, 0])) == 0


# --- charging on fills ---------------------------------------------------------


def _fills_handler():
    handler = object.__new__(stackHandlerForFills)
    handler._data = mock.MagicMock()
    return handler


def test_new_fills_are_charged_as_a_delta():
    handler = _fills_handler()
    before = _spread()
    before._fill = tradeQuantity([-1, 1])
    after = _spread()
    after._fill = tradeQuantity([-3, 3])
    with mock.patch("sysexecution.stack_handler.fills.dataTradeLimits") as limits_class:
        handler.charge_new_fills_to_trade_limits(before, after)
        limits_class.return_value.add_trade_quantity.assert_called_once()
        assert limits_class.return_value.add_trade_quantity.call_args[0][1] == 4


def test_unfilled_orders_are_never_charged():
    handler = _fills_handler()
    with mock.patch("sysexecution.stack_handler.fills.dataTradeLimits") as limits_class:
        handler.charge_new_fills_to_trade_limits(_spread(), _spread())
        limits_class.return_value.add_trade_quantity.assert_not_called()


def test_roll_fills_are_charged_by_position():
    handler = _fills_handler()
    after = _spread(roll=True)
    after._fill = tradeQuantity([-13, 13])
    with mock.patch("sysexecution.stack_handler.fills.dataTradeLimits") as limits_class:
        handler.charge_new_fills_to_trade_limits(_spread(roll=True), after)
        assert limits_class.return_value.add_trade_quantity.call_args[0][1] == 13


def test_add_trade_quantity_ignores_nothing_to_add():
    limits = object.__new__(dataTradeLimits)
    with mock.patch.object(
        dataTradeLimits, "db_trade_limit_data", new_callable=mock.PropertyMock
    ) as prop:
        limits.add_trade_quantity(mock.MagicMock(), 0)
        prop.return_value.add_trade.assert_not_called()


# --- checking before submission -------------------------------------------------


def _handler():
    handler = object.__new__(stackHandlerCreateBrokerOrders)
    handler._data = mock.MagicMock()
    handler._log = mock.MagicMock()
    return handler


def _roll_contract_order(trade=(-13, 13)):
    return contractOrder(
        "_ROLL_PSEUDO_STRATEGY",
        "INSTR",
        ["20260900", "20261200"],
        list(trade),
        roll_order=True,
    )


def _with_instrument_limit(possible):
    patcher = mock.patch(
        "sysexecution.stack_handler.create_broker_orders_from_contract_orders.dataTradeLimits"
    )
    limits_class = patcher.start()
    limits_class.return_value.what_trade_qty_possible_for_instrument_code.return_value = (
        possible
    )
    limits_class.return_value.what_trade_is_possible_for_strategy_instrument.return_value = (
        possible
    )
    return patcher, limits_class


def test_roll_within_instrument_limit_passes_whole():
    patcher, limits_class = _with_instrument_limit(13)
    try:
        result = _handler().apply_trade_limits_to_contract_order(_roll_contract_order())
    finally:
        patcher.stop()
    assert result.trade == tradeQuantity([-13, 13])
    limits_class.return_value.what_trade_qty_possible_for_instrument_code.assert_called_once_with(
        "INSTR", 13
    )


def test_roll_is_cut_by_instrument_limit():
    patcher, _ = _with_instrument_limit(4)
    try:
        result = _handler().apply_trade_limits_to_contract_order(_roll_contract_order())
    finally:
        patcher.stop()
    assert result.trade == tradeQuantity([-4, 4])


def test_limit_zero_still_stops_a_roll():
    patcher, _ = _with_instrument_limit(0)
    try:
        result = _handler().apply_trade_limits_to_contract_order(_roll_contract_order())
    finally:
        patcher.stop()
    assert result.trade == tradeQuantity([0, 0])


def test_strategy_orders_are_limited_as_before():
    patcher, _ = _with_instrument_limit(4)
    try:
        order = contractOrder("strategy", "INSTR", "20261200", 10)
        result = _handler().apply_trade_limits_to_contract_order(order)
    finally:
        patcher.stop()
    assert result.trade == tradeQuantity([4])


# --- cap on broker orders per contract order --------------------------------


def test_child_cap_blocks_and_logs_critical_once():
    handler = _handler()
    order = contractOrder("strategy", "INSTR", "20261200", 1)
    order._children = list(range(MAX_BROKER_ORDERS_PER_CONTRACT_ORDER))
    order._order_id = 7084
    assert handler.contract_order_has_too_many_children(order)
    assert handler.contract_order_has_too_many_children(order)
    handler._log.critical.assert_called_once()


def test_child_cap_allows_normal_orders():
    handler = _handler()
    order = contractOrder("strategy", "INSTR", "20261200", 1)
    order._children = [1, 2]
    assert not handler.contract_order_has_too_many_children(order)
