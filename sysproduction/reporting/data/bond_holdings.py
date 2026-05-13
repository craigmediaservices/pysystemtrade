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
        face = item.position  # IB position for bonds = face value
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
