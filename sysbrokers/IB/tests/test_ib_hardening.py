"""
IB client hardening (2026-09-16): a request timeout so an unanswered request
raises instead of hanging the process, and a per-symbol contract-chain cache
so combo-leg resolution does not hammer reqContractDetails.
"""
import datetime
from unittest import mock

from sysbrokers.IB import ib_connection
from sysbrokers.IB.client.ib_contracts_client import (
    ibContractsClient,
    CONTRACT_CHAIN_CACHE_SECONDS,
)


class _Config:
    def __init__(self, value=None, raise_on_read=False):
        self.value = value
        self.raise_on_read = raise_on_read

    def get_element_or_default(self, key, default):
        if self.raise_on_read:
            raise RuntimeError("no config")
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


def test_request_timeout_survives_a_broken_config():
    with mock.patch.object(
        ib_connection,
        "get_production_config",
        return_value=_Config(raise_on_read=True),
    ):
        assert (
            ib_connection.get_ib_request_timeout_seconds()
            == ib_connection.DEFAULT_IB_REQUEST_TIMEOUT_SECONDS
        )


def _client_with_fake_chain():
    client = object.__new__(ibContractsClient)
    client.ib_get_contract_chain = mock.MagicMock(return_value=["c1", "c2"])
    return client


def test_contract_chain_is_fetched_once_per_symbol():
    client = _client_with_fake_chain()
    for _ in range(26):
        assert client._get_contract_chain_for_symbol("M1MS") == ["c1", "c2"]
    assert client.ib_get_contract_chain.call_count == 1


def test_contract_chain_cache_is_per_symbol():
    client = _client_with_fake_chain()
    client._get_contract_chain_for_symbol("M1MS")
    client._get_contract_chain_for_symbol("FESB")
    assert client.ib_get_contract_chain.call_count == 2


def test_contract_chain_cache_expires():
    client = _client_with_fake_chain()
    client._get_contract_chain_for_symbol("M1MS")
    stale = datetime.datetime.now() - datetime.timedelta(
        seconds=CONTRACT_CHAIN_CACHE_SECONDS + 1
    )
    client._contract_chain_cache["M1MS"] = (stale, ["old"])
    assert client._get_contract_chain_for_symbol("M1MS") == ["c1", "c2"]
    assert client.ib_get_contract_chain.call_count == 2
