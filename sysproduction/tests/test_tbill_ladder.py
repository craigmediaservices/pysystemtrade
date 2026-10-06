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


# --- liquidity tie-break, re-pricing and what-if guard (2026-10-06) ----------

from sysproduction.tbill_ladder import (
    spread_bp,
    choose_bill_with_reason,
    next_limit_step,
    what_if_problem,
)


def test_spread_bp_is_bid_yield_minus_ask_yield():
    days = 142
    ask = price_from_yield_pct(4.15, days)
    bid = price_from_yield_pct(4.17, days)
    assert abs(spread_bp(bid, ask, days) - 2.0) < 0.05
    assert spread_bp(np.nan, ask, days) != spread_bp(np.nan, ask, days)
    assert spread_bp(ask + 1, ask, days) != spread_bp(ask + 1, ask, days)  # crossed


def _two_bills(yield_a, spread_a, yield_b, spread_b):
    days = [135, 142]
    ys, ss = [yield_a, yield_b], [spread_a, spread_b]
    return pd.DataFrame(
        dict(
            cusip=["A", "B"],
            conId=[1, 2],
            term=["52-Week", "26-Week"],
            maturity=[datetime.date(2027, 2, 18), datetime.date(2027, 2, 25)],
            days=days,
            auction_yield_pct=[3.5, 3.9],
            ask=[price_from_yield_pct(y, d) for y, d in zip(ys, days)],
            bid=[
                price_from_yield_pct(y + s / 100.0, d) if s == s else np.nan
                for y, s, d in zip(ys, ss, days)
            ],
        )
    )


def test_close_yields_go_to_the_tighter_spread():
    # B yields 1bp more but its spread is 3x wider: A wins the tie
    idx, reason = choose_bill_with_reason(_two_bills(4.14, 0.5, 4.15, 1.5))
    assert idx == 0 and "tightest spread" in reason


def test_clear_yield_winner_ignores_spread():
    # B yields 5bp more: outside the 2bp tie band, best yield wins
    idx, reason = choose_bill_with_reason(_two_bills(4.10, 0.5, 4.15, 3.0))
    assert idx == 1 and reason == "best yield"


def test_unquoted_spreads_fall_back_to_best_yield():
    idx, _ = choose_bill_with_reason(_two_bills(4.14, np.nan, 4.15, np.nan))
    assert idx == 1


def test_next_limit_step_raises_until_the_floor():
    days = 142
    floor = price_from_yield_pct(3.9 - 0.15, days)
    price, _ = next_limit_step(floor - 0.02, 3.9, days, 0.15, 0.005)
    assert abs(price - (floor - 0.015)) < 1e-5
    # one step would cross the floor: capped at it
    price, _ = next_limit_step(floor - 0.002, 3.9, days, 0.15, 0.005)
    assert floor - 0.002 < price <= floor + 1e-9
    # already at the floor: cannot raise
    at_floor = np.floor(floor * 1e5) / 1e5
    price, reason = next_limit_step(at_floor, 3.9, days, 0.15, 0.005)
    assert price != price and "floor" in reason
    price, _ = next_limit_step(98.0, 3.9, days, 0.15, 0.0)
    assert price != price


def test_what_if_problem_spots_a_rejected_preview():
    # the Error 460 run on the new LLC account, 2026-10-06 12:04
    assert what_if_problem(
        dict(commission=1.7e308, initMarginChange=None, maintMarginChange=None)
    )
    assert what_if_problem(None)
    assert "permission" in what_if_problem(
        dict(initMarginChange="1.0", warningText="No trading permissions")
    )
    # the 12:13 run that filled
    assert (
        what_if_problem(dict(initMarginChange="1491.6", maintMarginChange="1491.59"))
        == ""
    )


# --- unfilled-order menu, driven with a fake IB ------------------------------

from unittest import mock

from sysproduction import interactive_tbill_ladder as itl


class _FakeTrade:
    def __init__(self, order_id, units, price, filled=0.0):
        self.order = SimpleNamespace(orderId=order_id, lmtPrice=price)
        self.contract = object()
        self.orderStatus = SimpleNamespace(
            status="Submitted",
            filled=filled,
            remaining=units - filled,
            avgFillPrice=0.0,
        )
        self._done = False

    def isDone(self):
        return self._done


class _FakeIB:
    """Records modifies/cancels/new orders; cancels can race extra fills."""

    def __init__(self, fill_on_modify=False, extra_fill_on_cancel=0.0):
        self.modified, self.cancelled, self.placed = [], [], []
        self.fill_on_modify = fill_on_modify
        self.extra_fill_on_cancel = extra_fill_on_cancel

    def placeOrder(self, contract, order):
        self.modified.append(order.lmtPrice)

    def cancelOrder(self, order):
        self.cancelled.append(order.orderId)

    def sleep(self, s):
        pass


def _menu_fixtures():
    days = 142
    cands = _two_bills(4.10, 0.5, 4.16, 1.0)  # B (row 1) first choice, A the switch
    cands = cands.assign(ask_yield_pct=[4.10, 4.16], spread_bp=[0.5, 1.0])
    proposal = dict(
        cusip="B",
        conId=2,
        maturity=cands.loc[1, "maturity"],
        days=days,
        term="26-Week",
        auction_yield_pct=3.9,
        units=150,
        face=150000.0,
        limit_price=98.40,
        limit_reason="IB ask + pad",
        implied_yield_pct=4.16,
        cost=147600.0,
        target_month="2027-02",
        target_reason="gap",
    )
    state = dict(account_id="U0", base_cash=500000.0, buffer=200000.0)
    settings = dict(
        fill_wait_seconds=0,
        yield_tolerance_pct=0.15,
        reprice_step=0.005,
        limit_pad=0.005,
    )
    data = SimpleNamespace(log=SimpleNamespace(warning=lambda *a, **k: None))
    return cands, proposal, state, settings, data


def _run_menu(answers, ib, trade, yes=(), switch_trade=None):
    cands, proposal, state, settings, data = _menu_fixtures()
    answers = iter(answers)
    yes = iter(yes)

    def fake_cancel(ib_, t, wait_seconds=15.0):
        ib_.cancelOrder(t.order)
        t.orderStatus.filled += ib_.extra_fill_on_cancel
        t.orderStatus.status = "Cancelled"
        t._done = True
        return itl.wait_for_fill(ib_, t, 0)

    def fake_modify(ib_, t, price):
        t.order.lmtPrice = price
        ib_.placeOrder(None, t.order)
        if ib_.fill_on_modify:
            t.orderStatus.filled, t.orderStatus.status, t._done = 150.0, "Filled", True

    with mock.patch.object(
        itl, "get_input_from_user_and_convert_to_type", lambda *a, **k: next(answers)
    ), mock.patch.object(
        itl, "true_if_answer_is_yes", lambda *a, **k: next(yes)
    ), mock.patch.object(
        itl, "cancel_and_wait", fake_cancel
    ), mock.patch.object(
        itl, "modify_limit", fake_modify
    ), mock.patch.object(
        itl, "_show_proposal_and_preview", lambda *a, **k: "contract"
    ), mock.patch.object(
        itl, "_requoted", lambda ib_, row, settings_: row
    ), mock.patch.object(
        itl,
        "_place",
        lambda d, i, c, p, a: (i.placed.append(p["units"]) or switch_trade),
    ), mock.patch(
        "builtins.print"
    ):
        return itl._handle_unfilled(data, ib, trade, proposal, state, settings, cands)


def test_raise_the_limit_then_it_fills():
    ib, trade = _FakeIB(fill_on_modify=True), _FakeTrade(1, 150, 98.40)
    _, proposal, result = _run_menu(["r"], ib, trade)
    assert ib.modified == [98.405] and result["status"] == "Filled"
    assert proposal["limit_price"] == 98.405 and not ib.cancelled


def test_leave_working_touches_nothing():
    ib, trade = _FakeIB(), _FakeTrade(1, 150, 98.40)
    _, _, result = _run_menu(["w"], ib, trade)
    assert not ib.modified and not ib.cancelled and not result["done"]


def test_cancel():
    ib, trade = _FakeIB(), _FakeTrade(1, 150, 98.40)
    _, _, result = _run_menu(["c"], ib, trade)
    assert ib.cancelled == [1] and result["status"] == "Cancelled"


def test_switch_declined_keeps_the_original_working():
    ib, trade = _FakeIB(), _FakeTrade(1, 150, 98.40)
    _run_menu(["s", "w"], ib, trade, yes=[False])
    assert not ib.cancelled and not ib.placed


def test_switch_buys_only_the_unfilled_units():
    # 40 filled before the switch, 10 more race in while cancelling -> buy 100
    ib = _FakeIB(extra_fill_on_cancel=10.0)
    trade = _FakeTrade(1, 150, 98.40, filled=40.0)
    new = _FakeTrade(2, 100, 98.70)
    new.orderStatus.status, new._done = "Filled", True
    _, proposal, result = _run_menu(["s"], ib, trade, yes=[True], switch_trade=new)
    assert ib.cancelled == [1] and ib.placed == [100]
    assert proposal["cusip"] == "A" and result["status"] == "Filled"


def test_working_bill_order_is_flagged_before_proposing():
    working = SimpleNamespace(
        contract=SimpleNamespace(secType="BILL", conId=916577747),
        order=SimpleNamespace(
            orderId=108, action="BUY", totalQuantity=150.0, lmtPrice=98.40681
        ),
        orderStatus=SimpleNamespace(status="Submitted", filled=0.0),
        isDone=lambda: False,
    )
    future = SimpleNamespace(
        contract=SimpleNamespace(secType="FUT", conId=1), isDone=lambda: False
    )
    ib = SimpleNamespace(reqAllOpenOrders=lambda: [working, future])
    with mock.patch("builtins.print"):
        with mock.patch.object(itl, "true_if_answer_is_yes", lambda *a: False):
            assert not itl._no_working_bill_orders(ib, dry_run=False)
        assert itl._no_working_bill_orders(ib, dry_run=True)  # dry run only warns
        ib_clear = SimpleNamespace(reqAllOpenOrders=lambda: [future])
        assert itl._no_working_bill_orders(ib_clear, dry_run=False)


def test_resend_places_a_fresh_order_for_the_same_bill():
    # the 2026-10-06 13:20 case: the same bill and size that hung filled at once
    # when sent as a new order
    ib = _FakeIB()
    trade = _FakeTrade(1, 150, 98.40)
    new = _FakeTrade(2, 150, 98.41)
    new.orderStatus.status, new._done = "Filled", True
    _, proposal, result = _run_menu(["n"], ib, trade, yes=[True], switch_trade=new)
    assert ib.cancelled == [1] and ib.placed == [150]
    assert proposal["cusip"] == "B" and result["status"] == "Filled"
    assert not ib.modified


def test_resend_declined_keeps_the_original_working():
    ib, trade = _FakeIB(), _FakeTrade(1, 150, 98.40)
    _run_menu(["n", "w"], ib, trade, yes=[False])
    assert not ib.cancelled and not ib.placed


def test_menu_offers_resend_first_and_as_default():
    cands, proposal, *_ = _menu_fixtures()
    trade = _FakeTrade(1, 150, 98.40)
    _, keys = itl._unfilled_options(
        trade, proposal, dict(filled=0.0), 98.405, "", cands, 0
    )
    assert list(keys) == ["n", "s", "r", "w", "c"]
    assert itl.UNFILLED_MENU_DEFAULT == "n"


# --- TreasuryDirect: one row per CUSIP, from its LATEST auction ---------------

# Real TreasuryDirect records (TA_WS/securities/auctioned?type=Bill&days=400,
# fetched 2026-10-06), trimmed to the fields the parser reads, in the order the
# API returned them (newest first; original list positions 0, 1, 41, 63, 75).
TD_RECORDS_NEWEST_FIRST = [
    {
        "cusip": "912797UZ8",
        "auctionDate": "2026-10-06T00:00:00",
        "issueDate": "2026-10-08T00:00:00",
        "maturityDate": "2026-11-19T00:00:00",
        "securityTerm": "6-Week",
        "highInvestmentRate": "4.018000",
    },
    {
        "cusip": "912797VS3",
        "auctionDate": "2026-10-05T00:00:00",
        "issueDate": "2026-10-08T00:00:00",
        "maturityDate": "2027-01-07T00:00:00",
        "securityTerm": "13-Week",
        "highInvestmentRate": "4.149000",
    },
    {
        "cusip": "912797UZ8",
        "auctionDate": "2026-08-17T00:00:00",
        "issueDate": "2026-08-20T00:00:00",
        "maturityDate": "2026-11-19T00:00:00",
        "securityTerm": "13-Week",
        "highInvestmentRate": "3.802000",
    },
    {
        "cusip": "912797VS3",
        "auctionDate": "2026-07-06T00:00:00",
        "issueDate": "2026-07-09T00:00:00",
        "maturityDate": "2027-01-07T00:00:00",
        "securityTerm": "26-Week",
        "highInvestmentRate": "3.960000",
    },
    {
        "cusip": "912797UZ8",
        "auctionDate": "2026-05-18T00:00:00",
        "issueDate": "2026-05-21T00:00:00",
        "maturityDate": "2026-11-19T00:00:00",
        "securityTerm": "26-Week",
        "highInvestmentRate": "3.733000",
    },
]


def _fetch_with_records(records):
    from sysproduction import tbill_ladder

    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: records)
    with mock.patch("requests.get", lambda *a, **k: response):
        bills = tbill_ladder.fetch_treasurydirect_bills()
    return {b["cusip"]: b for b in bills}


def test_treasurydirect_keeps_the_latest_auction_per_cusip():
    for records in (TD_RECORDS_NEWEST_FIRST, TD_RECORDS_NEWEST_FIRST[::-1]):
        bills = _fetch_with_records(records)
        assert sorted(bills) == ["912797UZ8", "912797VS3"]
        uz8, vs3 = bills["912797UZ8"], bills["912797VS3"]
        assert uz8["auction_yield_pct"] == 4.018 and uz8["term"] == "6-Week"
        assert uz8["issue_date"] == "2026-10-08"
        assert uz8["maturity"] == datetime.date(2026, 11, 19)
        assert vs3["auction_yield_pct"] == 4.149 and vs3["term"] == "13-Week"
