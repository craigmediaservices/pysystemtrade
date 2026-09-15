"""
Helpers for the T-bill / bond ladder report.

The pure-ish math (parsing IB dates, days to maturity, approximate bill yield,
ladder bucketing) is unit tested; the IB position fetch needs a live broker
connection so it isn't.

Nothing here trades - this is read-only reporting support.
"""

import datetime

import numpy as np
import pandas as pd

from sysdata.data_blob import dataBlob

# IB secType values we treat as Treasury / bond holdings
BOND_SEC_TYPES = ("BOND", "BILL")

# IB quotes bills/bonds per 100 but holds positions in units of $1,000 face
BILL_FACE_PER_UNIT = 1000.0


def face_from_ib_position(position) -> float:
    """IB position units -> face value in currency (100 units = 100,000 face)."""
    try:
        return float(position) * BILL_FACE_PER_UNIT
    except BaseException:
        return np.nan


def ib_units_from_face(face) -> int:
    """Face value in currency -> whole IB position units, rounded down."""
    try:
        return int(np.floor(float(face) / BILL_FACE_PER_UNIT))
    except BaseException:
        return 0


# Short-term Treasury ETFs we surface as "near cash" (so VGSH etc. show up
# rather than being silently ignored). Extend as needed.
NEAR_CASH_ETF_SYMBOLS = ("VGSH", "SHV", "BIL", "SGOV", "USFR", "SHY", "SCHO")

DEFAULT_MATURING_SOON_DAYS = 35


def get_maturing_soon_days(data: dataBlob) -> int:
    """Days-to-maturity at/under which a rung is flagged 'maturing soon'."""
    try:
        return int(data.config.get_element("tbill_maturing_soon_days"))
    except BaseException:
        return DEFAULT_MATURING_SOON_DAYS


def parse_ib_date(date_str):
    """IB gives 'YYYYMMDD' for bond maturity / issue dates. Returns date or None."""
    if date_str is None or date_str == "":
        return None
    try:
        return datetime.datetime.strptime(str(date_str)[:8], "%Y%m%d").date()
    except BaseException:
        return None


def days_to_maturity(maturity_date, asof_date=None):
    if maturity_date is None:
        return np.nan
    if asof_date is None:
        asof_date = datetime.date.today()
    return (maturity_date - asof_date).days


def is_zero_coupon(coupon) -> bool:
    if coupon is None:
        return True
    try:
        coupon = float(coupon)
    except BaseException:
        return False
    if coupon != coupon:  # NaN
        return True
    return coupon == 0.0


def approx_bill_yield_pct(clean_price, days, face: float = 100.0):
    """
    Bond-equivalent (investment) yield of a zero-coupon discount bill, in %.

    clean_price is quoted per `face` (IB quotes bonds per 100). Returns NaN if
    the inputs are unusable. This is an approximation for at-a-glance use, not
    a settlement-accurate YTM.
    """
    if clean_price is None or days is None:
        return np.nan
    try:
        clean_price = float(clean_price)
        days = float(days)
    except BaseException:
        return np.nan
    if clean_price <= 0 or days <= 0:
        return np.nan
    return (face - clean_price) / clean_price * (365.0 / days) * 100.0


def ladder_buckets(maturities_and_face) -> pd.DataFrame:
    """
    maturities_and_face: iterable of (maturity_date_or_None, face_amount).
    Returns a DataFrame indexed by 'YYYY-MM' (plus 'unknown' last) with a
    single 'face' column - the shape of the ladder by maturity month.
    """
    rows: dict = {}
    for maturity_date, face in maturities_and_face:
        if maturity_date is None:
            key = "unknown"
        else:
            key = "%04d-%02d" % (maturity_date.year, maturity_date.month)
        rows[key] = rows.get(key, 0.0) + float(face)

    if not rows:
        return pd.DataFrame(columns=["face"])

    df = pd.DataFrame.from_dict(rows, orient="index", columns=["face"])
    known = sorted(k for k in df.index if k != "unknown")
    order = known + (["unknown"] if "unknown" in df.index else [])
    return df.reindex(order)


def maturing_soon_df(bond_df: pd.DataFrame, days_threshold: int) -> pd.DataFrame:
    if len(bond_df) == 0:
        return bond_df
    mask = bond_df["days_to_maturity"].apply(
        lambda d: (d is not None) and (d == d) and (d <= days_threshold)
    )
    return bond_df[mask]


def _portfolio_items(data: dataBlob):
    ib = data.ib_conn.ib
    return ib, ib.portfolio()


def get_bond_holdings_df(data: dataBlob) -> pd.DataFrame:
    """
    Treasury bill/bond positions from IB.

    Columns: cusip, secType, maturity, days_to_maturity, face, mark_price,
             market_value, approx_yield_pct, coupon  (indexed by symbol/cusip)
    """
    from ib_async import Contract  # deferred so module imports without IB

    ib, portfolio = _portfolio_items(data)
    today = datetime.date.today()

    rows = []
    for item in portfolio:
        contract = item.contract
        if contract.secType not in BOND_SEC_TYPES:
            continue

        # IB stores a bill/bond's maturity in the portfolio contract's
        # lastTradeDateOrContractMonth (the bond `maturity` field on
        # ContractDetails is left empty for T-bills).
        maturity_date = parse_ib_date(
            getattr(contract, "lastTradeDateOrContractMonth", "")
        )
        cusip = ""
        coupon = np.nan
        try:
            cds = ib.reqContractDetails(Contract(conId=contract.conId))
            if cds:
                cd = cds[0]
                # If the bond-specific maturity field is populated, prefer it
                cd_maturity = parse_ib_date(getattr(cd, "maturity", None))
                if cd_maturity is not None:
                    maturity_date = cd_maturity
                # CUSIP/ISIN live in secIdList as TagValue entries; cd.cusip
                # is IB's internal contract id (e.g. "IBCID826931582"), not a
                # real CUSIP, so we ignore it.
                for tv in getattr(cd, "secIdList", None) or []:
                    if getattr(tv, "tag", "") == "CUSIP":
                        cusip = tv.value
                        break
                coupon = getattr(cd, "coupon", np.nan)
        except BaseException:
            pass

        dtm = days_to_maturity(maturity_date, today)
        mark = item.marketPrice
        # IB reports bill/bond positions in units of $1,000 face: a position of
        # 100 marking at 99.47 shows a market value of ~99,473 (verified
        # 2026-09-07 on a held US-T bill), so face = position * 1000.
        face = face_from_ib_position(item.position)
        approx_yield = (
            approx_bill_yield_pct(mark, dtm) if is_zero_coupon(coupon) else np.nan
        )

        rows.append(
            dict(
                symbol=(contract.symbol or cusip or str(contract.conId)),
                cusip=cusip,
                secType=contract.secType,
                maturity=maturity_date.isoformat() if maturity_date else "",
                days_to_maturity=dtm,
                face=face,
                mark_price=mark,
                market_value=item.marketValue,
                approx_yield_pct=approx_yield,
                coupon=coupon,
            )
        )

    cols = [
        "symbol",
        "cusip",
        "secType",
        "maturity",
        "days_to_maturity",
        "face",
        "mark_price",
        "market_value",
        "approx_yield_pct",
        "coupon",
    ]
    df = pd.DataFrame(rows, columns=cols)
    if len(df) > 0:
        df = df.set_index("symbol").sort_values("days_to_maturity", na_position="last")
    return df


def get_near_cash_etf_df(data: dataBlob) -> pd.DataFrame:
    """Short-term Treasury ETF holdings (treated as near-cash in the report)."""
    _ib, portfolio = _portfolio_items(data)

    rows = []
    for item in portfolio:
        contract = item.contract
        if contract.secType != "STK":
            continue
        if (contract.symbol or "").upper() not in NEAR_CASH_ETF_SYMBOLS:
            continue
        rows.append(
            dict(
                symbol=contract.symbol,
                shares=item.position,
                mark_price=item.marketPrice,
                market_value=item.marketValue,
            )
        )

    df = pd.DataFrame(rows, columns=["symbol", "shares", "mark_price", "market_value"])
    if len(df) > 0:
        df = df.set_index("symbol")
    return df


def ladder_buckets_from_bond_df(bond_df: pd.DataFrame) -> pd.DataFrame:
    if len(bond_df) == 0:
        return pd.DataFrame(columns=["face"])
    pairs = [
        (parse_ib_date(m.replace("-", "")) if m else None, f)
        for m, f in zip(bond_df["maturity"], bond_df["face"])
    ]
    return ladder_buckets(pairs)


# ---------------------------------------------------------------------------
# Deployable-cash / margin-aware suggestion logic
#
# IB credits short Treasuries toward futures margin (excess liquidity ==
# net liquidation - maintenance margin when they are fully counted), so cash
# is only needed to settle daily variation margin. The buffer is therefore
# sized to P&L swings, not to the margin requirement.
# ---------------------------------------------------------------------------

DEFAULT_CASH_BUFFER_SIGMA = 3.0
DEFAULT_CASH_BUFFER_DAYS = 10
DEFAULT_CASH_BUFFER_FLOOR = 100000.0
DEFAULT_PNL_WINDOW_DAYS = 250
DEFAULT_LADDER_MONTHS = 6
DEFAULT_TRADE_ROUNDING = 10000.0
# Bills are treated as fully counted toward margin if the unexplained gap in
# excess liquidity is under this fraction of net liquidation
COLLATERAL_TOLERANCE = 0.02

MARGIN_SUMMARY_TAGS = (
    "NetLiquidation",
    "TotalCashValue",
    "FullInitMarginReq",
    "FullMaintMarginReq",
    "FullExcessLiquidity",
    "FullAvailableFunds",
)


def _config_value(data: dataBlob, key: str, default):
    try:
        return data.config.get_element(key)
    except BaseException:
        return default


def get_cash_buffer_settings(data: dataBlob) -> dict:
    """
    Private-config keys (all optional):
      tbill_cash_buffer        fixed USD buffer; overrides the derived one
      tbill_cash_buffer_sigma  sigmas of daily P&L to cover (default 3)
      tbill_cash_buffer_days   horizon in business days (default 10)
      tbill_cash_buffer_floor  minimum buffer in USD (default 100,000)
      tbill_pnl_window_days    lookback for realised daily P&L sd (default 250)
      tbill_ladder_months      ladder length in months (default 6)
      tbill_trade_rounding     round suggested purchases to this (default 10,000)
    """
    fixed = _config_value(data, "tbill_cash_buffer", None)
    return dict(
        fixed=None if fixed is None else float(fixed),
        sigma=float(
            _config_value(data, "tbill_cash_buffer_sigma", DEFAULT_CASH_BUFFER_SIGMA)
        ),
        days=int(
            _config_value(data, "tbill_cash_buffer_days", DEFAULT_CASH_BUFFER_DAYS)
        ),
        floor=float(
            _config_value(data, "tbill_cash_buffer_floor", DEFAULT_CASH_BUFFER_FLOOR)
        ),
        pnl_window=int(
            _config_value(data, "tbill_pnl_window_days", DEFAULT_PNL_WINDOW_DAYS)
        ),
        ladder_months=int(
            _config_value(data, "tbill_ladder_months", DEFAULT_LADDER_MONTHS)
        ),
        rounding=float(
            _config_value(data, "tbill_trade_rounding", DEFAULT_TRADE_ROUNDING)
        ),
    )


def daily_pnl_sd_from_capital_series(series: pd.Series, window_days: int) -> float:
    """
    Realised sd of business-daily changes in the broker account value over
    the last `window_days` observations. NaN if fewer than 10 usable points.
    """
    if series is None or len(series) == 0:
        return np.nan
    series = series.dropna()
    if len(series) == 0:
        return np.nan
    daily = series.resample("1B").last().dropna()
    pnl = daily.diff().dropna()
    if window_days > 0:
        pnl = pnl[-window_days:]
    if len(pnl) < 10:
        return np.nan
    return float(pnl.std())


def derived_cash_buffer(daily_sd, sigma: float, days: int, floor: float) -> float:
    """max(floor, sigma * daily_sd * sqrt(days)); floor alone if sd unusable."""
    try:
        daily_sd = float(daily_sd)
    except BaseException:
        return float(floor)
    if daily_sd != daily_sd or daily_sd <= 0:
        return float(floor)
    return float(max(floor, sigma * daily_sd * np.sqrt(days)))


def cash_buffer_with_explanation(settings: dict, daily_sd) -> tuple:
    """Returns (buffer_usd, human-readable derivation)."""
    if settings.get("fixed") is not None:
        buffer = float(settings["fixed"])
        return buffer, "fixed via config 'tbill_cash_buffer'"

    buffer = derived_cash_buffer(
        daily_sd, settings["sigma"], settings["days"], settings["floor"]
    )
    if daily_sd != daily_sd or daily_sd is None:
        text = "floor %s (no usable daily P&L history)" % format(
            round(settings["floor"]), ","
        )
    else:
        raw = settings["sigma"] * float(daily_sd) * np.sqrt(settings["days"])
        text = "max(floor %s, %.1f sigma x daily P&L sd %s x sqrt(%d days) = %s)" % (
            format(round(settings["floor"]), ","),
            settings["sigma"],
            format(round(daily_sd), ","),
            settings["days"],
            format(round(raw), ","),
        )
    return buffer, text


def deployable_cash(usd_cash: float, buffer: float) -> float:
    return float(usd_cash) - float(buffer)


def cash_available_for_bills(base_cash: float, total_cash) -> float:
    """
    Cash we can actually put into bills. Base-currency cash overstates this
    when other currency balances are negative (e.g. a delivered JPY future
    leaves +USD / -JPY): the USD is spoken for by the sweep that repays the
    yen. So use the lower of base cash and IB's TotalCashValue (all currencies
    netted in base). Falls back to base cash when the total is unusable.
    """
    base_cash = float(base_cash)
    try:
        total_cash = float(total_cash)
    except BaseException:
        return base_cash
    if total_cash != total_cash:  # NaN
        return base_cash
    return min(base_cash, total_cash)


def collateral_check(
    nlv, maint_margin, excess_liquidity, tolerance=COLLATERAL_TOLERANCE
) -> dict:
    """
    If bills are fully credited toward margin, excess liquidity should equal
    net liquidation minus maintenance margin. Any shortfall is an implied
    haircut on non-cash collateral.
    Returns dict(haircut, fully_counted, usable) - usable False if inputs bad.
    """
    try:
        nlv = float(nlv)
        maint = float(maint_margin)
        excess = float(excess_liquidity)
    except BaseException:
        return dict(haircut=np.nan, fully_counted=False, usable=False)
    if any(v != v for v in (nlv, maint, excess)) or nlv <= 0:
        return dict(haircut=np.nan, fully_counted=False, usable=False)
    haircut = (nlv - maint) - excess
    return dict(
        haircut=haircut,
        fully_counted=haircut <= tolerance * nlv,
        usable=True,
    )


def _add_months(d: datetime.date, n: int) -> datetime.date:
    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    return datetime.date(year, month, 1)


def month_key(d: datetime.date) -> str:
    return "%04d-%02d" % (d.year, d.month)


def ladder_gaps(
    maturity_dates, asof_date=None, months: int = DEFAULT_LADDER_MONTHS
) -> list:
    """
    Months (as 'YYYY-MM') in the window [next month, asof + months] with no
    rung maturing. maturity_dates: iterable of date or None.
    """
    if asof_date is None:
        asof_date = datetime.date.today()
    held = set(month_key(m) for m in maturity_dates if m is not None)
    return [
        month_key(_add_months(asof_date, i))
        for i in range(1, months + 1)
        if month_key(_add_months(asof_date, i)) not in held
    ]


def maturity_dates_from_bond_df(bond_df: pd.DataFrame) -> list:
    if len(bond_df) == 0:
        return []
    return [
        parse_ib_date(m.replace("-", "")) if m else None for m in bond_df["maturity"]
    ]


def round_down_to(amount: float, rounding: float) -> float:
    if rounding <= 0:
        return float(amount)
    return float(np.floor(float(amount) / rounding) * rounding)


def target_rung_size(total_near_cash: float, buffer: float, months: int) -> float:
    """Even rung size if (cash + bills - buffer) were spread over the ladder."""
    if months <= 0:
        return np.nan
    return max(0.0, (float(total_near_cash) - float(buffer)) / months)


def next_rung_month(
    maturity_dates,
    asof_date=None,
    months: int = DEFAULT_LADDER_MONTHS,
    gaps: list = None,
) -> tuple:
    """
    Which month the next purchase should mature in, and why.

    First gap in the window wins. With no gap, extend past the LAST held
    rung (not just asof + months, which would re-propose a month already
    held once the window is full). Returns ('YYYY-MM', reason).
    """
    if asof_date is None:
        asof_date = datetime.date.today()
    if gaps is None:
        gaps = ladder_gaps(maturity_dates, asof_date, months)
    if gaps:
        return gaps[0], "fills the ladder gap at %s" % gaps[0]
    held = [m for m in maturity_dates if m is not None]
    window_end = _add_months(asof_date, months)
    if held:
        after_last = _add_months(max(held), 1)
        target = max(after_last, window_end)
    else:
        target = window_end
    return month_key(target), "extends the ladder (no gaps)"


def suggested_action(
    usd_cash: float,
    buffer: float,
    gaps: list,
    asof_date=None,
    months: int = DEFAULT_LADDER_MONTHS,
    rounding: float = DEFAULT_TRADE_ROUNDING,
    collateral_ok: bool = True,
    maturity_dates=None,
) -> str:
    """
    One-line ACTION suggestion. Informational only - nothing here trades.
    maturity_dates (held rungs) lets the no-gap case extend past the last rung.
    """
    if asof_date is None:
        asof_date = datetime.date.today()
    usd_cash = float(usd_cash)
    buffer = float(buffer)

    if usd_cash < 0:
        return (
            "ACTION: USD cash is NEGATIVE (%s). IB debit interest exceeds the bill "
            "yield - do not roll the next maturing rung; let it land as cash."
            % format(round(usd_cash), ",")
        )

    if not collateral_ok:
        return (
            "ACTION: none. Bills do not appear to be fully credited toward margin "
            "(see collateral check) - keep cash against margin until this is understood."
        )

    spare = deployable_cash(usd_cash, buffer)
    amount = round_down_to(spare, rounding)
    if amount < rounding or amount <= 0:
        if usd_cash < buffer:
            return (
                "ACTION: none - cash (%s) is below the buffer (%s). Let the next "
                "maturing rung land as cash rather than rolling it."
                % (format(round(usd_cash), ","), format(round(buffer), ","))
            )
        return (
            "ACTION: none. Deployable cash (%s) is under the trade rounding."
            % format(round(spare), ",")
        )

    target, why = next_rung_month(
        maturity_dates or [], asof_date=asof_date, months=months, gaps=gaps
    )
    return (
        "ACTION: buy ~%s of a bill maturing around %s (%s). Deployable = cash %s "
        "(net of negative currency balances) - buffer %s."
        % (
            format(round(amount), ","),
            target,
            why,
            format(round(usd_cash), ","),
            format(round(buffer), ","),
        )
    )


def get_margin_summary(data: dataBlob, account_id: str, base_currency: str) -> dict:
    """
    Margin-related account tags for ONE account (the configured trading
    account, so a linked IRA etc. is not mixed in). Values in base currency.
    Missing tags come back as NaN.
    """
    ib = data.ib_conn.ib
    rows = ib.accountSummary(account=account_id)
    out = {tag: np.nan for tag in MARGIN_SUMMARY_TAGS}
    for row in rows:
        if row.account != account_id or row.tag not in out:
            continue
        if row.currency not in (base_currency, "BASE", ""):
            continue
        try:
            out[row.tag] = float(row.value)
        except BaseException:
            pass
    return out


# ---------------------------------------------------------------------------
# Whole-ladder state: one dict shared by the report and the purchase tool so
# that both see the same numbers (cash, buffer, deployable, gaps, action).
# ---------------------------------------------------------------------------


def compute_ladder_state(data: dataBlob) -> dict:
    """
    Gather everything the ladder report / purchase tool needs. Read-only.

    Keys: settings, today, base_currency, account_id, bond_df, etf_df, gaps,
    ladder_months, balances_ok, base_cash, negative_ccys, margin, nlv, maint,
    init, excess, total_cash, bills_mv, etf_mv, daily_sd, buffer, buffer_text,
    spare, check (collateral dict), collateral_ok, rung, action.

    A broker hiccup reading balances sets balances_ok=False and an 'unknown'
    action rather than raising.
    """
    from sysproduction.data.broker import dataBroker
    from sysproduction.data.capital import dataCapital
    from sysproduction.data.currency_data import dataCurrency

    settings = get_cash_buffer_settings(data)
    today = datetime.date.today()
    currency_data = dataCurrency(data)
    base_currency = currency_data.get_base_currency()

    bond_df = get_bond_holdings_df(data)
    etf_df = get_near_cash_etf_df(data)
    gaps = ladder_gaps(
        maturity_dates_from_bond_df(bond_df), today, settings["ladder_months"]
    )

    state = dict(
        settings=settings,
        today=today,
        base_currency=base_currency,
        account_id="?",
        bond_df=bond_df,
        etf_df=etf_df,
        gaps=gaps,
        ladder_months=settings["ladder_months"],
        balances_ok=False,
        base_cash=np.nan,
        negative_ccys=[],
        margin={},
        nlv=np.nan,
        maint=np.nan,
        init=np.nan,
        excess=np.nan,
        total_cash=np.nan,
        bills_mv=float(bond_df["market_value"].sum()) if len(bond_df) else 0.0,
        etf_mv=float(etf_df["market_value"].sum()) if len(etf_df) else 0.0,
        daily_sd=np.nan,
        buffer=np.nan,
        buffer_text="",
        spare=np.nan,
        deploy_cash=np.nan,
        check=dict(haircut=np.nan, fully_counted=False, usable=False),
        collateral_ok=False,
        rung=np.nan,
        action="ACTION: unknown - broker cash balance unavailable.",
    )

    data_broker = dataBroker(data)
    try:
        balances = data_broker.broker_fx_balances()
        base_cash = float(balances.get(base_currency, 0.0))
    except BaseException:
        return state
    state["balances_ok"] = True
    state["base_cash"] = base_cash

    negative_ccys = []
    try:
        for ccy, v in balances.items():
            if ccy in (base_currency, "BASE") or float(v) >= 0:
                continue
            in_base = float(v) * currency_data.get_last_fx_rate_to_base(ccy)
            if in_base < -NEGATIVE_BALANCE_ALERT_BASE:
                negative_ccys.append(
                    "%s (~%s %s)" % (ccy, format(round(in_base), ","), base_currency)
                )
        negative_ccys = sorted(negative_ccys)
    except BaseException:
        negative_ccys = []
    state["negative_ccys"] = negative_ccys

    try:
        account_id = data_broker.get_broker_account()
        margin = get_margin_summary(data, account_id, base_currency)
    except BaseException:
        account_id = "?"
        margin = {}
    state["account_id"] = account_id
    state["margin"] = margin
    state["nlv"] = margin.get("NetLiquidation", np.nan)
    state["maint"] = margin.get("FullMaintMarginReq", np.nan)
    state["init"] = margin.get("FullInitMarginReq", np.nan)
    state["excess"] = margin.get("FullExcessLiquidity", np.nan)
    state["total_cash"] = margin.get("TotalCashValue", np.nan)

    try:
        capital_series = dataCapital(data).get_series_of_all_global_capital()["Broker"]
        daily_sd = daily_pnl_sd_from_capital_series(
            capital_series, settings["pnl_window"]
        )
    except BaseException:
        daily_sd = np.nan
    state["daily_sd"] = daily_sd

    buffer, buffer_text = cash_buffer_with_explanation(settings, daily_sd)
    state["buffer"] = buffer
    state["buffer_text"] = buffer_text
    deploy_cash = cash_available_for_bills(base_cash, state["total_cash"])
    state["deploy_cash"] = deploy_cash
    state["spare"] = deployable_cash(deploy_cash, buffer)

    check = collateral_check(state["nlv"], state["maint"], state["excess"])
    state["check"] = check
    state["collateral_ok"] = (not check["usable"]) or check["fully_counted"]

    near_cash_total = base_cash + state["bills_mv"] + state["etf_mv"]
    state["rung"] = target_rung_size(near_cash_total, buffer, settings["ladder_months"])

    state["action"] = suggested_action(
        deploy_cash,
        buffer,
        gaps,
        asof_date=today,
        months=settings["ladder_months"],
        rounding=settings["rounding"],
        collateral_ok=state["collateral_ok"],
        maturity_dates=maturity_dates_from_bond_df(bond_df),
    )
    return state


# Only flag negative non-base balances worth more than this (base currency);
# accrued-interest dust of a few dollars is not worth an alert line.
NEGATIVE_BALANCE_ALERT_BASE = 1000.0
