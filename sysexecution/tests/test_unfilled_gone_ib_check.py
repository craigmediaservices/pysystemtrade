"""
The broker side of completing zero-fill broker orders IB no longer knows
about: 'gone, unfilled' is only confirmed from a fresh open-order list (the
same authoritative check as cancel-and-confirm) plus the executions IB has
reported to this session, never from ib_async's local status, and never for
an order whose ids we don't know.
"""
from types import SimpleNamespace
from unittest import mock

from sysbrokers.IB.ib_orders import ibExecutionStackData, order_keys_from_ib_fills
from sysexecution.orders.broker_orders import brokerOrder

TEMPID = "U5570413/138/373298"
PERMID = 1246379236


def _ib_open_trade(perm_id, client_id, order_id):
    return SimpleNamespace(
        order=SimpleNamespace(permId=perm_id, clientId=client_id, orderId=order_id)
    )


def _ib_fill(perm_id, client_id, order_id):
    return SimpleNamespace(
        execution=SimpleNamespace(permId=perm_id, clientId=client_id, orderId=order_id)
    )


def _ib_stack(open_trades=(), fills=()):
    stack = object.__new__(ibExecutionStackData)
    ib = mock.MagicMock()
    ib.reqAllOpenOrders.return_value = list(open_trades)
    ib.fills.return_value = list(fills)
    stack._ib_client = SimpleNamespace(ib=ib)
    stack.log = mock.MagicMock()
    return stack


def _db_order(tempid=TEMPID, permid=PERMID):
    return brokerOrder(
        "strat", "R1000", "20261200", 1, broker_tempid=tempid, broker_permid=permid
    )


def test_ib_gone_and_never_executed_is_confirmed():
    stack = _ib_stack(open_trades=[_ib_open_trade(999, 138, 1)])
    assert stack.check_unfilled_order_is_gone_from_broker(_db_order())


def test_ib_still_open_is_not_gone():
    stack = _ib_stack(open_trades=[_ib_open_trade(PERMID, 138, 373298)])
    assert not stack.check_unfilled_order_is_gone_from_broker(_db_order())


def test_ib_still_open_under_temp_id_only_is_not_gone():
    stack = _ib_stack(open_trades=[_ib_open_trade(0, 138, 373298)])
    assert not stack.check_unfilled_order_is_gone_from_broker(_db_order())


def test_ib_gone_but_executed_is_not_confirmed_unfilled():
    stack = _ib_stack(fills=[_ib_fill(PERMID, 0, 0)])
    assert not stack.check_unfilled_order_is_gone_from_broker(_db_order())


def test_ib_order_without_ids_is_never_confirmed_gone():
    stack = _ib_stack()
    assert not stack.check_unfilled_order_is_gone_from_broker(
        _db_order(tempid="", permid="")
    )


def test_fill_keys_use_both_ids():
    assert order_keys_from_ib_fills([_ib_fill(PERMID, 138, 373298)]) == {
        ("perm", PERMID),
        ("temp", 138, 373298),
    }
