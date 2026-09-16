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
from sysbrokers.IB.ib_orders import ibExecutionStackData


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


def test_unanswered_open_order_request_means_assume_open():
    stack = _exec_stack(open_keys=None)
    assert stack._any_key_open_at_broker({("perm", 1)})


def test_answered_request_is_matched_normally():
    stack = _exec_stack(open_keys={("perm", 1)})
    assert stack._any_key_open_at_broker({("perm", 1)})
    assert not stack._any_key_open_at_broker({("perm", 2)})


def test_timeout_from_ib_returns_none_not_exception():
    stack = object.__new__(ibExecutionStackData)
    stack.log = mock.MagicMock()
    ib = mock.MagicMock()
    ib.reqAllOpenOrders.side_effect = asyncio.TimeoutError()
    stack._ib_client = SimpleNamespace(ib=ib)
    assert stack.get_open_order_keys_from_broker() is None
    stack.log.warning.assert_called_once()
