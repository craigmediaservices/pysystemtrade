"""
Claiming a contract order for an algo is a compare-and-set in the store.

The order generator's canceller (cancel_unsubmitted_orders) and the stack
handler (add_controlling_algo_to_order) can both claim the same contract
order within milliseconds. The old claim read the order, checked it was
uncontrolled, then overwrote the whole document: if the other process
claimed between the read and the write, both claims 'succeeded' and both
went on to act on the order (a possible double trade).

The race is reproduced deterministically by handing the claimer a stale
read: the order as it was before the other process claimed it.
"""
import copy
from types import SimpleNamespace
from unittest import mock

import pytest

from sysdata.mongodb.mongo_generic import mongoDataWithSingleKey
from sysdata.mongodb.mongo_order_stack import mongoContractOrderStackData
from sysexecution.orders.contract_orders import contractOrder
from sysexecution.strategies.cancel_unsubmitted_orders import CANCEL_REF
from sysexecution.tests.test_cancel_unsubmitted_orders import Stacks

HANDLER_REF = "sysexecution.algos.algo_original_best.algoOriginalBest"


def _claim_with_stale_read(contract_stack, order_id, winner_ref, loser_ref):
    stale = contract_stack.get_order_with_id_from_stack(order_id)
    # the other process gets there first
    contract_stack.add_controlling_algo_ref(order_id, winner_ref)
    # ... while we are still working from what we read before it did
    with mock.patch.object(
        contract_stack, "get_order_with_id_from_stack", return_value=stale
    ):
        contract_stack.add_controlling_algo_ref(order_id, loser_ref)


# --- in-memory stack (same code path as production above the store) ---------


def _child_id(stacks):
    order_id = stacks.place_unsubmitted(-2)
    return stacks.contract_child_of(order_id).order_id


def test_canceller_losing_the_race_raises_and_handler_keeps_the_order():
    stacks = Stacks()
    child_id = _child_id(stacks)

    with pytest.raises(Exception, match="Already controlled"):
        _claim_with_stale_read(stacks.contract, child_id, HANDLER_REF, CANCEL_REF)

    stored = stacks.contract.get_order_with_id_from_stack(child_id)
    assert stored.reference_of_controlling_algo == HANDLER_REF


def test_handler_losing_the_race_raises_and_canceller_keeps_the_order():
    stacks = Stacks()
    child_id = _child_id(stacks)

    with pytest.raises(Exception, match="Already controlled"):
        _claim_with_stale_read(stacks.contract, child_id, CANCEL_REF, HANDLER_REF)

    stored = stacks.contract.get_order_with_id_from_stack(child_id)
    assert stored.reference_of_controlling_algo == CANCEL_REF


def test_unclaimed_order_is_claimed():
    stacks = Stacks()
    child_id = _child_id(stacks)

    stacks.contract.add_controlling_algo_ref(child_id, HANDLER_REF)

    stored = stacks.contract.get_order_with_id_from_stack(child_id)
    assert stored.reference_of_controlling_algo == HANDLER_REF


def test_claiming_again_with_the_same_ref_is_still_fine():
    # unchanged behaviour: the canceller re-claims its own marker after an
    # earlier failed attempt
    stacks = Stacks()
    child_id = _child_id(stacks)

    stacks.contract.add_controlling_algo_ref(child_id, CANCEL_REF)
    stacks.contract.add_controlling_algo_ref(child_id, CANCEL_REF)

    stored = stacks.contract.get_order_with_id_from_stack(child_id)
    assert stored.reference_of_controlling_algo == CANCEL_REF


def test_already_controlled_order_still_raises():
    stacks = Stacks()
    child_id = _child_id(stacks)
    stacks.contract.add_controlling_algo_ref(child_id, HANDLER_REF)

    with pytest.raises(Exception, match="Already controlled"):
        stacks.contract.add_controlling_algo_ref(child_id, CANCEL_REF)


def test_locked_order_cannot_be_claimed():
    stacks = Stacks()
    child_id = _child_id(stacks)
    stacks.contract.lock_order_on_stack(child_id)

    with pytest.raises(Exception):
        stacks.contract.add_controlling_algo_ref(child_id, HANDLER_REF)

    stored = stacks.contract.get_order_with_id_from_stack(child_id)
    assert stored.reference_of_controlling_algo is None


def test_release_then_claim_works():
    stacks = Stacks()
    child_id = _child_id(stacks)
    stacks.contract.add_controlling_algo_ref(child_id, HANDLER_REF)
    stacks.contract.release_order_from_algo_control(child_id)

    stacks.contract.add_controlling_algo_ref(child_id, CANCEL_REF)

    stored = stacks.contract.get_order_with_id_from_stack(child_id)
    assert stored.reference_of_controlling_algo == CANCEL_REF


# --- mongo implementation: the condition is in the update filter ------------


class _FakeCollection:
    """
    Just enough of a pymongo collection for one document keyed on order_id:
    equality, $in and $ne in filters, $set in updates.
    """

    def __init__(self):
        self.docs = []
        self.update_filters = []

    @staticmethod
    def _matches(doc, query):
        for field, condition in query.items():
            value = doc.get(field)
            if isinstance(condition, dict) and "$in" in condition:
                if value not in condition["$in"]:
                    return False
            elif isinstance(condition, dict) and "$ne" in condition:
                if value == condition["$ne"]:
                    return False
            elif value != condition:
                return False
        return True

    def find_one(self, query):
        for doc in self.docs:
            if self._matches(doc, query):
                found = copy.deepcopy(doc)
                found["_id"] = "fake"
                return found
        return None

    def update_one(self, query, update):
        self.update_filters.append(query)
        for doc in self.docs:
            if self._matches(doc, query):
                doc.update(update["$set"])
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)


def _mongo_contract_stack_with(order: contractOrder):
    collection = _FakeCollection()
    doc = order.as_dict()
    collection.docs.append(doc)

    mongo_data = object.__new__(mongoDataWithSingleKey)
    mongo_data._mongo = SimpleNamespace(collection=collection)
    mongo_data._key_name = "order_id"

    stack = object.__new__(mongoContractOrderStackData)
    stack._mongo_data = mongo_data
    stack.log = mock.MagicMock()

    return stack, collection


def _contract_order(order_id=42):
    return contractOrder("strat", "INSTR", "20261200", -2, order_id=order_id)


def test_mongo_claim_race_lost_raises_and_keeps_the_winner():
    stack, collection = _mongo_contract_stack_with(_contract_order())

    with pytest.raises(Exception, match="Already controlled"):
        _claim_with_stale_read(stack, 42, HANDLER_REF, CANCEL_REF)

    assert collection.docs[0]["reference_of_controlling_algo"] == HANDLER_REF


def test_mongo_claim_sends_a_conditional_update_of_one_field():
    stack, collection = _mongo_contract_stack_with(_contract_order())

    stack.add_controlling_algo_ref(42, CANCEL_REF)

    assert collection.update_filters[-1] == {
        "order_id": 42,
        "reference_of_controlling_algo": {"$in": [None, CANCEL_REF]},
        "locked": {"$ne": True},
    }
    assert collection.docs[0]["reference_of_controlling_algo"] == CANCEL_REF
