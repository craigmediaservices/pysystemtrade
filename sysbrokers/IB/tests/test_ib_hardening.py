"""
IB client hardening (2026-09-16): a request timeout so an unanswered request
raises instead of hanging the process, open-order checks that assume 'still
open' when IB does not answer, and a per-symbol contract-chain cache that
refetches once on a miss.
"""
import asyncio
from types import SimpleNamespace
from unittest import mock

import pytest

from syscore.cache import Cache
from syscore.exceptions import missingContract
from sysbrokers.IB import ib_connection
from sysbrokers.IB.client.ib_contracts_client import ibContractsClient
from sysbrokers.IB.ib_orders import (
    ibExecutionStackData,
    contract_for_instrument_lookup,
)


class _Config:
    def __init__(self, value=None):
        self.value = value

    def get_element_or_default(self, key, default):
        return default if self.value is None else self.value


def test_request_timeout_defaults_when_unconfigured():
    with mock.patch.object(
        ib_connection, "get_production_config", return_value=_Config()
    ):
        assert (
            ib_connection.get_ib_request_timeout_seconds()
            == ib_connection.DEFAULT_IB_REQUEST_TIMEOUT_SECONDS
        )


def test_request_timeout_reads_private_config():
    with mock.patch.object(
        ib_connection, "get_production_config", return_value=_Config(value=30)
    ):
        assert ib_connection.get_ib_request_timeout_seconds() == 30.0


def test_request_timeout_typo_is_loud():
    with mock.patch.object(
        ib_connection, "get_production_config", return_value=_Config(value="lots")
    ):
        with pytest.raises(ValueError):
            ib_connection.get_ib_request_timeout_seconds()


# --- contract chain cache -------------------------------------------------------


def _contracts(*con_ids):
    return [SimpleNamespace(conId=c) for c in con_ids]


def _client(chains):
    client = object.__new__(ibContractsClient)
    client._cache = Cache(client)
    client.ib_get_contract_chain = mock.MagicMock(side_effect=chains)
    return client


def test_contract_chain_is_fetched_once_per_symbol():
    client = _client([_contracts(1, 2)])
    for _ in range(26):
        assert client.ib_get_contract_with_conId("M1MS", 2).conId == 2
    assert client.ib_get_contract_chain.call_count == 1


def test_contract_chain_cache_is_per_symbol():
    client = _client([_contracts(1), _contracts(9)])
    client.ib_get_contract_with_conId("M1MS", 1)
    client.ib_get_contract_with_conId("FESB", 9)
    assert client.ib_get_contract_chain.call_count == 2


def test_missing_conid_refetches_once_then_finds_it():
    client = _client([_contracts(1, 2), _contracts(1, 2, 3)])
    assert client.ib_get_contract_with_conId("M1MS", 3).conId == 3
    assert client.ib_get_contract_chain.call_count == 2


def test_missing_conid_after_refetch_raises():
    client = _client([_contracts(1, 2), _contracts(1, 2)])
    with pytest.raises(missingContract):
        client.ib_get_contract_with_conId("M1MS", 7)
    assert client.ib_get_contract_chain.call_count == 2


# --- open-order check under timeout ----------------------------------------------


def _exec_stack(open_keys):
    stack = object.__new__(ibExecutionStackData)
    stack.get_open_order_keys_from_broker = mock.MagicMock(return_value=open_keys)
    return stack


def test_answered_request_is_matched_normally():
    stack = _exec_stack(open_keys={("perm", 1)})
    assert stack._any_key_open_at_broker({("perm", 1)})
    assert not stack._any_key_open_at_broker({("perm", 2)})


def test_timeout_from_ib_is_fatal_and_logged_critical():
    # a stalled reply can poison the next request, so the process must die
    # visibly rather than carry on with an unreliable connection
    stack = object.__new__(ibExecutionStackData)
    stack.log = mock.MagicMock()
    ib = mock.MagicMock()
    ib.reqAllOpenOrders.side_effect = asyncio.TimeoutError()
    stack._ib_client = SimpleNamespace(ib=ib)
    with pytest.raises(asyncio.TimeoutError):
        stack.get_open_order_keys_from_broker()
    stack.log.critical.assert_called_once()


# --- combo orders identify their instrument from a leg ----------------------------


def test_combo_uses_first_leg_for_instrument_lookup():
    leg = SimpleNamespace(secType="FUT", symbol="M1MS", conId=655438056)
    combo = SimpleNamespace(
        ibcontract=SimpleNamespace(secType="BAG", symbol="M1MS"), legs=[leg]
    )
    assert contract_for_instrument_lookup(combo) is leg


def test_outright_contract_is_used_directly():
    fut = SimpleNamespace(secType="FUT", symbol="FESB")
    assert (
        contract_for_instrument_lookup(SimpleNamespace(ibcontract=fut, legs=[])) is fut
    )


def test_combo_without_resolved_legs_falls_back_to_itself():
    bag = SimpleNamespace(secType="BAG", symbol="M1MS")
    assert (
        contract_for_instrument_lookup(SimpleNamespace(ibcontract=bag, legs=None))
        is bag
    )


# --- instrument code per conId is looked up once (2026-09-21) ---------------------


def _instrument_client(codes):
    from sysbrokers.IB.client.ib_client import ibClient

    client = object.__new__(ibClient)
    client._get_instrument_code_from_broker_contract_object_uncached = mock.MagicMock(
        side_effect=codes
    )
    return client


def test_instrument_code_is_fetched_once_per_con_id():
    client = _instrument_client(["V2X"])
    contract = SimpleNamespace(conId=555, symbol="V2TX")
    for _ in range(107):
        assert client.get_instrument_code_from_broker_contract_object(contract) == "V2X"
    assert (
        client._get_instrument_code_from_broker_contract_object_uncached.call_count == 1
    )


def test_instrument_code_cache_is_per_con_id():
    client = _instrument_client(["V2X", "FESB"])
    assert (
        client.get_instrument_code_from_broker_contract_object(SimpleNamespace(conId=1))
        == "V2X"
    )
    assert (
        client.get_instrument_code_from_broker_contract_object(SimpleNamespace(conId=2))
        == "FESB"
    )


def test_contract_without_con_id_is_never_cached():
    client = _instrument_client(["A", "B"])
    pattern = SimpleNamespace(conId=0)
    assert client.get_instrument_code_from_broker_contract_object(pattern) == "A"
    assert client.get_instrument_code_from_broker_contract_object(pattern) == "B"


def test_failed_lookup_is_not_cached():
    client = _instrument_client([missingContract(), "V2X"])
    contract = SimpleNamespace(conId=9)
    with pytest.raises(missingContract):
        client.get_instrument_code_from_broker_contract_object(contract)
    assert client.get_instrument_code_from_broker_contract_object(contract) == "V2X"


# --- position-break check survives a timeout ------------------------------------


def _checks(side_effect):
    from sysexecution.stack_handler.checks import stackHandlerChecks
    from sysexecution.stack_handler import checks as checks_module

    handler = object.__new__(stackHandlerChecks)
    handler._log = mock.MagicMock()
    handler._data = SimpleNamespace()
    handler.log_and_lock_new_breaks = mock.MagicMock()
    handler.clear_position_locks_where_breaks_fixed = mock.MagicMock()
    broker = mock.MagicMock()
    broker.get_list_of_breaks_between_broker_and_db_contract_positions.side_effect = (
        side_effect
    )
    patch = mock.patch.object(checks_module, "dataBroker", return_value=broker)
    return handler, patch


def test_position_break_check_skips_the_pass_on_timeout():
    handler, patch = _checks(asyncio.TimeoutError())
    with patch:
        handler.check_external_position_break()
    handler.log_and_lock_new_breaks.assert_not_called()
    handler.clear_position_locks_where_breaks_fixed.assert_not_called()
    handler.log.warning.assert_called_once()


def test_position_break_check_still_locks_when_ib_answers():
    handler, patch = _checks([["BREAK"]])
    with patch:
        handler.check_external_position_break()
    handler.log_and_lock_new_breaks.assert_called_once_with(["BREAK"])
    handler.clear_position_locks_where_breaks_fixed.assert_called_once_with(["BREAK"])


def test_position_break_check_does_not_swallow_other_errors():
    handler, patch = _checks(ValueError("boom"))
    with patch, pytest.raises(ValueError):
        handler.check_external_position_break()
