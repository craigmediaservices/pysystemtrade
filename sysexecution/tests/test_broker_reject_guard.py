"""
Reject -> resubmit loop guard, part (b) (2026-10-02: R1000 produced 37
IB-cancelled broker orders between 03:33 and 09:27, 'Order price is outside
price limits', because every zero-fill end made the stack handler place
another). Once a contract order has 3 children the broker refused (tagged
'IB reject ...' in algo_comment by the broker layer), the stack handler
stops creating broker orders for it and logs CRITICAL once. Our own algo
timeouts (zero-fill cancels, 2-3 an hour is normal) never count, and the
instrument is not locked.
"""
from unittest import mock

from sysexecution.orders.broker_orders import brokerOrder
from sysexecution.orders.contract_orders import contractOrder
from sysexecution.orders.named_order_objects import missing_order
from sysexecution.stack_handler.create_broker_orders_from_contract_orders import (
    stackHandlerCreateBrokerOrders,
)
from sysexecution.tests.test_cancel_unsubmitted_orders import memBrokerStack
from sysexecution.trade_qty import tradeQuantity

OUR_CANCEL = "Error 202, reqId 373298: Order Canceled - reason:"


def _broker_order(algo_comment="", fill=0, parent=7084):
    order = brokerOrder("strat", "R1000", "20261200", 1, parent=parent)
    order.algo_comment = algo_comment
    order._fill = tradeQuantity(fill)
    return order


# --- (b) the stack handler stops resending ----------------------------------


class _FakeDataBroker:
    def __init__(self):
        self.asked_open = []

    def check_order_is_still_open_at_broker(self, broker_order):
        self.asked_open.append(broker_order.order_id)
        return False

    def is_contract_okay_to_trade(self, futures_contract):
        return True


def _handler_with_children(comments):
    broker_stack = memBrokerStack()
    child_ids = [
        broker_stack.put_order_on_stack(_broker_order(comment)) for comment in comments
    ]
    contract_order = contractOrder(
        "strat", "R1000", "20261200", 1, order_id=7084, children=child_ids
    )

    handler = object.__new__(stackHandlerCreateBrokerOrders)
    handler._broker_stack = broker_stack
    handler._data_broker = _FakeDataBroker()
    handler._data = mock.MagicMock()
    handler._log = mock.MagicMock()
    handler.size_contract_order = lambda order: order

    return handler, contract_order


def _preprocess(handler, contract_order):
    with mock.patch(
        "sysexecution.stack_handler.create_broker_orders_from_contract_orders.dataLocks"
    ) as locks_class:
        locks_class.return_value.is_instrument_locked.return_value = False
        result = handler.preprocess_contract_order(contract_order)
        locks_class.return_value.add_lock_for_instrument.assert_not_called()
    return result


IB_REJECT = "IB reject 202: Order price is outside price limits | log"


def test_three_broker_rejects_stop_resending_with_one_critical():
    handler, contract_order = _handler_with_children([IB_REJECT] * 3)

    assert _preprocess(handler, contract_order) is missing_order
    assert _preprocess(handler, contract_order) is missing_order

    handler._log.critical.assert_called_once()
    assert "Order price is outside price limits" in str(handler._log.critical.call_args)


def test_two_broker_rejects_still_resend():
    handler, contract_order = _handler_with_children([IB_REJECT] * 2)

    assert _preprocess(handler, contract_order) is contract_order
    handler._log.critical.assert_not_called()


def test_our_own_algo_timeouts_never_stop_resending():
    handler, contract_order = _handler_with_children([OUR_CANCEL] * 6)

    assert _preprocess(handler, contract_order) is contract_order
    handler._log.critical.assert_not_called()


def test_timeouts_and_rejects_mixed_count_only_rejects():
    handler, contract_order = _handler_with_children(
        [OUR_CANCEL, IB_REJECT, OUR_CANCEL, IB_REJECT, OUR_CANCEL]
    )

    assert _preprocess(handler, contract_order) is contract_order


# --- the reject count survives a stack handler restart ----------------------
# After a restart ib_async rebuilds today's completed orders with an EMPTY
# trade log, so the comment the broker layer derives for a matched order no
# longer carries the reject; refreshing algo_comment from it must not wipe
# the tag (2026-10-05: three restarts would each have reset the count).


def _match_with_comment(broker_stack, order_id, comment):
    matched = _broker_order(comment)
    broker_stack.add_execution_details_from_matched_broker_order(order_id, matched)
    return broker_stack.get_order_with_id_from_stack(order_id)


def test_reject_tag_survives_refresh_from_an_empty_log_trade():
    broker_stack = memBrokerStack()
    order_id = broker_stack.put_order_on_stack(_broker_order(IB_REJECT))

    refreshed = _match_with_comment(broker_stack, order_id, "")

    assert refreshed.algo_comment.startswith(
        "IB reject 202: Order price is outside price limits"
    )


def test_reject_tag_is_not_doubled_when_the_broker_still_has_it():
    broker_stack = memBrokerStack()
    order_id = broker_stack.put_order_on_stack(_broker_order(IB_REJECT))

    refreshed = _match_with_comment(broker_stack, order_id, IB_REJECT)

    assert refreshed.algo_comment == IB_REJECT


def test_completion_note_survives_refresh():
    broker_stack = memBrokerStack()
    note = "Unfilled and gone from broker: completed with zero fill, original trade [1]"
    order_id = broker_stack.put_order_on_stack(
        _broker_order("IB reject 201: No Trading Permission | " + note)
    )

    refreshed = _match_with_comment(broker_stack, order_id, "TradeLogEntry(...)")

    assert refreshed.algo_comment.startswith("IB reject 201: No Trading Permission")
    assert refreshed.algo_comment.endswith(note)


def test_reject_count_survives_a_simulated_restart():
    handler, contract_order = _handler_with_children([IB_REJECT] * 3)
    # restart: every child is matched to an empty-log trade and refreshed
    for child_id in contract_order.children:
        _match_with_comment(handler._broker_stack, child_id, "")

    assert _preprocess(handler, contract_order) is missing_order
    handler._log.critical.assert_called_once()
