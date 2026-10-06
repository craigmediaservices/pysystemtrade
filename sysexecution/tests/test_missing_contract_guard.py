"""Regression: an IB contract lookup failure must not kill run_stack_handler.

2026-10-06 03:32: during an ISP outage IB returned Error 200 ("No security
definition"); missingContract propagated out of the ticker / market-conditions
lookups (only missingData was caught) and the stack handler died.
"""

from unittest.mock import MagicMock, patch

import pytest

from syscore.exceptions import missingContract, missingData
from sysexecution.orders.contract_orders import best_order_type, contractOrder
from sysexecution.orders.named_order_objects import missing_order


def _contract_order():
    return contractOrder(
        "dynamic_system",
        "BUND",
        "20261200",
        [-1],
        order_type=best_order_type,
        algo_to_use="sysexecution.algos.algo_original_best.algoOriginalBest",
    )


@pytest.mark.parametrize("exc", [missingContract, missingData])
def test_original_best_returns_missing_order_when_ticker_lookup_fails(exc):
    from sysexecution.algos.algo_original_best import algoOriginalBest

    with patch("sysexecution.algos.algo.dataBroker") as data_broker_cls:
        broker = MagicMock()
        broker.get_ticker_object_for_order.side_effect = exc()
        data_broker_cls.return_value = broker
        algo = algoOriginalBest(MagicMock(), _contract_order())
        assert algo.prepare_and_submit_trade() is missing_order


@pytest.mark.parametrize("exc", [missingContract, missingData])
def test_market_size_is_zero_when_market_conditions_lookup_fails(exc):
    from sysproduction.data.broker import dataBroker

    broker = dataBroker.__new__(dataBroker)
    broker._log = MagicMock()
    with patch.object(
        dataBroker,
        "get_market_conditions_for_contract_order_by_leg",
        side_effect=exc(),
    ), patch.object(dataBroker, "log", new=MagicMock(), create=True):
        side, offside = broker.get_current_size_for_contract_order_by_leg(
            _contract_order()
        )
    assert side == [0] and offside == [0]
