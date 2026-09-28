"""
Balance trades (2026-09-28): the strategy-level order made from a balance
contract order must carry the NET of the legs. It used to take the first
leg, so a spread balance trade booked by hand moved the strategy position.
"""
import datetime

from sysexecution.orders.broker_orders import brokerOrder, balance_order_type
from sysexecution.stack_handler.balance_trades import (
    create_balance_contract_order_from_broker_order,
    create_balance_instrument_order_from_contract_order,
)


def _balance_broker_order(contract_dates, qty):
    return brokerOrder(
        "dynamic_system",
        "MSCIASIA",
        contract_dates,
        qty,
        fill=qty,
        algo_used="balance_trade",
        order_type=balance_order_type,
        filled_price=1127.4,
        fill_datetime=datetime.datetime(2026, 9, 16, 3, 47),
        manual_fill=True,
        active=False,
    )


def test_outright_balance_trade_moves_strategy_by_the_trade():
    contract_order = create_balance_contract_order_from_broker_order(
        _balance_broker_order("20260900", -2)
    )
    instrument_order = create_balance_instrument_order_from_contract_order(
        contract_order
    )
    assert instrument_order.trade.as_single_trade_qty_or_error() == -2
    assert instrument_order.fill.as_single_trade_qty_or_error() == -2


def test_spread_balance_trade_is_flat_at_strategy_level():
    contract_order = create_balance_contract_order_from_broker_order(
        _balance_broker_order(["20260900", "20261200"], [-2, 2])
    )
    instrument_order = create_balance_instrument_order_from_contract_order(
        contract_order
    )
    assert instrument_order.trade.as_single_trade_qty_or_error() == 0
    assert instrument_order.fill.as_single_trade_qty_or_error() == 0


def test_unequal_spread_balance_trade_moves_strategy_by_the_net():
    contract_order = create_balance_contract_order_from_broker_order(
        _balance_broker_order(["20260900", "20261200"], [-3, 2])
    )
    instrument_order = create_balance_instrument_order_from_contract_order(
        contract_order
    )
    assert instrument_order.trade.as_single_trade_qty_or_error() == -1
    assert instrument_order.strategy_name == "dynamic_system"
    assert instrument_order.instrument_code == "MSCIASIA"
