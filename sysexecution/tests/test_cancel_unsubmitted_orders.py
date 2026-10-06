"""
Retiring an unfilled, unsubmitted instrument order that a new order
supersedes, instead of netting an opposing order against it.

The stacks here are in-memory versions of the mongo ones (same as_dict /
from_dict round trip), so the real stack code - existing-order lookup,
residual netting, deactivation, algo claims - is what runs.
"""
from unittest import mock

import pytest

from sysexecution.orders.broker_orders import brokerOrder
from sysexecution.orders.contract_orders import contractOrder
from sysexecution.orders.instrument_orders import (
    instrumentOrder,
    instrumentOrderType,
    best_order_type,
)
from sysexecution.orders.named_order_objects import missing_order
from sysexecution.trade_qty import tradeQuantity
from sysexecution.order_stacks.broker_order_stack import brokerOrderStackData
from sysexecution.order_stacks.contract_order_stack import contractOrderStackData
from sysexecution.order_stacks.instrument_order_stack import (
    instrumentOrderStackData,
    zeroOrderException,
)
from sysexecution.stack_handler.completed_orders import stackHandlerForCompletions
from sysexecution.strategies.cancel_unsubmitted_orders import (
    CANCEL_REF,
    family_is_unsubmitted,
    unsubmittedOrderCanceller,
)
from sysexecution.strategies.strategy_order_handling import orderGeneratorForStrategy


# --- in-memory stacks -------------------------------------------------------


class _memStack(object):
    order_class = None

    def __init__(self):
        super().__init__()
        self._store = {}
        self._next = 1

    def _get_list_of_all_order_ids(self):
        return list(self._store.keys())

    def get_order_with_id_from_stack(self, order_id):
        as_dict = self._store.get(order_id)
        if as_dict is None:
            return missing_order
        return self.order_class.from_dict(dict(as_dict))

    def _put_order_on_stack_no_checking(self, order):
        self._store[order.order_id] = order.as_dict()

    def _change_order_on_stack_no_checking(self, order_id, order):
        self._store[order_id] = order.as_dict()

    def _remove_order_with_id_from_stack_no_checking(self, order_id):
        del self._store[order_id]

    def _get_next_order_id(self):
        order_id = self._next
        self._next += 1
        return order_id


class memInstrumentStack(_memStack, instrumentOrderStackData):
    order_class = instrumentOrder


class memContractStack(_memStack, contractOrderStackData):
    order_class = contractOrder

    def _claim_order_for_algo_if_unclaimed(
        self, order_id, control_algo_ref, allow_same_ref=False
    ):
        # same condition as the mongo filter; a dict in one thread is atomic
        stored = self._store.get(order_id)
        if stored is None or stored["locked"]:
            return False
        claimable = (None, control_algo_ref) if allow_same_ref else (None,)
        if stored["reference_of_controlling_algo"] not in claimable:
            return False
        stored["reference_of_controlling_algo"] = control_algo_ref
        return True


class memBrokerStack(_memStack, brokerOrderStackData):
    order_class = brokerOrder


class Stacks(object):
    def __init__(self):
        self.instrument = memInstrumentStack()
        self.contract = memContractStack()
        self.broker = memBrokerStack()
        self.archived = []

        completions = stackHandlerForCompletions.__new__(stackHandlerForCompletions)
        completions._instrument_stack = self.instrument
        completions._contract_stack = self.contract
        completions._broker_stack = self.broker
        completions._log = mock.MagicMock()
        completions._data = mock.MagicMock()
        completions.add_order_family_to_historic_orders_database = (
            lambda family: self.archived.append(family)
        )
        self.completions = completions

        canceller = unsubmittedOrderCanceller.__new__(unsubmittedOrderCanceller)
        canceller._completions = completions
        self.canceller = canceller

    # the state an order is in after the generator placed it and the stack
    # handler spawned its contract child, with the market shut
    def place_unsubmitted(self, trade, contract_date="20261200", with_child=True):
        order = instrumentOrder("strat", "INSTR", trade, order_type=best_order_type)
        order_id = self.instrument.put_order_on_stack(order)
        if with_child:
            child = contractOrder(
                "strat", "INSTR", contract_date, trade, parent=order_id
            )
            child_id = self.contract.put_order_on_stack(child)
            self.instrument.add_children_to_order_without_existing_children(
                order_id, [child_id]
            )
        return order_id

    def contract_child_of(self, order_id):
        order = self.instrument.get_order_with_id_from_stack(order_id)
        return self.contract.get_order_with_id_from_stack(order.children[0])

    def active_instrument_trades(self):
        return [
            o.trade.as_single_trade_qty_or_error()
            for o in self.instrument.get_list_of_orders()
        ]


def _wanted(trade):
    return instrumentOrder("strat", "INSTR", trade, order_type=best_order_type)


# --- the three shapes the bug took -------------------------------------------


def test_exact_reversal_retires_the_order_and_places_nothing():
    """sell 1 at 02:51, strategy wants no trade at 10:51: under the old
    behaviour the stack added buy 1 and both executed."""
    stacks = Stacks()
    order_id = stacks.place_unsubmitted(-1)

    retired = stacks.canceller.cancel_orders_superseded_by(_wanted(0))

    assert retired == [order_id]
    assert stacks.active_instrument_trades() == []
    assert stacks.contract.get_list_of_orders() == []
    assert len(stacks.archived) == 1
    assert stacks.archived[0].instrument_order_id == order_id
    # the stack now sees nothing to net against: a zero order is refused
    with pytest.raises(zeroOrderException):
        stacks.instrument.put_order_on_stack(_wanted(0))


def test_shrink_places_the_smaller_order_fresh():
    """sell 2, then strategy wants sell 1: old behaviour added buy 1."""
    stacks = Stacks()
    old_id = stacks.place_unsubmitted(-2)

    retired = stacks.canceller.cancel_orders_superseded_by(_wanted(-1))
    new_id = stacks.instrument.put_order_on_stack(_wanted(-1))

    assert retired == [old_id]
    assert new_id != old_id
    assert stacks.active_instrument_trades() == [-1]


def test_flip_places_the_full_new_trade():
    """sell 2, then strategy wants buy 1: old behaviour added buy 3."""
    stacks = Stacks()
    stacks.place_unsubmitted(-2)

    stacks.canceller.cancel_orders_superseded_by(_wanted(1))
    stacks.instrument.put_order_on_stack(_wanted(1))

    assert stacks.active_instrument_trades() == [1]


def test_same_direction_increase_also_goes_fresh():
    stacks = Stacks()
    stacks.place_unsubmitted(-1)

    stacks.canceller.cancel_orders_superseded_by(_wanted(-3))
    stacks.instrument.put_order_on_stack(_wanted(-3))

    assert stacks.active_instrument_trades() == [-3]


def test_retired_child_carries_the_marker_for_the_audit_trail():
    stacks = Stacks()
    order_id = stacks.place_unsubmitted(-1)
    child = stacks.contract_child_of(order_id)

    stacks.canceller.cancel_orders_superseded_by(_wanted(0))

    retired_child = stacks.contract.get_order_with_id_from_stack(child.order_id)
    assert not retired_child.active
    assert retired_child.reference_of_controlling_algo == CANCEL_REF


def test_order_without_children_yet_is_retired_too():
    stacks = Stacks()
    order_id = stacks.place_unsubmitted(-1, with_child=False)

    assert stacks.canceller.cancel_orders_superseded_by(_wanted(0)) == [order_id]
    assert stacks.active_instrument_trades() == []


# --- anything in flight is left to the residual logic ------------------------


def _residual_after(stacks, wanted):
    assert stacks.canceller.cancel_orders_superseded_by(_wanted(wanted)) == []
    stacks.instrument.put_order_on_stack(_wanted(wanted))
    return stacks.active_instrument_trades()


def test_broker_child_means_hands_off():
    stacks = Stacks()
    order_id = stacks.place_unsubmitted(-2)
    child = stacks.contract_child_of(order_id)
    broker = brokerOrder("strat", "INSTR", "20261200", -2, parent=child.order_id)
    broker_id = stacks.broker.put_order_on_stack(broker)
    stacks.contract.add_children_to_order_without_existing_children(
        child.order_id, [broker_id]
    )

    # old behaviour, unchanged: -2 stays, +1 residual is added
    assert sorted(_residual_after(stacks, -1)) == [-2, 1]


def test_broker_order_the_child_does_not_know_about_yet_means_hands_off():
    stacks = Stacks()
    order_id = stacks.place_unsubmitted(-2)
    child = stacks.contract_child_of(order_id)
    stacks.broker.put_order_on_stack(
        brokerOrder("strat", "INSTR", "20261200", -2, parent=child.order_id)
    )

    assert sorted(_residual_after(stacks, -1)) == [-2, 1]


def test_partial_fill_means_hands_off():
    stacks = Stacks()
    order_id = stacks.place_unsubmitted(-2)
    stacks.instrument.change_fill_quantity_for_order(order_id, tradeQuantity(-1))

    assert stacks.canceller.cancel_orders_superseded_by(_wanted(-1)) == []
    assert stacks.instrument.get_order_with_id_from_stack(order_id).active


def test_algo_already_working_the_child_means_hands_off():
    stacks = Stacks()
    order_id = stacks.place_unsubmitted(-2)
    child = stacks.contract_child_of(order_id)
    stacks.contract.add_controlling_algo_ref(child.order_id, "algo_original_best")

    assert sorted(_residual_after(stacks, -1)) == [-2, 1]
    still = stacks.contract.get_order_with_id_from_stack(child.order_id)
    assert still.reference_of_controlling_algo == "algo_original_best"


def test_algo_grabbing_the_child_between_read_and_claim_falls_back():
    stacks = Stacks()
    order_id = stacks.place_unsubmitted(-2)
    child = stacks.contract_child_of(order_id)
    real_add = stacks.contract.add_controlling_algo_ref

    def handler_gets_there_first(contract_order_id, ref, **kwargs):
        real_add(contract_order_id, "algo_original_best")
        return real_add(contract_order_id, ref, **kwargs)  # raises: controlled

    with mock.patch.object(
        stacks.contract,
        "add_controlling_algo_ref",
        side_effect=handler_gets_there_first,
    ):
        assert stacks.canceller.cancel_orders_superseded_by(_wanted(-1)) == []

    assert stacks.instrument.get_order_with_id_from_stack(order_id).active
    assert (
        stacks.contract.get_order_with_id_from_stack(
            child.order_id
        ).reference_of_controlling_algo
        == "algo_original_best"
    )


def test_manual_limit_order_under_the_strategy_name_is_left_alone():
    stacks = Stacks()
    manual = instrumentOrder(
        "strat", "INSTR", -2, order_type=instrumentOrderType("limit"), limit_price=100.0
    )
    stacks.instrument.put_order_on_stack(manual)

    assert sorted(_residual_after(stacks, -1)) == [-2, 1]


def test_other_instruments_are_untouched():
    stacks = Stacks()
    stacks.place_unsubmitted(-1)
    other = instrumentOrder("strat", "OTHER", 5, order_type=best_order_type)
    stacks.instrument.put_order_on_stack(other)

    stacks.canceller.cancel_orders_superseded_by(_wanted(0))

    assert [o.instrument_code for o in stacks.instrument.get_list_of_orders()] == [
        "OTHER"
    ]


# --- the pure predicate -------------------------------------------------------


def test_predicate_rejects_missing_child():
    order = instrumentOrder(
        "strat", "INSTR", -1, order_type=best_order_type, children=[7]
    )
    assert not family_is_unsubmitted(order, [missing_order], [], _wanted(0))


def test_predicate_accepts_own_marker_from_an_earlier_failed_attempt():
    order = instrumentOrder(
        "strat", "INSTR", -1, order_type=best_order_type, children=[7]
    )
    child = contractOrder("strat", "INSTR", "20261200", -1, parent=1, order_id=7)
    child.add_controlling_algo_ref(CANCEL_REF)
    assert family_is_unsubmitted(order, [child], [], _wanted(0))


# --- the generator hook -------------------------------------------------------


def _generator(stacks, canceller):
    gen = orderGeneratorForStrategy.__new__(orderGeneratorForStrategy)
    gen._log = mock.MagicMock()
    gen._data_orders = mock.MagicMock(db_instrument_stack_data=stacks.instrument)
    gen._unsubmitted_order_canceller = canceller
    gen.needs_force_warning = lambda order: False
    return gen


def test_generator_retires_then_places():
    stacks = Stacks()
    stacks.place_unsubmitted(-2)
    gen = _generator(stacks, stacks.canceller)

    gen.submit_order(_wanted(-1))

    assert stacks.active_instrument_trades() == [-1]


def test_generator_does_not_place_when_retiring_fails_part_way():
    """A half-retired family would be netted wrongly; skip the instrument
    this run and let the next run (or the 22:00 clean-up) sort it out."""
    stacks = Stacks()
    stacks.place_unsubmitted(-2)
    broken = mock.MagicMock()
    broken.cancel_orders_superseded_by.side_effect = Exception("mongo went away")
    gen = _generator(stacks, broken)

    gen.submit_order(_wanted(-1))

    assert stacks.active_instrument_trades() == [-2]
    gen._log.critical.assert_called_once()
