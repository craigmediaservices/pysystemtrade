"""
The broker side of completing zero-fill broker orders IB no longer knows
about: 'gone, unfilled' is only confirmed from a fresh open-order list (the
same authoritative check as cancel-and-confirm) plus the executions IB has
reported to this session, never from ib_async's local status, and never for
an order whose ids we don't know.

reqExecutions is asked fresh: the session's fill cache comes from ib_async's
startup sync, which timed out in production (2026-09-23, 2026-10-05) and
then is empty exactly when an order can't be matched.
"""
import asyncio
import datetime
from types import SimpleNamespace
from unittest import mock

from sysbrokers.IB.ib_orders import (
    ibExecutionStackData,
    order_keys_from_ib_fills,
    order_submitted_today,
)
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


# someone else's execution today, so the fresh reply is a real answer
OTHER_EXECUTION = (555, 77, 1)


def _ib_stack(open_trades=(), fills=(), todays_fills=None, executions_error=None):
    stack = object.__new__(ibExecutionStackData)
    ib = mock.MagicMock()
    ib.reqAllOpenOrders.return_value = list(open_trades)
    ib.fills.return_value = list(fills)
    if todays_fills is None:
        todays_fills = [_ib_fill(*OTHER_EXECUTION)]
    ib.reqExecutions.return_value = list(todays_fills)
    if executions_error is not None:
        ib.reqExecutions.side_effect = executions_error
    stack._ib_client = SimpleNamespace(ib=ib)
    stack.log = mock.MagicMock()
    return stack


def _db_order(tempid=TEMPID, permid=PERMID, submitted=None):
    order = brokerOrder(
        "strat", "R1000", "20261200", 1, broker_tempid=tempid, broker_permid=permid
    )
    order.submit_datetime = datetime.datetime.now() if submitted is None else submitted
    return order


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


def test_startup_sync_failed_but_fresh_executions_show_a_fill():
    # ib.fills() empty (startup sync timed out), the order DID fill today
    stack = _ib_stack(fills=[], todays_fills=[_ib_fill(PERMID, 0, 0)])
    assert not stack.check_unfilled_order_is_gone_from_broker(_db_order())


def test_fresh_executions_matched_on_temp_id_too():
    stack = _ib_stack(fills=[], todays_fills=[_ib_fill(0, 138, 373298)])
    assert not stack.check_unfilled_order_is_gone_from_broker(_db_order())


def test_executions_request_timing_out_means_not_gone():
    stack = _ib_stack(executions_error=asyncio.TimeoutError())
    assert not stack.check_unfilled_order_is_gone_from_broker(_db_order())
    stack.log.warning.assert_called_once()


def test_executions_request_failing_means_not_gone():
    stack = _ib_stack(executions_error=ConnectionError("gateway went away"))
    assert not stack.check_unfilled_order_is_gone_from_broker(_db_order())


def test_empty_executions_reply_means_not_gone():
    # ib_async answers an errored request with an empty list
    stack = _ib_stack(todays_fills=[])
    assert not stack.check_unfilled_order_is_gone_from_broker(_db_order())


def test_order_submitted_yesterday_is_left_for_end_of_day():
    yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
    stack = _ib_stack()
    assert not stack.check_unfilled_order_is_gone_from_broker(
        _db_order(submitted=yesterday)
    )
    stack._ib_client.ib.reqExecutions.assert_not_called()


def test_order_without_submit_time_is_never_gone():
    order = _db_order()
    order.submit_datetime = None
    assert not _ib_stack().check_unfilled_order_is_gone_from_broker(order)


def test_submitted_today_is_by_calendar_date():
    now = datetime.datetime(2026, 10, 5, 2, 30)
    assert order_submitted_today(
        _db_order(submitted=datetime.datetime(2026, 10, 5, 0, 1)), now
    )
    assert not order_submitted_today(
        _db_order(submitted=datetime.datetime(2026, 10, 4, 23, 59)), now
    )
