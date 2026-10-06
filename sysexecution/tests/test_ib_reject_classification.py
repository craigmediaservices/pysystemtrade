"""
Reject -> resubmit loop guard, part (a): when IB refused or killed an order
that never filled, the broker layer says so at the start of algo_comment
('IB reject <code>: <reason>'), and the normal fill path persists that on
the database broker order. Our own cancels (algo timeouts, end-of-day:
202 with an empty reason) and a refused modification of a working order
(the 2026-09-16 Eurex spread) are not rejects.
"""
import datetime

import pytest
from ib_async import (
    Future,
    LimitOrder,
    OrderStatus,
    Trade,
    TradeLogEntry,
)

from sysbrokers.IB.ib_contracts import ibcontractWithLegs
from sysbrokers.IB.ib_translate_broker_order_objects import (
    extract_trade_info,
    ib_error_reason,
    ib_reject_from_trade,
    tradeWithContract,
)
from sysexecution.orders.broker_orders import (
    brokerOrder,
    broker_order_was_rejected_by_broker,
)
from sysexecution.tests.test_cancel_unsubmitted_orders import memBrokerStack
from sysexecution.trade_qty import tradeQuantity

NOW = datetime.datetime(2026, 10, 2, 3, 33)

PRICE_LIMITS = (
    "Error 202, reqId 373298: Order Canceled - reason:"
    "Order price is outside price limits"
)
OUR_CANCEL = "Error 202, reqId 373298: Order Canceled - reason:"
NO_PERMISSION = (
    "Error 201, reqId 373298: Order rejected - reason:"
    "YOUR ACCOUNT IS NOT ELIGIBLE TO TRADE THIS PRODUCT"
)
NOT_ALLOWED = (
    "Error 203, reqId 373298: The security <FUT> is not available or "
    "allowed for this account."
)
DUPLICATE_ID = "Error 201, reqId 373298: Order rejected - reason:Duplicate ID"


def _ib_trade(log_entries, status="Cancelled", filled=0.0):
    log = [TradeLogEntry(NOW, "PendingSubmit", "", 0)]
    for message, code in log_entries:
        log.append(TradeLogEntry(NOW, status, message, code))
    return Trade(
        contract=Future(symbol="RTY", lastTradeDateOrContractMonth="20261218"),
        order=LimitOrder("BUY", 1, 2400.0, orderId=373298, clientId=138),
        orderStatus=OrderStatus(orderId=373298, status=status, filled=filled),
        fills=[],
        log=log,
    )


# --- (a) classifying how an order ended -------------------------------------


def test_reason_is_extracted_from_ib_messages():
    assert ib_error_reason(PRICE_LIMITS) == "Order price is outside price limits"
    assert ib_error_reason(OUR_CANCEL) == ""
    assert ib_error_reason(OUR_CANCEL + ", contract: Future(conId=1)") == ""
    assert ib_error_reason(NOT_ALLOWED).startswith("The security")


def test_ib_cancel_with_a_reason_is_a_reject():
    trade = _ib_trade([(PRICE_LIMITS, 202)])
    assert ib_reject_from_trade(trade) == (
        202,
        "Order price is outside price limits",
    )


@pytest.mark.parametrize("message,code", [(NO_PERMISSION, 201), (NOT_ALLOWED, 203)])
def test_new_order_refused_is_a_reject(message, code):
    reject = ib_reject_from_trade(_ib_trade([(message, code)], status="Inactive"))
    assert reject is not None
    assert reject[0] == code


def test_our_own_cancel_is_not_a_reject():
    # algo timeout / end-of-day: we cancel, IB answers 202 with no reason
    trade = _ib_trade([("", 0), (OUR_CANCEL, 202)])
    assert ib_reject_from_trade(trade) is None


def test_refused_modification_of_a_working_order_is_not_a_reject():
    # 2026-09-16 Eurex spread: modify refused with 201, ib_async wrote
    # 'Cancelled' locally while the exchange still worked the order
    trade = _ib_trade([("Modify", 0), (DUPLICATE_ID, 201)])
    assert ib_reject_from_trade(trade) is None


def test_working_or_filled_orders_are_never_rejects():
    assert ib_reject_from_trade(_ib_trade([(PRICE_LIMITS, 202)], "Submitted")) is None
    assert ib_reject_from_trade(_ib_trade([(PRICE_LIMITS, 202)], "Filled")) is None
    partly_filled = _ib_trade([(PRICE_LIMITS, 202)], filled=1.0)
    assert ib_reject_from_trade(partly_filled) is None


def test_reject_leads_the_algo_comment_and_the_log_is_kept():
    trade = _ib_trade([(PRICE_LIMITS, 202)])
    info = extract_trade_info(
        tradeWithContract(ibcontractWithLegs(trade.contract), trade)
    )
    assert info.algo_msg.startswith(
        "IB reject 202: Order price is outside price limits | "
    )
    assert "TradeLogEntry" in info.algo_msg


def test_algo_comment_unchanged_for_our_own_cancel():
    trade = _ib_trade([(OUR_CANCEL, 202)])
    info = extract_trade_info(
        tradeWithContract(ibcontractWithLegs(trade.contract), trade)
    )
    assert info.algo_msg == " ".join(str(entry) for entry in trade.log)


# --- (a) the reject is persisted on the database broker order ---------------


def _broker_order(algo_comment="", fill=0, parent=7084):
    order = brokerOrder("strat", "R1000", "20261200", 1, parent=parent)
    order.algo_comment = algo_comment
    order._fill = tradeQuantity(fill)
    return order


def test_reject_reaches_the_database_through_the_normal_fill_path():
    stack = memBrokerStack()
    order_id = stack.put_order_on_stack(_broker_order())
    from_ib = _broker_order("IB reject 202: Order price is outside price limits | x")

    stack.add_execution_details_from_matched_broker_order(order_id, from_ib)

    assert broker_order_was_rejected_by_broker(
        stack.get_order_with_id_from_stack(order_id)
    )


def test_only_unfilled_tagged_orders_count_as_rejected():
    assert not broker_order_was_rejected_by_broker(_broker_order(""))
    assert not broker_order_was_rejected_by_broker(_broker_order(OUR_CANCEL))
    assert not broker_order_was_rejected_by_broker(_broker_order(None))
    assert not broker_order_was_rejected_by_broker(
        _broker_order("IB reject 202: x", fill=1)
    )
    assert broker_order_was_rejected_by_broker(_broker_order("IB reject 201: x"))
