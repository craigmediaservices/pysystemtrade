"""
T-bill ladder purchase logic.

Turns the ladder report's ACTION line ("buy ~X of a bill maturing around
YYYY-MM") into a concrete, priced order:

  1. candidate bills  = TreasuryDirect auction list (CUSIP, maturity, auction
                        yield)  JOIN  the US-T bill universe IB will trade
  2. pick the month   = first ladder gap, else extend the ladder
  3. pick the bill    = best implied yield in that month (IB ask if quoted,
                        else the auction yield); ties go to the later maturity
  4. limit price      = IB ask, but never above the price implied by
                        (auction yield - tolerance): the "yield floor"
  5. size             = deployable cash rounded down, in whole $1,000 units

Everything above the IB helper section is pure and unit tested. The IB
helpers (quotes, what-if, place, wait) are thin wrappers around ib_async and
are only exercised by the interactive tool.

Nothing in this module places an order on import; `place_limit_buy` is the
single function that trades and the interactive tool only calls it after an
explicit y/n.
"""

import datetime
import time

import numpy as np
import pandas as pd

from sysdata.data_blob import dataBlob
from sysproduction.reporting.data.bond_holdings import (
    BILL_FACE_PER_UNIT,
    approx_bill_yield_pct,
    ib_units_from_face,
    round_down_to,
    month_key,
    _add_months,
    next_rung_month,
)

TREASURYDIRECT_URL = "https://www.treasurydirect.gov/TA_WS/securities/auctioned"
TREASURYDIRECT_LOOKBACK_DAYS = 400

IB_BILL_SYMBOL = "US-T"
IB_BILL_SECTYPE = "BILL"
IB_BILL_EXCHANGE = "SMART"
IB_BILL_CURRENCY = "USD"

DEFAULT_YIELD_TOLERANCE_PCT = 0.15  # accept up to this much below auction yield
DEFAULT_MONTH_SLACK_DAYS = 15  # if no bill matures IN the month, look this far out
DEFAULT_QUOTE_WAIT_SECONDS = 5
DEFAULT_FILL_WAIT_SECONDS = 60
DEFAULT_LIMIT_PAD = 0.005  # paid above the ask (per 100) so odd lots fill in one go
PRICE_DECIMALS = 5  # IB minTick for bills is 1e-05

CANDIDATE_COLUMNS = ["cusip", "maturity", "days", "term", "auction_yield_pct", "conId"]


def _config_value(data: dataBlob, key: str, default):
    try:
        return data.config.get_element(key)
    except BaseException:
        return default


def get_purchase_settings(data: dataBlob) -> dict:
    """
    Private-config keys (all optional):
      tbill_yield_tolerance_pct  max shortfall vs auction yield accepted (0.15)
      tbill_month_slack_days     days outside the target month to search (15)
      tbill_quote_wait_seconds   how long to wait for an IB quote (5)
      tbill_fill_wait_seconds    how long to wait for a fill after placing (60)
      tbill_limit_pad            price pad above the IB ask, per 100 (0.005);
                                 still capped by the yield floor
    """
    return dict(
        yield_tolerance_pct=float(
            _config_value(
                data, "tbill_yield_tolerance_pct", DEFAULT_YIELD_TOLERANCE_PCT
            )
        ),
        month_slack_days=int(
            _config_value(data, "tbill_month_slack_days", DEFAULT_MONTH_SLACK_DAYS)
        ),
        quote_wait_seconds=float(
            _config_value(data, "tbill_quote_wait_seconds", DEFAULT_QUOTE_WAIT_SECONDS)
        ),
        fill_wait_seconds=float(
            _config_value(data, "tbill_fill_wait_seconds", DEFAULT_FILL_WAIT_SECONDS)
        ),
        limit_pad=float(_config_value(data, "tbill_limit_pad", DEFAULT_LIMIT_PAD)),
    )


# ---------------------------------------------------------------------------
# Pure pricing / selection logic
# ---------------------------------------------------------------------------


def price_from_yield_pct(yield_pct, days, face: float = 100.0):
    """
    Inverse of approx_bill_yield_pct: clean price per `face` that gives a
    bond-equivalent yield of `yield_pct` over `days`. NaN on bad input.
    """
    try:
        yield_pct = float(yield_pct)
        days = float(days)
    except BaseException:
        return np.nan
    if yield_pct != yield_pct or days != days or days <= 0:
        return np.nan
    return face / (1.0 + yield_pct / 100.0 * days / 365.0)


def target_month_and_reason(
    gaps: list, asof_date: datetime.date, months: int, maturity_dates=None
):
    """Same choice the report's ACTION line makes: first gap, else extend
    past the last held rung (see bond_holdings.next_rung_month)."""
    return next_rung_month(
        maturity_dates or [], asof_date=asof_date, months=months, gaps=gaps
    )


def purchase_face(spare_cash, rounding: float) -> float:
    """Face to buy: deployable cash rounded down; never negative."""
    try:
        spare_cash = float(spare_cash)
    except BaseException:
        return 0.0
    if spare_cash != spare_cash or spare_cash <= 0:
        return 0.0
    return max(0.0, round_down_to(spare_cash, rounding))


def build_candidate_table(
    td_bills: list, ib_by_cusip: dict, asof_date: datetime.date
) -> pd.DataFrame:
    """
    td_bills: list of dict(cusip, maturity(date), term, auction_yield_pct)
    ib_by_cusip: cusip -> object with a .conId attribute (IB contract)

    Returns only bills IB will trade that have not matured, sorted by maturity.
    """
    rows = []
    for b in td_bills:
        cusip = b.get("cusip", "")
        maturity = b.get("maturity")
        if cusip not in ib_by_cusip or maturity is None:
            continue
        days = (maturity - asof_date).days
        if days <= 0:
            continue
        rows.append(
            dict(
                cusip=cusip,
                maturity=maturity,
                days=days,
                term=b.get("term", ""),
                auction_yield_pct=b.get("auction_yield_pct", np.nan),
                conId=ib_by_cusip[cusip].conId,
            )
        )
    df = pd.DataFrame(rows, columns=CANDIDATE_COLUMNS)
    if len(df):
        df = df.sort_values("maturity").reset_index(drop=True)
    return df


def _month_bounds(target_month: str):
    year, month = int(target_month[:4]), int(target_month[5:7])
    first = datetime.date(year, month, 1)
    last = _add_months(first, 1) - datetime.timedelta(days=1)
    return first, last


def candidates_for_month(
    table: pd.DataFrame, target_month: str, slack_days: int = DEFAULT_MONTH_SLACK_DAYS
) -> pd.DataFrame:
    """
    Bills maturing in target_month ('YYYY-MM'). If there are none, widen to
    `slack_days` either side of the month so a nearby bill can still fill the
    rung. Empty frame if nothing fits.
    """
    if len(table) == 0:
        return table
    first, last = _month_bounds(target_month)
    in_month = table[(table["maturity"] >= first) & (table["maturity"] <= last)]
    if len(in_month):
        return in_month.reset_index(drop=True)
    slack = datetime.timedelta(days=slack_days)
    near = table[
        (table["maturity"] >= first - slack) & (table["maturity"] <= last + slack)
    ]
    return near.reset_index(drop=True)


def limit_price_with_floor(
    ask, auction_yield_pct, days, tolerance_pct: float, pad: float = 0.0
):
    """
    Limit price = IB ask (+ pad, to clear IB's odd-lot minimum-size rule),
    capped by the price implied by (auction yield - tolerance). Returns
    (price, reason). NaN price if there is neither a usable ask nor a usable
    auction yield.
    """
    try:
        if ask == ask and float(ask) > 0:
            ask = float(ask) + float(pad)
    except BaseException:
        pass

    def _ok(v):
        try:
            v = float(v)
        except BaseException:
            return False
        return v == v and v > 0

    floor_price = (
        price_from_yield_pct(float(auction_yield_pct) - tolerance_pct, days)
        if _ok(auction_yield_pct)
        else np.nan
    )
    # truncate (never round up) so the limit always respects the yield floor
    if floor_price == floor_price:
        floor_price = (
            np.floor(floor_price * 10**PRICE_DECIMALS) / 10**PRICE_DECIMALS
        )
    ask_ok = _ok(ask)
    floor_ok = _ok(floor_price)

    if ask_ok and floor_ok:
        if float(ask) <= floor_price:
            price, reason = float(ask), "IB ask%s (within yield floor)" % (
                " + pad" if pad else ""
            )
        else:
            (
                price,
                reason,
            ) = floor_price, "yield floor (IB ask %.5f is above it)" % float(ask)
    elif ask_ok:
        price, reason = float(ask), "IB ask (no auction yield to check against)"
    elif floor_ok:
        price, reason = floor_price, "yield-derived (no IB quote)"
    else:
        return np.nan, "no usable price"

    return round(price, PRICE_DECIMALS), reason


def implied_yield_for_row(ask, days):
    return approx_bill_yield_pct(ask, days)


def choose_bill(cands: pd.DataFrame):
    """
    Pick the candidate with the best yield: IB-ask-implied yield when quoted,
    else the auction yield. Ties (within 0.005%) go to the later maturity.
    Returns the index label of the chosen row, or None if empty.
    """
    if len(cands) == 0:
        return None
    scores = []
    for idx, row in cands.iterrows():
        ask = row.get("ask", np.nan)
        score = approx_bill_yield_pct(ask, row["days"]) if ask == ask else np.nan
        if score != score:
            score = row.get("auction_yield_pct", np.nan)
        if score != score:
            score = -np.inf
        scores.append((round(float(score), 3), row["maturity"], idx))
    scores.sort(key=lambda t: (t[0], t[1]))
    return scores[-1][2]


def order_cost(units: int, price) -> float:
    """Cash needed for `units` x $1,000 face at `price` per 100."""
    try:
        return float(units) * BILL_FACE_PER_UNIT * float(price) / 100.0
    except BaseException:
        return np.nan


def build_proposal(
    face: float,
    chosen: pd.Series,
    limit_price,
    limit_reason: str,
    target_month: str,
    target_reason: str,
) -> dict:
    units = ib_units_from_face(face)
    cost = order_cost(units, limit_price)
    return dict(
        cusip=chosen["cusip"],
        conId=int(chosen["conId"]),
        maturity=chosen["maturity"],
        days=int(chosen["days"]),
        term=chosen.get("term", ""),
        auction_yield_pct=chosen.get("auction_yield_pct", np.nan),
        face=units * BILL_FACE_PER_UNIT,
        units=units,
        limit_price=limit_price,
        limit_reason=limit_reason,
        implied_yield_pct=approx_bill_yield_pct(limit_price, chosen["days"]),
        cost=cost,
        target_month=target_month,
        target_reason=target_reason,
    )


def proposal_text(p: dict, base_cash, buffer) -> str:
    def _f(v, d=0):
        return "n/a" if v is None or v != v else format(round(float(v), d), ",")

    lines = [
        "BUY %d units (face %s) of US T-bill CUSIP %s maturing %s (%d days, %s)"
        % (p["units"], _f(p["face"]), p["cusip"], p["maturity"], p["days"], p["term"]),
        "  limit %.5f per 100 [%s] -> implied yield %.3f%% (auction %.3f%%)"
        % (
            p["limit_price"],
            p["limit_reason"],
            p["implied_yield_pct"],
            float(p["auction_yield_pct"])
            if p["auction_yield_pct"] == p["auction_yield_pct"]
            else np.nan,
        ),
        "  cost ~%s; cash after ~%s vs buffer %s"
        % (_f(p["cost"]), _f(float(base_cash) - p["cost"]), _f(buffer)),
        "  target month %s (%s)" % (p["target_month"], p["target_reason"]),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# External data: TreasuryDirect
# ---------------------------------------------------------------------------


def fetch_treasurydirect_bills(
    days: int = TREASURYDIRECT_LOOKBACK_DAYS, timeout: float = 20.0
) -> list:
    """
    Bills auctioned in the last `days`: list of dict(cusip, maturity(date),
    term, auction_yield_pct, issue_date). Raises on network failure.
    """
    import requests

    r = requests.get(
        TREASURYDIRECT_URL,
        params=dict(type="Bill", days=days, format="json"),
        timeout=timeout,
    )
    r.raise_for_status()
    out = {}
    for b in r.json():
        cusip = b.get("cusip", "")
        try:
            maturity = datetime.date.fromisoformat(str(b.get("maturityDate", ""))[:10])
        except BaseException:
            continue
        try:
            auction_yield = float(b.get("highInvestmentRate") or np.nan)
        except BaseException:
            auction_yield = np.nan
        # the same CUSIP can be re-opened at several auctions: keep the latest
        out[cusip] = dict(
            cusip=cusip,
            maturity=maturity,
            term=b.get("securityTerm", ""),
            auction_yield_pct=auction_yield,
            issue_date=str(b.get("issueDate", ""))[:10],
        )
    return list(out.values())


# ---------------------------------------------------------------------------
# IB helpers (thin ib_async wrappers; not unit tested)
# ---------------------------------------------------------------------------


def fetch_ib_bill_universe(ib) -> dict:
    """cusip -> IB Contract for every US T-bill IB will trade."""
    from ib_async import Contract

    cds = ib.reqContractDetails(
        Contract(
            secType=IB_BILL_SECTYPE,
            symbol=IB_BILL_SYMBOL,
            exchange=IB_BILL_EXCHANGE,
            currency=IB_BILL_CURRENCY,
        )
    )
    out = {}
    for cd in cds:
        for tv in getattr(cd, "secIdList", None) or []:
            if getattr(tv, "tag", "") == "CUSIP":
                out[tv.value] = cd.contract
                break
    return out


def get_quote(ib, contract, wait_seconds: float = DEFAULT_QUOTE_WAIT_SECONDS) -> dict:
    """Snapshot bid/ask/last/close (delayed data if live is not subscribed)."""
    ib.reqMarketDataType(2)
    ticker = ib.reqMktData(contract, "", True, False)
    ib.sleep(wait_seconds)
    out = dict(bid=ticker.bid, ask=ticker.ask, last=ticker.last, close=ticker.close)
    # snapshot requests end by themselves (cancelling one logs IB Error 300);
    # IB uses -1 for "no quote", which we report as NaN
    return {k: _clean_quote_value(v) for k, v in out.items()}


def _clean_quote_value(v):
    try:
        v = float(v)
    except BaseException:
        return np.nan
    if v != v or v <= 0:
        return np.nan
    return v


def _limit_buy_order(units: int, price: float, account: str, tif: str = "DAY"):
    from ib_async import LimitOrder

    order = LimitOrder("BUY", float(units), float(price), tif=tif)
    if account:
        order.account = account
    return order


def what_if_buy(ib, contract, units: int, price: float, account: str) -> dict:
    """IB's pre-trade check: commission and margin impact. Never trades."""
    order = _limit_buy_order(units, price, account)
    state = ib.whatIfOrder(contract, order)
    keys = (
        "commission",
        "initMarginChange",
        "maintMarginChange",
        "equityWithLoanChange",
        "warningText",
        "status",
    )
    return {k: getattr(state, k, None) for k in keys}


def place_limit_buy(ib, contract, units: int, price: float, account: str, tif="DAY"):
    """THE ONLY FUNCTION HERE THAT TRADES. Returns the ib_async Trade."""
    order = _limit_buy_order(units, price, account, tif=tif)
    return ib.placeOrder(contract, order)


def wait_for_fill(ib, trade, wait_seconds: float = DEFAULT_FILL_WAIT_SECONDS) -> dict:
    deadline = time.time() + wait_seconds
    while time.time() < deadline and not trade.isDone():
        ib.sleep(2)
    status = trade.orderStatus
    return dict(
        order_id=trade.order.orderId,
        status=status.status,
        filled=status.filled,
        remaining=status.remaining,
        avg_fill_price=status.avgFillPrice,
        done=trade.isDone(),
    )
