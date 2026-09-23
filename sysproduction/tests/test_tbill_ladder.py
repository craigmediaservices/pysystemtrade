import datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd

from sysproduction.reporting.data.bond_holdings import (
    approx_bill_yield_pct,
    face_from_ib_position,
    ib_units_from_face,
)
from sysproduction.tbill_ladder import (
    price_from_yield_pct,
    target_month_and_reason,
    purchase_face,
    build_candidate_table,
    candidates_for_month,
    limit_price_with_floor,
    choose_bill,
    order_cost,
    _clean_quote_value,
    build_proposal,
    proposal_text,
)

ASOF = datetime.date(2026, 9, 7)


def test_face_units_round_trip():
    # IB: position 100 == 100,000 face
    assert face_from_ib_position(100) == 100000.0
    assert ib_units_from_face(100000.0) == 100
    assert ib_units_from_face(100999.0) == 100  # rounds down to whole units
    assert ib_units_from_face(999.0) == 0
    assert face_from_ib_position("x") != face_from_ib_position("x")  # NaN
    assert ib_units_from_face(None) == 0


def test_price_from_yield_inverts_yield():
    days = 180
    for y in (0.5, 3.9, 5.25):
        p = price_from_yield_pct(y, days)
        assert abs(approx_bill_yield_pct(p, days) - y) < 1e-9
    assert price_from_yield_pct(np.nan, days) != price_from_yield_pct(np.nan, days)
    assert price_from_yield_pct(4.0, 0) != price_from_yield_pct(4.0, 0)
    assert price_from_yield_pct(4.0, days) < 100.0


def test_target_month_and_reason():
    m, why = target_month_and_reason(["2027-03", "2027-05"], ASOF, 6)
    assert m == "2027-03" and "gap" in why
    m, why = target_month_and_reason([], ASOF, 6)
    assert m == "2027-03" and "extends" in why
    # ladder full through Apr-2027: extend to the month AFTER the last rung
    held = [
        datetime.date(2026, 10, 29),
        datetime.date(2027, 3, 4),
        datetime.date(2027, 4, 15),
    ]
    m, why = target_month_and_reason([], ASOF, 6, maturity_dates=held)
    assert m == "2027-05" and "extends" in why
    # last rung inside the window: window end wins
    held = [datetime.date(2026, 10, 29), datetime.date(2026, 12, 24)]
    m, why = target_month_and_reason([], ASOF, 6, maturity_dates=held)
    assert m == "2027-03"


def test_purchase_face():
    assert purchase_face(123456.0, 10000.0) == 120000.0
    assert purchase_face(-5.0, 10000.0) == 0.0
    assert purchase_face(np.nan, 10000.0) == 0.0
    assert purchase_face("bad", 10000.0) == 0.0


def _td_bills():
    return [
        dict(
            cusip="A",
            maturity=datetime.date(2027, 3, 4),
            term="26-Week",
            auction_yield_pct=4.02,
        ),
        dict(
            cusip="B",
            maturity=datetime.date(2027, 3, 18),
            term="52-Week",
            auction_yield_pct=3.95,
        ),
        dict(
            cusip="C",
            maturity=datetime.date(2027, 2, 25),
            term="26-Week",
            auction_yield_pct=3.92,
        ),
        dict(
            cusip="NOT_AT_IB",
            maturity=datetime.date(2027, 3, 11),
            term="26-Week",
            auction_yield_pct=4.5,
        ),
        dict(
            cusip="MATURED",
            maturity=datetime.date(2026, 9, 1),
            term="17-Week",
            auction_yield_pct=4.0,
        ),
        dict(cusip="NOMAT", maturity=None, term="", auction_yield_pct=4.0),
    ]


def _ib_universe():
    return {
        "A": SimpleNamespace(conId=1),
        "B": SimpleNamespace(conId=2),
        "C": SimpleNamespace(conId=3),
        "MATURED": SimpleNamespace(conId=4),
        "NOMAT": SimpleNamespace(conId=5),
    }


def test_build_candidate_table_joins_and_filters():
    table = build_candidate_table(_td_bills(), _ib_universe(), ASOF)
    assert list(table["cusip"]) == ["C", "A", "B"]  # sorted by maturity
    assert "NOT_AT_IB" not in set(table["cusip"])
    assert "MATURED" not in set(table["cusip"])
    assert int(table.loc[table["cusip"] == "A", "conId"].iloc[0]) == 1
    assert (
        int(table.loc[table["cusip"] == "A", "days"].iloc[0])
        == (datetime.date(2027, 3, 4) - ASOF).days
    )


def test_build_candidate_table_empty():
    table = build_candidate_table([], {}, ASOF)
    assert len(table) == 0
    assert "cusip" in table.columns


def test_candidates_for_month_and_slack():
    table = build_candidate_table(_td_bills(), _ib_universe(), ASOF)
    in_march = candidates_for_month(table, "2027-03", 15)
    assert list(in_march["cusip"]) == ["A", "B"]
    # nothing in April: the 15-day slack picks up B (Mar 18 is 14 days before Apr 1)
    near_april = candidates_for_month(table, "2027-04", 15)
    assert list(near_april["cusip"]) == ["B"]
    assert len(candidates_for_month(table, "2027-06", 15)) == 0
    assert len(candidates_for_month(table.iloc[0:0], "2027-03", 15)) == 0


def test_limit_price_uses_ask_when_within_floor():
    days = 180
    floor = price_from_yield_pct(4.0 - 0.15, days)
    ask = floor - 0.01  # cheaper than the floor -> fine
    price, reason = limit_price_with_floor(ask, 4.0, days, 0.15)
    assert abs(price - ask) < 1e-5
    assert "IB ask" in reason


def test_limit_price_caps_at_yield_floor():
    days = 180
    floor = price_from_yield_pct(4.0 - 0.15, days)
    ask = floor + 0.05  # too expensive -> capped
    price, reason = limit_price_with_floor(ask, 4.0, days, 0.15)
    assert abs(price - floor) < 1e-5
    assert "yield floor" in reason
    # the capped price implies at least (auction - tolerance)
    assert approx_bill_yield_pct(price, days) >= 4.0 - 0.15 - 1e-6


def test_limit_price_fallbacks():
    days = 180
    price, reason = limit_price_with_floor(np.nan, 4.0, days, 0.15)
    assert abs(price - price_from_yield_pct(3.85, days)) < 1e-5
    assert "no IB quote" in reason
    price, reason = limit_price_with_floor(98.2, np.nan, days, 0.15)
    assert price == 98.2 and "no auction yield" in reason
    price, reason = limit_price_with_floor(np.nan, np.nan, days, 0.15)
    assert price != price and reason == "no usable price"
    price, reason = limit_price_with_floor(-1.0, 0.0, days, 0.15)
    assert price != price


def test_choose_bill_prefers_ask_yield_then_auction_then_later_maturity():
    cands = pd.DataFrame(
        dict(
            cusip=["A", "B", "C"],
            maturity=[
                datetime.date(2027, 3, 4),
                datetime.date(2027, 3, 18),
                datetime.date(2027, 3, 25),
            ],
            days=[178, 192, 199],
            auction_yield_pct=[4.02, 3.95, 3.95],
            ask=[np.nan, price_from_yield_pct(4.10, 192), np.nan],
        )
    )
    # B has a live ask implying 4.10 > A's auction 4.02
    assert choose_bill(cands) == 1
    cands["ask"] = np.nan
    # no quotes: A wins on auction yield
    assert choose_bill(cands) == 0
    cands.loc[0, "auction_yield_pct"] = 3.95
    # three-way tie on yield: later maturity wins
    assert choose_bill(cands) == 2
    assert choose_bill(cands.iloc[0:0]) is None
    cands["auction_yield_pct"] = np.nan
    assert choose_bill(cands) in (0, 1, 2)  # all -inf, still returns a row


def test_order_cost_and_proposal_text():
    assert order_cost(100, 98.0) == 98000.0
    assert order_cost(100, np.nan) != order_cost(100, np.nan)
    chosen = pd.Series(
        dict(
            cusip="A",
            conId=1,
            maturity=datetime.date(2027, 3, 4),
            days=178,
            term="26-Week",
            auction_yield_pct=4.02,
        )
    )
    p = build_proposal(120999.0, chosen, 98.1, "IB ask", "2027-03", "fills the gap")
    assert p["units"] == 120 and p["face"] == 120000.0
    assert abs(p["cost"] - 120 * 1000 * 0.981) < 1e-6
    text = proposal_text(p, base_cash=250000.0, buffer=100000.0)
    assert "BUY 120 units" in text and "CUSIP A" in text and "2027-03" in text
    assert "cash after" in text


def test_clean_quote_value_treats_ib_minus_one_as_missing():
    assert _clean_quote_value(98.1) == 98.1
    assert _clean_quote_value(-1.0) != _clean_quote_value(-1.0)
    assert _clean_quote_value(None) != _clean_quote_value(None)
    assert _clean_quote_value("x") != _clean_quote_value("x")


def test_limit_price_pad_is_applied_but_capped():
    days = 180
    ask = 98.0
    price, reason = limit_price_with_floor(ask, 4.0, days, 0.15, pad=0.005)
    assert abs(price - 98.005) < 1e-9 and "pad" in reason
    floor = price_from_yield_pct(4.0 - 0.15, days)
    price, reason = limit_price_with_floor(floor - 0.001, 4.0, days, 0.15, pad=0.05)
    assert price <= floor + 1e-9 and "yield floor" in reason
