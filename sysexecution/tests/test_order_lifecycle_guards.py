"""
Guards added after the 2026-09-16 MSCIASIA incident: a calendar-spread roll
order was submitted three times because IB refused a price modification,
the local order status read as done, the algo walked away from a live
order, the stack handler created another broker order, and the resulting
over-fill killed the stack handler.

Design: the local order status is only a TRIGGER ("broker says something
happened"); whether an order is really gone is decided from the broker's
open-order list.
"""
import datetime
from types import SimpleNamespace
from unittest import mock

import pytest

from sysbrokers.IB.ib_orders import (
    ib_status_means_done_not_filled,
    ib_status_means_filled,
    open_order_keys_from_ib_trades,
    keys_for_db_broker_order,
)
from sysexecution.orders.base_orders import overFilledOrder
from sysexecution.orders.broker_orders import brokerOrder
from sysexecution.orders.contract_orders import contractOrder
from sysexecution.orders.named_order_objects import missing_order
from sysexecution.stack_handler.create_broker_orders_from_contract_orders import (
    stackHandlerCreateBrokerOrders,
)
from sysexecution.stack_handler.fills import stackHandlerForFills
from sysexecution.trade_qty import tradeQuantity


# --- broker status is a trigger only ----------------------------------------


@pytest.mark.parametrize("status", ["Cancelled", "ApiCancelled", "Inactive"])
def test_done_without_fill_triggers_explicit_cancel(status):
    assert ib_status_means_done_not_filled(status)


@pytest.mark.parametrize(
    "status", ["Submitted", "PreSubmitted", "PendingSubmit", "PendingCancel", "Filled"]
)
def test_working_or_filled_orders_do_not_trigger(status):
    assert not ib_status_means_done_not_filled(status)


def test_filled_is_recognised():
    assert ib_status_means_filled("Filled")
    assert not ib_status_means_filled("Inactive")


# --- identity keys against the broker's open-order list ---------------------


def _ib_trade(perm_id, client_id, order_id):
    order = SimpleNamespace(permId=perm_id, clientId=client_id, orderId=order_id)
    return SimpleNamespace(order=order)


def test_open_order_keys_use_both_permanent_and_temporary_ids():
    keys = open_order_keys_from_ib_trades(
        [_ib_trade(1246379236, 138, 373298), _ib_trade(0, 138, 373410)]
    )
    assert ("perm", 1246379236) in keys
    assert ("temp", 138, 373298) in keys
    # no permanent id yet: only the temporary key
    assert ("perm", 0) not in keys
    assert ("temp", 138, 373410) in keys


def _db_broker_order(tempid, permid):
    order = brokerOrder(
        "strategy",
        "INSTR",
        ["20260900", "20261200"],
        [-1, 1],
        broker_tempid=tempid,
        broker_permid=permid,
    )
    return order


def test_db_order_keys_parse_tempid_and_permid():
    keys = keys_for_db_broker_order(_db_broker_order("U123/138/373298", 1246379236))
    assert keys == {("perm", 1246379236), ("temp", 138, 373298)}


def test_db_order_keys_survive_missing_permid_and_odd_tempid():
    assert keys_for_db_broker_order(_db_broker_order("U123/138/373298", "")) == {
        ("temp", 138, 373298)
    }
    assert keys_for_db_broker_order(_db_broker_order("", 0)) == set()
    assert keys_for_db_broker_order(_db_broker_order("garbage", None)) == set()


def test_same_order_matches_across_the_two_views():
    ib_keys = open_order_keys_from_ib_trades([_ib_trade(1246379236, 138, 373298)])
    db_keys = keys_for_db_broker_order(_db_broker_order("U123/138/373298", 0))
    assert db_keys & ib_keys


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


def _contract_order_with_children(children, order_id=7084):
    order = contractOrder("strategy", "INSTR", ["20260900", "20261200"], [-1, 1])
    order._children = list(children)
    order._order_id = order_id
    return order


def _child(order_id, fill):
    child = brokerOrder("strategy", "INSTR", ["20260900", "20261200"], [-1, 1])
    child._order_id = order_id
    child._fill = tradeQuantity(fill)
    return child


def test_no_children_means_nothing_open():
    handler = _handler_with({}, open_ids=set())
    order = contractOrder("strategy", "INSTR", "20261200", 1)
    assert not handler.contract_order_has_unfilled_child_still_open_at_broker(order)


def test_unfilled_child_still_open_blocks_and_warns_once():
    children = {7719: _child(7719, [0, 0])}
    handler = _handler_with(children, open_ids={7719})
    order = _contract_order_with_children([7719])
    assert handler.contract_order_has_unfilled_child_still_open_at_broker(order)
    assert handler.contract_order_has_unfilled_child_still_open_at_broker(order)
    handler._log.warning.assert_called_once()
    handler._log.debug.assert_called_once()


def test_unfilled_child_gone_from_broker_does_not_block():
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


# --- an over-fill locks the instrument, stops the order, does not raise -----


def _fills_handler(raising_exc):
    handler = object.__new__(stackHandlerForFills)
    handler._contract_stack = mock.MagicMock()
    handler._contract_stack.change_fill_quantity_for_order.side_effect = raising_exc
    handler._log = mock.MagicMock()
    handler._data = mock.MagicMock()
    return handler


def _apply(handler, order, qty):
    handler.apply_fills_to_contract_order(
        contract_order_before_fill=order,
        filled_qty=tradeQuantity(qty),
        filled_price=-10.8,
        fill_datetime=datetime.datetime.now(),
    )


def test_overfill_locks_stops_order_and_logs_critical_once():
    handler = _fills_handler(overFilledOrder("fill [-2, 2] > trade [-1, 1]"))
    order = _contract_order_with_children([7719])
    with mock.patch("sysexecution.stack_handler.fills.dataLocks") as locks_class:
        locks = locks_class.return_value
        locks.is_instrument_locked.return_value = False
        _apply(handler, order, [-2, 2])
        # the next pass sees the same over-fill again: no second critical,
        # no re-lock even if the operator has since cleared the lock
        _apply(handler, order, [-3, 3])
        locks.add_lock_for_instrument.assert_called_once_with("INSTR")
    handler._contract_stack.stop_further_trading_of_order.assert_called_once_with(7084)
    handler._log.critical.assert_called_once()


def test_overfill_on_already_locked_instrument_does_not_relock():
    handler = _fills_handler(overFilledOrder("again"))
    order = _contract_order_with_children([7719])
    with mock.patch("sysexecution.stack_handler.fills.dataLocks") as locks_class:
        locks = locks_class.return_value
        locks.is_instrument_locked.return_value = True
        _apply(handler, order, [-2, 2])
        locks.add_lock_for_instrument.assert_not_called()
    handler._log.critical.assert_called_once()


def test_other_fill_errors_still_propagate():
    handler = _fills_handler(ValueError("something else"))
    order = _contract_order_with_children([7719])
    with pytest.raises(ValueError):
        _apply(handler, order, [-1, 1])
