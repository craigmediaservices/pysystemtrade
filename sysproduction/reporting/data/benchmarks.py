"""
Benchmark / competitor data layer for the benchmark report.

Pulls daily total-return-adjusted prices for a configurable set of competitor
funds (managed-futures / CTA ETFs plus context benchmarks) and caches them to
local CSVs under private/benchmark_data/ so the report runs offline after the
first fetch.

Data source priority (chosen for a SHARED live-trading system Python env, so we
add NO heavyweight dependencies):
  1. Tiingo REST API via `requests` (already a dependency). Needs a free token.
  2. yfinance, IFF the package happens to be installed (lazy import, never
     required). We deliberately do NOT install it into the live env.

Token resolution (first hit wins):
  - env var TIINGO_API_KEY  (or TIINGO_TOKEN)
  - `tiingo_token:` in private_config.yaml (production config)

Note on the SG indices: the gold-standard CTA benchmarks (SG Trend Index,
SG CTA Index, BTOP50) are proprietary and not available via any free feed, so
they cannot be auto-fetched here. DBMF is included because it is explicitly
designed to replicate the SG CTA Index, making it the investable stand-in.
"""

import os
from io import StringIO
from pathlib import Path
import datetime

import numpy as np
import pandas as pd
import requests

from syscore.constants import arg_not_supplied
from syscore.dateutils import BUSINESS_DAYS_IN_YEAR, ROOT_BDAYS_INYEAR

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CACHE_DIR = REPO_ROOT / "private" / "benchmark_data"

# ticker -> human-friendly label shown in the report.
# Pure managed-futures / trend-following ETFs only (no SPY/AGG, and not RSST,
# which is return-stacked S&P 500 + managed futures and so carries equity beta).
DEFAULT_BENCHMARKS = {
    "DBMF": "DBMF (iMGP DBi Mgd Futures, ~SG CTA proxy)",
    "KMLM": "KMLM (KraneShares Mt Lucas)",
    "CTA": "CTA (Simplify Mgd Futures)",
    "WTMF": "WTMF (WisdomTree Mgd Futures)",
    "FMF": "FMF (First Trust Mgd Futures)",
    "TFPN": "TFPN (Blueprint Chesapeake Trend)",
    "AHLT": "AHLT (American Beacon AHL Trend)",
    "ASMF": "ASMF (Virtus AlphaSimplex Mgd Futures)",
    "MFUT": "MFUT (Cambria Chesapeake Trend)",
}

# DBMF first so it is the default reference for beta/correlation/tracking error
DEFAULT_REFERENCE = "DBMF"

# Traditional-asset context: NOT shown alongside the MF peers, only in their own
# "vs traditional assets" chart set, and SPY is used for the correlation column.
CONTEXT_BENCHMARKS = {
    "SPY": "SPY (S&P 500)",
    "AGG": "AGG (US Aggregate Bonds)",
}
SP500_TICKER = "SPY"

TIINGO_URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"
DEFAULT_LOOKBACK_YEARS = 15
# re-request the last few cached days each run to pick up Tiingo adjClose revisions
REFETCH_TAIL_DAYS = 5

# .env files searched for an existing TIINGO_API_KEY, so the token lives in one
# place (the user already keeps it in ~/cryptopulse/.env) rather than duplicated.
DEFAULT_ENV_FILE_CANDIDATES = [
    "~/cryptopulse/.env",
    "~/.env",
    "~/gpm/.env",
]


# ---------------------------------------------------------------------------
# token
# ---------------------------------------------------------------------------
def _read_token_from_env_file(path):
    try:
        p = Path(path).expanduser()
        if not p.exists():
            return None
        for line in p.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip().upper() in ("TIINGO_API_KEY", "TIINGO_TOKEN"):
                val = val.strip().strip('"').strip("'")
                if val:
                    return val
    except Exception:
        return None
    return None


def get_tiingo_token(data=arg_not_supplied):
    # 1. environment
    token = os.environ.get("TIINGO_API_KEY") or os.environ.get("TIINGO_TOKEN")
    if token:
        return token.strip()

    # 2. private_config: explicit token, or a path to a .env file holding it
    config_env_file = None
    try:
        from sysdata.config.production_config import get_production_config

        config = get_production_config()
        try:
            token = config.get_element("tiingo_token")
            if token:
                return str(token).strip()
        except Exception:
            pass
        try:
            config_env_file = config.get_element("tiingo_env_file")
        except Exception:
            config_env_file = None
    except Exception:
        pass

    # 3. .env files (configured path first, then known defaults)
    candidates = ([config_env_file] if config_env_file else []) + (
        DEFAULT_ENV_FILE_CANDIDATES
    )
    for path in candidates:
        tok = _read_token_from_env_file(path)
        if tok:
            return tok

    return None


# ---------------------------------------------------------------------------
# fetch (one ticker)
# ---------------------------------------------------------------------------
def _fetch_tiingo(ticker, token, start_date):
    # token goes in the Authorization header, NOT the query string, so it never
    # lands in urllib3/request URL logs
    params = {
        "startDate": start_date.strftime("%Y-%m-%d"),
        "format": "csv",
        "resampleFreq": "daily",
    }
    headers = {"Authorization": "Token %s" % token}
    resp = requests.get(
        TIINGO_URL.format(ticker=ticker), params=params, headers=headers, timeout=30
    )
    resp.raise_for_status()
    text = resp.text.strip()
    if not text or "date" not in text.splitlines()[0]:
        return None
    df = pd.read_csv(StringIO(text), parse_dates=["date"]).set_index("date")
    df.index = df.index.tz_localize(None).normalize()
    if "adjClose" not in df.columns:
        return None
    return df[["adjClose"]].rename(columns={"adjClose": "price"}).dropna()


def _fetch_yfinance(ticker, start_date):
    try:
        import yfinance as yf  # lazy: never a hard dependency
    except ImportError:
        return None
    try:
        df = yf.download(ticker, start=start_date, progress=False, auto_adjust=True)
    except Exception:
        return None
    if df is None or len(df) == 0:
        return None
    close = df["Close"]
    if isinstance(close, pd.DataFrame):  # yfinance multiindex quirk
        close = close.iloc[:, 0]
    out = pd.DataFrame({"price": close})
    out.index = pd.to_datetime(out.index).tz_localize(None).normalize()
    out.index.name = "date"
    return out.dropna()


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------
def _cache_path(ticker, cache_dir):
    return Path(cache_dir) / ("%s.csv" % ticker.upper())


def _load_one(ticker, cache_dir):
    path = _cache_path(ticker, cache_dir)
    if not path.exists():
        return None
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
    return df.sort_index()


def update_benchmark_cache(
    tickers=arg_not_supplied,
    token=arg_not_supplied,
    cache_dir=DEFAULT_CACHE_DIR,
    lookback_years=DEFAULT_LOOKBACK_YEARS,
):
    """
    Best-effort refresh of the local cache. Returns {ticker: status_string}.
    Never raises on a single-ticker failure - records it and moves on.
    """
    if tickers is arg_not_supplied:
        tickers = list(DEFAULT_BENCHMARKS.keys()) + list(CONTEXT_BENCHMARKS.keys())
    if token is arg_not_supplied:
        token = get_tiingo_token()

    os.makedirs(cache_dir, exist_ok=True)
    today = datetime.date.today()
    statuses = {}

    for ticker in tickers:
        existing = _load_one(ticker, cache_dir)
        if existing is not None and len(existing):
            start_date = (
                existing.index.max() - pd.Timedelta(days=REFETCH_TAIL_DAYS)
            ).date()
        else:
            start_date = today - datetime.timedelta(days=int(lookback_years * 365.25))

        fetched, source = None, None
        if token:
            try:
                fetched = _fetch_tiingo(ticker, token, start_date)
                source = "tiingo"
            except Exception as e:
                statuses[ticker] = "tiingo error: %s" % e
        if fetched is None or len(fetched) == 0:
            yf = _fetch_yfinance(ticker, start_date)
            if yf is not None and len(yf):
                fetched, source = yf, "yfinance"

        if fetched is None or len(fetched) == 0:
            if ticker not in statuses:
                statuses[ticker] = (
                    "no data (no token / yfinance not installed)"
                    if not token
                    else "no data returned"
                )
            continue

        if existing is not None and len(existing):
            combined = pd.concat([existing, fetched])
            combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        else:
            combined = fetched.sort_index()

        combined.to_csv(_cache_path(ticker, cache_dir), index_label="date")
        statuses[ticker] = "%s rows via %s (to %s)" % (
            len(combined),
            source,
            combined.index.max().date(),
        )

    return statuses


def get_benchmark_prices(tickers=arg_not_supplied, cache_dir=DEFAULT_CACHE_DIR):
    if tickers is arg_not_supplied:
        tickers = list(DEFAULT_BENCHMARKS.keys())
    frames = {}
    for ticker in tickers:
        df = _load_one(ticker, cache_dir)
        if df is not None and len(df):
            frames[ticker] = df["price"]
    if not frames:
        return pd.DataFrame()
    return pd.DataFrame(frames).sort_index()


def get_benchmark_returns(tickers=arg_not_supplied, cache_dir=DEFAULT_CACHE_DIR):
    prices = get_benchmark_prices(tickers=tickers, cache_dir=cache_dir)
    if prices.empty:
        return prices
    return prices.pct_change()


# ---------------------------------------------------------------------------
# stats (operate column-wise; each column uses its own full history)
#
# RETURNS are COMPOUNDED (geometric) - the basis IBKR and fund fact-sheets quote,
# so cumulative figures tie IBKR's reported time-weighted return:
#   total / YTD / 12M = (1+r).prod() - 1          (compounded total return)
#   drawdown          = on the (1+r).cumprod() curve
# RISK-ADJUSTED metrics use the STANDARD arithmetic basis, so Sharpe matches the
# textbook definition and the annual review (~0.81):
#   ann return = BUSINESS_DAYS_IN_YEAR * mean     (256 * mean, arithmetic)
#   ann vol    = ROOT_BDAYS_INYEAR    * std       (16 * std)
#   Sharpe     = ann return / ann vol
# Applied identically to my fund and every peer, so it stays apples-to-apples.
# ---------------------------------------------------------------------------
def _ann_vol(returns):
    return returns.std() * ROOT_BDAYS_INYEAR


def _drawdown(returns):
    # drawdown on the compounded growth curve, as a fraction of the prior peak
    curve = (1 + returns).cumprod()
    return curve / curve.cummax() - 1


def _period_return(returns, start):
    window = returns[returns.index >= pd.Timestamp(start)]
    if len(window) == 0:
        return np.nan
    return (1 + window).prod() - 1


def performance_stats(returns_df, label_map=None, corr_to=None):
    """One row per series, columns = metrics. Each series uses its own history.

    corr_to: optional daily-returns Series (e.g. SPY) -> adds a 'Corr S&P' column
    = each fund's correlation to it over the overlapping dates.
    """
    label_map = label_map or {}
    today = pd.Timestamp(datetime.date.today())
    ytd_start = pd.Timestamp(datetime.date(today.year, 1, 1))
    rows = []
    for col in returns_df.columns:
        r = returns_df[col].dropna()
        if len(r) < 2:
            continue
        n = len(r)
        years = n / BUSINESS_DAYS_IN_YEAR
        total = (1 + r).prod() - 1  # compounded total return (geometric)
        # arithmetic annualised return for the STANDARD Sharpe (ties annual review)
        ann_ret = r.mean() * BUSINESS_DAYS_IN_YEAR
        vol = _ann_vol(r)
        sharpe = ann_ret / vol if vol > 0 else np.nan
        downside = r[r < 0].std() * ROOT_BDAYS_INYEAR
        sortino = ann_ret / downside if downside and downside > 0 else np.nan
        dd = _drawdown(r)
        row = {
            "Fund": label_map.get(col, col),
            "From": r.index.min().date().isoformat(),
            "Yrs": round(years, 1),
            "Total %": round(total * 100, 1),
            "AnnRet %": round(ann_ret * 100, 1),
            "Vol %": round(vol * 100, 1),
            "Sharpe": round(sharpe, 2),
            "Sortino": round(sortino, 2),
            "MaxDD %": round(dd.min() * 100, 1),
            "CurDD %": round(dd.iloc[-1] * 100, 1),
            "YTD %": round(_period_return(r, ytd_start) * 100, 1),
            "12M %": round(_period_return(r, today - pd.Timedelta(days=365)) * 100, 1),
        }
        if corr_to is not None:
            pair = pd.concat([r, corr_to], axis=1).dropna()
            row["Corr S&P"] = (
                round(pair.iloc[:, 0].corr(pair.iloc[:, 1]), 2)
                if len(pair) >= 20
                else np.nan
            )
        rows.append(row)
    out = pd.DataFrame(rows)
    if len(out):
        out = out.set_index("Fund")
    return out


def vol_adjusted_returns(returns_df, target_vol=0.15):
    """Rescale each column to a common annualised vol so curves are comparable."""
    scaled = returns_df.copy()
    for col in scaled.columns:
        realized = _ann_vol(scaled[col].dropna())
        if realized and realized > 0:
            scaled[col] = scaled[col] * (target_vol / realized)
    return scaled


def relative_stats(returns_df, reference, label_map=None):
    """Correlation / beta / tracking error of each series vs the reference series."""
    label_map = label_map or {}
    if reference not in returns_df.columns:
        return pd.DataFrame()
    rows = []
    for col in returns_df.columns:
        if col == reference:
            r = returns_df[col].dropna()
            rows.append(
                {
                    "Fund": label_map.get(col, col),
                    "Corr": 1.0,
                    "Beta": 1.0,
                    "TrackErr %": 0.0,
                    "Overlap days": len(r),
                }
            )
            continue
        pair = returns_df[[col, reference]].dropna()
        if len(pair) < 20:
            continue
        a, b = pair[col], pair[reference]
        corr = a.corr(b)
        beta = a.cov(b) / b.var() if b.var() > 0 else np.nan
        te = (a - b).std() * ROOT_BDAYS_INYEAR
        rows.append(
            {
                "Fund": label_map.get(col, col),
                "Corr": round(corr, 2),
                "Beta": round(beta, 2),
                "TrackErr %": round(te * 100, 1),
                "Overlap days": len(pair),
            }
        )
    out = pd.DataFrame(rows)
    if len(out):
        out = out.set_index("Fund")
    return out


def correlation_matrix(returns_df, label_map=None):
    """Pairwise correlation matrix among all funds (short ticker labels)."""
    label_map = label_map or {}
    short = [str(label_map.get(c, c)).split(" (")[0] for c in returns_df.columns]
    corr = returns_df.corr()  # pandas pairwise (complete observations per pair)
    corr.index = short
    corr.columns = short
    return corr.round(2)
