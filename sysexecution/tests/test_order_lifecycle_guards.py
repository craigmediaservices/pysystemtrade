"""
Guards added after the 2026-09-16 MSCIASIA incident: a calendar-spread roll
order was submitted three times because IB reported 'Inactive' after refusing
a price modification, the algo treated Inactive as cancelled and walked away
from a live order, the stack handler created another broker order, and the
resulting over-fill killed the stack handler.
"""
import datetime
from unittest import mock

import pytest

from sysbrokers.IB.ib_orders import (
    ib_status_means_cancelled,
    ib_status_means_inactive,
    ib_status_means_open,
)
from sysexecution.algos.algo_original_best import order_must_be_cancelled_not_modified
from sysexecution.orders.base_orders import overFilledOrder
from sysexecution.orders.broker_orders import brokerOrder
from sysexecution.orders.contract_orders import contractOrder
from sysexecution.orders.named_order_objects import missing_order
from sysexecution.stack_handler.create_broker_orders_from_contract_orders import (
    stackHandlerCreateBrokerOrders,
)
from sysexecution.stack_handler.fills import stackHandlerForFills
from sysexecution.trade_qty import tradeQuantity


# --- broker status interpretation -------------------------------------------


@pytest.mark.parametrize("status", ["Cancelled", "ApiCancelled"])
def test_explicit_cancellations_are_cancelled(status):
    assert ib_status_means_cancelled(status)
    assert not ib_status_means_open(status)


@pytest.mark.parametrize(
    "status",
    ["Inactive", "Submitted", "PreSubmitted", "PendingSubmit", "PendingCancel"],
)
def test_working_or_inactive_orders_are_not_cancelled(status):
    # Inactive is the important one: IB uses it for a working order whose
    # modification was refused, and that order can still fill
    assert not ib_status_means_cancelled(status)
    assert ib_status_means_open(status)


def test_inactive_is_recognised_separately():
    assert ib_status_means_inactive("Inactive")
    assert not ib_status_means_inactive("Submitted")
    assert not ib_status_means_inactive("Cancelled")


def test_filled_is_neither_cancelled_nor_open():
    assert not ib_status_means_cancelled("Filled")
    assert not ib_status_means_open("Filled")


# --- spread orders are cancelled and re-placed, never modified ---------------


def _broker_order(contract_id, trade):
    return brokerOrder("strategy", "INSTR", contract_id, trade)


def test_calendar_spread_orders_are_cancelled_not_modified():
    spread = _broker_order(["20260900", "20261200"], [-1, 1])
    assert spread.calendar_spread_order
    assert order_must_be_cancelled_not_modified(spread)


def test_outright_orders_can_still_be_modified():
    outright = _broker_order("20261200", 1)
    assert not outright.calendar_spread_order
    assert not order_must_be_cancelled_not_modified(outright)


def test_objects_without_the_flag_default_to_modify():
    assert not order_must_be_cancelled_not_modified(object())


# --- no second broker order while the first is still working ----------------


class _FakeBrokerStack:
    def __init__(self, orders):
        self._orders = orders

    def get_list_of_orders_from_order_id_list(self, id_list):
        return [self._orders[i] for i in id_list]


class _FakeDataBroker:
    def __init__(self, open_ids):
        self.open_ids = open_ids
        self.asked = []

    def check_order_is_still_open_at_broker(self, broker_order):
        self.asked.append(broker_order.order_id)
        return broker_order.order_id in self.open_ids


def _handler_with(broker_orders, open_ids):
    handler = object.__new__(stackHandlerCreateBrokerOrders)
    handler._broker_stack = _FakeBrokerStack(broker_orders)
    handler._data_broker = _FakeDataBroker(open_ids)
    handler._log = mock.MagicMock()
    return handler


def _contract_order_with_children(children):
    order = contractOrder("strategy", "INSTR", ["20260900", "20261200"], [-1, 1])
    order._children = list(children)
    return order


def _child(order_id, fill):
    child = _broker_order(["20260900", "20261200"], [-1, 1])
    child._order_id = order_id
    child._fill = tradeQuantity(fill)
    return child


def test_no_children_means_nothing_open():
    handler = _handler_with({}, open_ids=set())
    order = contractOrder("strategy", "INSTR", "20261200", 1)
    assert not handler.contract_order_has_unfilled_child_still_open_at_broker(order)


def test_unfilled_child_still_open_blocks_new_broker_order():
    children = {7719: _child(7719, [0, 0])}
    handler = _handler_with(children, open_ids={7719})
    order = _contract_order_with_children([7719])
    assert handler.contract_order_has_unfilled_child_still_open_at_broker(order)
    handler._log.warning.assert_called_once()


def test_unfilled_child_that_is_gone_at_broker_does_not_block():
    children = {7719: _child(7719, [0, 0])}
    handler = _handler_with(children, open_ids=set())
    order = _contract_order_with_children([7719])
    assert not handler.contract_order_has_unfilled_child_still_open_at_broker(order)


def test_filled_children_are_not_queried():
    children = {7719: _child(7719, [-1, 1]), 7721: _child(7721, [0, 0])}
    handler = _handler_with(children, open_ids={7719})
    order = _contract_order_with_children([7719, 7721])
    assert not handler.contract_order_has_unfilled_child_still_open_at_broker(order)
    assert handler._data_broker.asked == [7721]


def test_missing_children_are_skipped():
    children = {7719: missing_order}
    handler = _handler_with(children, open_ids=set())
    order = _contract_order_with_children([7719])
    assert not handler.contract_order_has_unfilled_child_still_open_at_broker(order)


# --- an over-fill locks the instrument instead of killing the process --------


def _fills_handler(raising_exc):
    handler = object.__new__(stackHandlerForFills)
    handler._contract_stack = mock.MagicMock()
    handler._contract_stack.change_fill_quantity_for_order.side_effect = raising_exc
    handler._log = mock.MagicMock()
    handler._data = mock.MagicMock()
    return handler


def test_overfill_locks_instrument_and_does_not_raise():
    handler = _fills_handler(overFilledOrder("fill [-2, 2] > trade [-1, 1]"))
    order = _contract_order_with_children([7719])
    order._order_id = 7084
    with mock.patch("sysexecution.stack_handler.fills.dataLocks") as data_locks_class:
        data_locks = data_locks_class.return_value
        data_locks.is_instrument_locked.return_value = False
        handler.apply_fills_to_contract_order(
            contract_order_before_fill=order,
            filled_qty=tradeQuantity([-2, 2]),
            filled_price=-10.8,
            fill_datetime=datetime.datetime.now(),
        )
        data_locks.add_lock_for_instrument.assert_called_once_with("INSTR")
    handler._log.critical.assert_called_once()


def test_overfill_on_already_locked_instrument_is_quiet():
    handler = _fills_handler(overFilledOrder("again"))
    order = _contract_order_with_children([7719])
    order._order_id = 7084
    with mock.patch("sysexecution.stack_handler.fills.dataLocks") as data_locks_class:
        data_locks = data_locks_class.return_value
        data_locks.is_instrument_locked.return_value = True
        handler.apply_fills_to_contract_order(
            contract_order_before_fill=order,
            filled_qty=tradeQuantity([-2, 2]),
            filled_price=-10.8,
            fill_datetime=datetime.datetime.now(),
        )
        data_locks.add_lock_for_instrument.assert_not_called()
    handler._log.critical.assert_not_called()


def test_other_fill_errors_still_propagate():
    handler = _fills_handler(ValueError("something else"))
    order = _contract_order_with_children([7719])
    order._order_id = 7084
    with pytest.raises(ValueError):
        handler.apply_fills_to_contract_order(
            contract_order_before_fill=order,
            filled_qty=tradeQuantity([-1, 1]),
            filled_price=-10.8,
            fill_datetime=datetime.datetime.now(),
        )
