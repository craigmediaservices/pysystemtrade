"""
Trade limits (2026-09-16): count filled quantity, not submitted quantity, and
do not apply limits to roll orders.
"""
from unittest import mock

from sysexecution.orders.broker_orders import brokerOrder
from sysexecution.orders.contract_orders import contractOrder
from sysexecution.stack_handler.create_broker_orders_from_contract_orders import (
    stackHandlerCreateBrokerOrders,
)
from sysexecution.trade_qty import tradeQuantity
from sysproduction.data.controls import dataTradeLimits


def _limits_with_fake_db():
    limits = object.__new__(dataTradeLimits)
    fake_db = mock.MagicMock()
    with mock.patch.object(
        dataTradeLimits, "db_trade_limit_data", new_callable=mock.PropertyMock
    ) as prop:
        prop.return_value = fake_db
        yield limits, fake_db


def test_unfilled_order_consumes_no_limit():
    for limits, fake_db in _limits_with_fake_db():
        order = brokerOrder("strategy", "INSTR", ["20260900", "20261200"], [-5, 5])
        limits.add_trade(order)
        fake_db.add_trade.assert_not_called()


def test_partial_fill_counts_only_the_fill():
    for limits, fake_db in _limits_with_fake_db():
        order = brokerOrder("strategy", "INSTR", ["20260900", "20261200"], [-5, 5])
        order._fill = tradeQuantity([-2, 2])
        limits.add_trade(order)
        fake_db.add_trade.assert_called_once()
        assert fake_db.add_trade.call_args[0][1] == 4


def test_roll_orders_bypass_trade_limits():
    handler = object.__new__(stackHandlerCreateBrokerOrders)
    handler._data = mock.MagicMock()
    roll = contractOrder(
        "_ROLL_PSEUDO_STRATEGY",
        "INSTR",
        ["20260900", "20261200"],
        [-13, 13],
        roll_order=True,
    )
    with mock.patch(
        "sysexecution.stack_handler.create_broker_orders_from_contract_orders.dataTradeLimits"
    ) as limits_class:
        result = handler.apply_trade_limits_to_contract_order(roll)
        limits_class.assert_not_called()
    assert result.trade == roll.trade


def test_strategy_orders_are_still_limited():
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
