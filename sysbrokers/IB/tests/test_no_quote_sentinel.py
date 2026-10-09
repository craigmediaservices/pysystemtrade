"""
IB's 'no quote' sentinel (2026-10-09): IB sends bid/ask -1 with size 0 when
there is no quote (no subscription, delayed data with nothing to show). It was
passed through as a price: 14 KOSPI_mini broker orders went out with limit -1.0
on 2026-09-16, and the spread sampler stored zero spreads for markets with no
quote. Both now see NaN, which they already treat as 'no market data'.
"""
from types import SimpleNamespace

import numpy as np
import pytest

from syscore.exceptions import missingData
from sysbrokers.IB.client.ib_price_client import tickerWithBS
from sysbrokers.IB.ib_futures_contract_price_data import (
    ibTickerObject,
    price_or_nan_if_no_quote,
)
from sysexecution.tick_data import get_df_of_ticks_from_ticker_object


class _Client:
    def refresh(self):
        pass


def _ticker_object(bid, bid_size, ask, ask_size, BorS="BUY"):
    ticker = SimpleNamespace(bid=bid, bidSize=bid_size, ask=ask, askSize=ask_size)
    return ibTickerObject(tickerWithBS(ticker, BorS), _Client())


@pytest.mark.parametrize("size", [0, 0.0, np.nan, None])
def test_minus_one_without_size_is_no_quote(size):
    assert np.isnan(price_or_nan_if_no_quote(-1.0, size))


def test_minus_one_with_size_is_a_real_price():
    # e.g. a calendar spread genuinely quoted at -1
    assert price_or_nan_if_no_quote(-1.0, 5) == -1.0


@pytest.mark.parametrize("price", [-0.65, -12.25, 0.0123, 7844.25])
def test_other_prices_pass_through(price):
    # negative spread prices (V2X, ETHEREUM rolls) must survive
    assert price_or_nan_if_no_quote(price, 0) == price


def test_none_price_is_nan():
    assert np.isnan(price_or_nan_if_no_quote(None, 0))


def test_ticker_object_hides_no_quote():
    ticker_object = _ticker_object(-1.0, 0.0, -1.0, 0.0)
    assert np.isnan(ticker_object.bid())
    assert np.isnan(ticker_object.ask())


def test_ticker_object_keeps_live_quote():
    ticker_object = _ticker_object(7834.75, 40.0, 7835.0, 31.0)
    assert ticker_object.bid() == 7834.75
    assert ticker_object.ask() == 7835.0


def test_execution_wait_raises_instead_of_returning_minus_one():
    # algo.get_market_data_for_order_modifies_ticker_object catches missingData
    # and does not trade; before the fix this returned a -1/-1 tick
    ticker_object = _ticker_object(-1.0, 0.0, -1.0, 0.0, BorS="SELL")
    with pytest.raises(missingData):
        ticker_object.wait_for_valid_bid_and_ask_and_return_current_tick(
            wait_time_seconds=0.05
        )


def test_spread_sampling_stores_nothing_instead_of_zero():
    # additional_sampling.refresh_sampling_without_checks catches missingData
    # and skips the write; before the fix this averaged to a 0.0 spread
    ticker_object = _ticker_object(-1.0, 0.0, -1.0, 0.0)
    with pytest.raises(missingData):
        ticks = get_df_of_ticks_from_ticker_object(
            ticker_object, n_ticks=5, time_out_seconds=0.05
        )
        ticks.average_bid_offer_spread()
