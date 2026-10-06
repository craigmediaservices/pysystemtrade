"""
Zero-fill broker orders that IB no longer knows about (2026-10-02..05: 36
R1000 orders after a missed end-of-day clean-up) stayed active forever: the
fills pass re-queried IB for each on every pass (~486k "does not match any
broker orders: can't fill" warnings) and their parents could never complete.

Now, once such an order is CONFIRMED gone - not in a fresh open-order list,
no execution reported for it, nothing filled, and no algo controlling its
contract order (an algo applies its own fills before releasing control) -
it is marked complete with zero fill, with one warning.
"""
from unittest import mock

from sysexecution.orders.broker_orders import brokerOrder
from sysexecution.orders.contract_orders import contractOrder
from sysexecution.orders.named_order_objects import missing_order
from sysexecution.orders.instrument_orders import instrumentOrder, best_order_type
from sysexecution.stack_handler.fills import stackHandlerForFills
from sysexecution.tests.test_cancel_unsubmitted_orders import Stacks
from sysexecution.trade_qty import tradeQuantity

TEMPID = "U5570413/138/373298"
PERMID = 1246379236
ALGO_REF = "sysexecution.algos.algo_original_best.algoOriginalBest"


class _FakeDataBroker:
    """IB has never heard of the order in this session (no match)."""

    def __init__(self, gone=True):
        self.gone = gone
        self.asked_gone = []

    def match_db_broker_order_to_order_from_brokers(self, broker_order):
        return missing_order

    def check_unfilled_order_is_gone_from_broker(self, broker_order):
        self.asked_gone.append(broker_order.order_id)
        return self.gone


def _fills_handler(stacks):
    handler = stackHandlerForFills.__new__(stackHandlerForFills)
    handler._instrument_stack = stacks.instrument
    handler._contract_stack = stacks.contract
    handler._broker_stack = stacks.broker
    handler._log = mock.MagicMock()
    handler._data = mock.MagicMock()
    return handler


def _family(stacks, broker_fills, contract_fill=0):
    """instrument order -> contract order -> one broker order per fill"""
    trade = 1
    instrument_id = stacks.instrument.put_order_on_stack(
        instrumentOrder("strat", "R1000", trade, order_type=best_order_type)
    )
    contract_id = stacks.contract.put_order_on_stack(
        contractOrder("strat", "R1000", "20261200", trade, parent=instrument_id)
    )
    stacks.instrument.add_children_to_order_without_existing_children(
        instrument_id, [contract_id]
    )
    broker_ids = []
    for fill in broker_fills:
        order = brokerOrder(
            "strat",
            "R1000",
            "20261200",
            trade,
            parent=contract_id,
            broker_tempid=TEMPID,
            broker_permid=PERMID,
        )
        order._fill = tradeQuantity(fill)
        broker_ids.append(stacks.broker.put_order_on_stack(order))
    stacks.contract.add_children_to_order_without_existing_children(
        contract_id, broker_ids
    )
    if contract_fill:
        stacks.contract.change_fill_quantity_for_order(
            contract_id, tradeQuantity(contract_fill)
        )
        stacks.instrument.change_fill_quantity_for_order(
            instrument_id, tradeQuantity(contract_fill)
        )
    return instrument_id, contract_id, broker_ids


def _fills_pass(handler, data_broker, broker_order_id):
    with mock.patch(
        "sysexecution.stack_handler.fills.dataBroker", return_value=data_broker
    ):
        handler.apply_broker_fill_from_broker_to_broker_database(broker_order_id)


def _no_match_warnings(handler):
    return [
        call
        for call in handler._log.warning.call_args_list
        if "does not match any broker orders" in str(call)
    ]


def test_confirmed_gone_unfilled_order_is_completed_and_warned_once():
    stacks = Stacks()
    _, _, (broker_id,) = _family(stacks, [0])
    handler = _fills_handler(stacks)
    data_broker = _FakeDataBroker(gone=True)

    for _ in range(3):
        _fills_pass(handler, data_broker, broker_id)

    order = stacks.broker.get_order_with_id_from_stack(broker_id)
    assert order.fill_equals_desired_trade()
    assert order.fill_equals_zero()
    assert stacks.broker.is_completed(broker_id)
    # the archive / reports still see what was originally asked for
    assert order.algo_comment.endswith("original trade [1]")
    # checked once, then left alone: no more IB queries, no warning spam
    assert data_broker.asked_gone == [broker_id]
    handler._log.warning.assert_called_once()
    assert _no_match_warnings(handler) == []


def test_the_family_can_then_complete():
    # contract order filled by a later broker order; the earlier one was
    # cancelled unfilled and IB has forgotten it (e.g. after a restart)
    stacks = Stacks()
    instrument_id, _, (gone_id, filled_id) = _family(stacks, [0, 1], contract_fill=1)
    handler = _fills_handler(stacks)

    stacks.completions.handle_completed_orders()
    assert stacks.archived == []

    _fills_pass(handler, _FakeDataBroker(gone=True), gone_id)
    stacks.completions.handle_completed_orders()

    assert [family.instrument_order_id for family in stacks.archived] == [instrument_id]


def test_order_not_confirmed_gone_is_left_alone():
    stacks = Stacks()
    _, _, (broker_id,) = _family(stacks, [0])
    handler = _fills_handler(stacks)

    _fills_pass(handler, _FakeDataBroker(gone=False), broker_id)

    assert not stacks.broker.is_completed(broker_id)
    assert len(_no_match_warnings(handler)) == 1


def test_order_whose_contract_order_an_algo_controls_is_left_alone():
    # the algo (possibly in another process) applies its own fills before
    # releasing control: do not pre-empt it
    stacks = Stacks()
    _, contract_id, (broker_id,) = _family(stacks, [0])
    stacks.contract.add_controlling_algo_ref(contract_id, ALGO_REF)
    handler = _fills_handler(stacks)
    data_broker = _FakeDataBroker(gone=True)

    _fills_pass(handler, data_broker, broker_id)

    assert not stacks.broker.is_completed(broker_id)
    assert data_broker.asked_gone == []
    assert len(_no_match_warnings(handler)) == 1


def test_partially_filled_order_is_left_alone():
    stacks = Stacks()
    instrument_id = stacks.instrument.put_order_on_stack(
        instrumentOrder("strat", "R1000", 2, order_type=best_order_type)
    )
    contract_id = stacks.contract.put_order_on_stack(
        contractOrder("strat", "R1000", "20261200", 2, parent=instrument_id)
    )
    order = brokerOrder(
        "strat", "R1000", "20261200", 2, parent=contract_id, broker_tempid=TEMPID
    )
    order._fill = tradeQuantity(1)
    broker_id = stacks.broker.put_order_on_stack(order)
    handler = _fills_handler(stacks)
    data_broker = _FakeDataBroker(gone=True)

    _fills_pass(handler, data_broker, broker_id)

    assert not stacks.broker.is_completed(broker_id)
    assert data_broker.asked_gone == []


def test_broker_reject_tag_survives_completion():
    # Fix 1 counts rejects by the START of algo_comment; completing the
    # order must not hide it
    stacks = Stacks()
    _, _, (broker_id,) = _family(stacks, [0])
    order = stacks.broker.get_order_with_id_from_stack(broker_id)
    order.algo_comment = "IB reject 201: No Trading Permission | log"
    stacks.broker._change_order_on_stack(broker_id, order)
    handler = _fills_handler(stacks)

    _fills_pass(handler, _FakeDataBroker(gone=True), broker_id)

    done = stacks.broker.get_order_with_id_from_stack(broker_id)
    assert done.algo_comment.startswith("IB reject 201: No Trading Permission | ")
    assert done.algo_comment.endswith("original trade [1]")
