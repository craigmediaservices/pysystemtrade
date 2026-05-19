"""
Score duplicate-market candidates with extra heuristics beyond the built-in
"smallest contract that passes hard filters" rule.

Background
----------
``sysproduction.reporting.data.duplicate_remove_markets`` applies three hard
filters (SR_cost, daily volume contracts, daily volume risk) and then picks
the leg with the smallest contract_size. That treats "barely passes the
filter" as equivalent to "passes by 1000x", and ignores both SR cost and
target capital scale. For users with a hard capital cap, that produces
recommendations that look right on paper but are operationally fragile.

This script wraps the same underlying scoring tables and adds four derived
signals so the user can make a judgement call from the data instead of
prose:

* ``liq_score``    log10(vol_risk / min_filter) -- safety margin above the
                   hard liquidity floor. Higher is safer.
* ``cap_fit``      banding of contract_size / capital. "ok" if the contract
                   represents 0.1%-2% of capital (enough granularity without
                   wasting commissions on micro-positions).
* ``cost_ratio``   SR_cost relative to the cheapest leg in the same pair.
                   Flags legs that buy granularity at a cost penalty.
* ``flag``         actionable composite -- one of "thin-vs-alt",
                   "cost-loser", "too-big", "too-small", "liq-leader",
                   "no-data", or "ok".

The script:

1. Reads pair groups from ``config.duplicate_instruments`` if populated.
2. Optionally auto-discovers candidate pairs from the user's instrument
   universe using common naming-suffix heuristics
   (``--auto-discover``).
3. For each candidate pair, calls
   ``duplicate_remove_markets.get_df_of_data_for_duplicate`` and
   ``get_best_market`` so the "tool recommends" column is whatever the
   nightly duplicate-market report would say.
4. Emits a console table plus optional CSV.

It modifies nothing.
"""

import argparse
import math
import sys
from typing import Optional

import numpy as np
import pandas as pd

from sysdata.config.instruments import generate_matching_duplicate_dict
from sysdata.data_blob import dataBlob
from sysproduction.data.capital import dataCapital
from sysproduction.reporting.data.constants import (
    DUPLICATE_CAP_FIT_MAX,
    DUPLICATE_CAP_FIT_MIN,
    DUPLICATE_COST_LOSER_RATIO,
    DUPLICATE_LIQ_DOMINANT_RATIO,
)
from sysproduction.reporting.data.duplicate_remove_markets import (
    get_bad_market_filter_parameters,
    get_data_for_markets,
    get_best_market,
    get_df_of_data_for_duplicate,
    no_good_markets,
)


# Suffix patterns we strip when auto-discovering duplicate pairs. Order
# matters: more specific suffixes first.
AUTO_DISCOVER_SUFFIXES = (
    "_mini",
    "_micro",
    "-mini",
    "-micro",
    "-SGX-TITAN",
    "-SGX-mini",
    "-SGX",
    "-DJ",
    "-onshore",
    "-LAST",
    "-PEN",
    "_W",
)


def main() -> None:
    args = _parse_args()
    data = dataBlob(log_name="score_duplicate_markets")
    capital = _resolve_capital(data, args.capital)
    print(f"Using capital: {capital:,.0f}", file=sys.stderr)

    pair_groups = _collect_pair_groups(
        data, auto_discover=args.auto_discover, extra_pairs=args.pair
    )
    if not pair_groups:
        print(
            "No duplicate-market pairs found. Either populate "
            "config.duplicate_instruments or pass --auto-discover.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(
        f"Scoring {len(pair_groups)} pair group(s) "
        f"({sum(len(v['legs']) for v in pair_groups.values())} legs total).",
        file=sys.stderr,
    )

    mkt_data = get_data_for_markets(data)
    filters = get_bad_market_filter_parameters()
    min_risk = filters[2]
    print(
        f"Filters: max_SR_cost={filters[0]}  "
        f"min_volume_contracts={filters[1]}  "
        f"min_volume_risk={min_risk}",
        file=sys.stderr,
    )

    rows = _score_all_pairs(
        mkt_data=mkt_data,
        pair_groups=pair_groups,
        filters=filters,
        capital=capital,
        min_risk=min_risk,
    )

    df = pd.DataFrame(rows)
    if args.output_csv:
        df.to_csv(args.output_csv, index=False)
        print(f"Wrote {args.output_csv}", file=sys.stderr)

    _print_console_table(df)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--capital",
        type=float,
        default=None,
        help="Capital base for cap-fit scoring (default: production capital).",
    )
    parser.add_argument(
        "--auto-discover",
        action="store_true",
        help=(
            "Also discover candidate pairs by stripping common suffixes "
            "(_mini, _micro, -SGX, -DJ, etc.) from the user's instrument "
            "universe and grouping by stripped root."
        ),
    )
    parser.add_argument(
        "--pair",
        action="append",
        default=[],
        metavar="LEG_A,LEG_B[,...]",
        help=(
            "Add an explicit pair group. Repeat the flag for multiple "
            "groups. Example: --pair CRUDE_W,CRUDE_W_mini "
            "--pair GOLD,GOLD_micro,GOLD-mini"
        ),
    )
    parser.add_argument(
        "--output-csv",
        default=None,
        metavar="PATH",
        help="Also write the full scoring table to this CSV path.",
    )
    return parser.parse_args()


def _resolve_capital(data: dataBlob, override: Optional[float]) -> float:
    if override is not None:
        return override
    try:
        return float(dataCapital(data).get_current_total_capital())
    except Exception:
        # Capital lookup can fail in non-production environments; fall back
        # to a recognisable sentinel so the cap-fit column is interpretable.
        return 1_000_000.0


def _collect_pair_groups(
    data: dataBlob, auto_discover: bool, extra_pairs: list
) -> dict:
    groups: dict = {}

    configured = generate_matching_duplicate_dict(config=data.config)
    for key, entry in configured.items():
        legs = list(entry["included"]) + list(entry["excluded"])
        legs = [leg for leg in legs if leg]
        if len(legs) < 2:
            continue
        groups[key] = {
            "legs": legs,
            "user_pick": entry["included"][0] if entry["included"] else None,
            "source": "config",
        }

    for raw in extra_pairs:
        legs = [leg.strip() for leg in raw.split(",") if leg.strip()]
        if len(legs) < 2:
            continue
        key = f"cli:{legs[0]}"
        groups[key] = {"legs": legs, "user_pick": legs[0], "source": "cli"}

    if auto_discover:
        for key, legs in _auto_discover_pairs(data).items():
            if key not in groups:
                groups[key] = {"legs": legs, "user_pick": None, "source": "auto"}

    return groups


def _auto_discover_pairs(data: dataBlob) -> dict:
    universe = sorted(set(_universe_from_config(data) or _universe_from_data(data)))
    by_root: dict = {}
    for code in universe:
        root = _strip_suffix(code)
        by_root.setdefault(root, []).append(code)
    return {f"auto:{root}": legs for root, legs in by_root.items() if len(legs) > 1}


def _strip_suffix(code: str) -> str:
    for suffix in AUTO_DISCOVER_SUFFIXES:
        if code.endswith(suffix) and len(code) > len(suffix):
            return code[: -len(suffix)]
    return code


def _universe_from_config(data: dataBlob) -> list:
    try:
        weights = data.config.get_element("instrument_weights")
    except Exception:
        return []
    return _flatten_weight_leaves(weights)


def _flatten_weight_leaves(node) -> list:
    out: list = []
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, dict):
                out.extend(_flatten_weight_leaves(value))
            elif isinstance(value, (int, float)) and key != "weight":
                out.append(key)
    return out


def _universe_from_data(data: dataBlob) -> list:
    from sysproduction.data.prices import diagPrices

    try:
        return list(diagPrices(data).get_list_of_instruments_in_multiple_prices())
    except Exception:
        return []


def _score_all_pairs(
    mkt_data: tuple,
    pair_groups: dict,
    filters: tuple,
    capital: float,
    min_risk: float,
) -> list:
    rows: list = []
    for pair_key, info in pair_groups.items():
        legs = info["legs"]
        df = get_df_of_data_for_duplicate(mkt_data, legs)
        best = get_best_market(df, filters)
        cheapest_cost = _min_finite(df, "SR_cost")
        vol_risks = sorted(_finite_values(df, "volume_risk"), reverse=True)
        max_vol_risk = vol_risks[0] if vol_risks else None
        second_vol_risk = vol_risks[1] if len(vol_risks) > 1 else None

        for leg in legs:
            leg_row = df.loc[leg].to_dict() if leg in df.index else {}
            rows.append(
                _row_for_leg(
                    pair_key=pair_key,
                    leg=leg,
                    info=info,
                    leg_row=leg_row,
                    best=best,
                    cheapest_cost=cheapest_cost,
                    max_vol_risk=max_vol_risk,
                    second_vol_risk=second_vol_risk,
                    min_risk=min_risk,
                    capital=capital,
                )
            )
    return rows


def _row_for_leg(
    pair_key: str,
    leg: str,
    info: dict,
    leg_row: dict,
    best,
    cheapest_cost: Optional[float],
    max_vol_risk: Optional[float],
    second_vol_risk: Optional[float],
    min_risk: float,
    capital: float,
) -> dict:
    sr_cost = leg_row.get("SR_cost")
    vol_contracts = leg_row.get("volume_contracts")
    vol_risk = leg_row.get("volume_risk")
    contract_size = leg_row.get("contract_size")

    return {
        "pair": pair_key,
        "leg": leg,
        "user_pick": "YES" if leg == info.get("user_pick") else "",
        "tool_pick": "YES" if best is not no_good_markets and leg == best else "",
        "SR_cost": sr_cost,
        "vol_contracts": vol_contracts,
        "vol_risk": vol_risk,
        "contract_size": contract_size,
        "liq_score": _liq_score(vol_risk, min_risk),
        "cap_fit": _cap_fit(contract_size, capital),
        "cost_ratio": _cost_ratio(sr_cost, cheapest_cost),
        "flag": _flag(
            sr_cost=sr_cost,
            vol_risk=vol_risk,
            contract_size=contract_size,
            cheapest_cost=cheapest_cost,
            max_vol_risk=max_vol_risk,
            second_vol_risk=second_vol_risk,
            capital=capital,
        ),
    }


def _finite_values(df: pd.DataFrame, column: str) -> list:
    if column not in df.columns:
        return []
    return [float(v) for v in df[column].dropna().tolist()]


def _min_finite(df: pd.DataFrame, column: str) -> Optional[float]:
    values = _finite_values(df, column)
    return min(values) if values else None


def _liq_score(vol_risk: Optional[float], min_risk: float) -> Optional[float]:
    if vol_risk is None or _is_nan(vol_risk) or vol_risk <= 0:
        return None
    return round(math.log10(vol_risk / min_risk), 2)


def _cap_fit(contract_size: Optional[float], capital: float) -> str:
    if contract_size is None or _is_nan(contract_size) or capital <= 0:
        return "no-data"
    ratio = contract_size / capital
    if ratio < DUPLICATE_CAP_FIT_MIN:
        return "too-small"
    if ratio > DUPLICATE_CAP_FIT_MAX:
        return "too-big"
    return "ok"


def _cost_ratio(sr_cost: Optional[float], cheapest: Optional[float]) -> Optional[float]:
    if sr_cost is None or cheapest is None or _is_nan(sr_cost) or cheapest <= 0:
        return None
    return round(sr_cost / cheapest, 2)


def _flag(
    sr_cost: Optional[float],
    vol_risk: Optional[float],
    contract_size: Optional[float],
    cheapest_cost: Optional[float],
    max_vol_risk: Optional[float],
    second_vol_risk: Optional[float],
    capital: float,
) -> str:
    """Pick the single most actionable label for this leg.

    Priority order: missing data > liquidity gap > cost penalty > capital
    fit > "this leg dominates liquidity" > ok.
    """
    if vol_risk is None or _is_nan(vol_risk):
        return "no-data"

    if (
        max_vol_risk is not None
        and max_vol_risk > 0
        and max_vol_risk / max(vol_risk, 1e-9) >= DUPLICATE_LIQ_DOMINANT_RATIO
    ):
        return "thin-vs-alt"

    if (
        cheapest_cost is not None
        and sr_cost is not None
        and not _is_nan(sr_cost)
        and cheapest_cost > 0
        and sr_cost / cheapest_cost >= DUPLICATE_COST_LOSER_RATIO
    ):
        return "cost-loser"

    cap = _cap_fit(contract_size, capital)
    if cap != "ok":
        return cap

    if (
        second_vol_risk is not None
        and second_vol_risk > 0
        and vol_risk / second_vol_risk >= DUPLICATE_LIQ_DOMINANT_RATIO
    ):
        return "liq-leader"

    return "ok"


def _is_nan(value) -> bool:
    try:
        return bool(np.isnan(value))
    except (TypeError, ValueError):
        return False


def _print_console_table(df: pd.DataFrame) -> None:
    if df.empty:
        print("(no data)")
        return
    display_cols = [
        "pair",
        "leg",
        "user_pick",
        "tool_pick",
        "SR_cost",
        "vol_contracts",
        "vol_risk",
        "contract_size",
        "liq_score",
        "cap_fit",
        "cost_ratio",
        "flag",
    ]
    formatted = df[display_cols].copy()
    for col in ("SR_cost", "cost_ratio"):
        formatted[col] = formatted[col].apply(
            lambda v: "" if v is None or _is_nan(v) else f"{v:.4g}"
        )
    for col in ("vol_contracts", "vol_risk", "contract_size", "liq_score"):
        formatted[col] = formatted[col].apply(
            lambda v: "" if v is None or _is_nan(v) else f"{v:g}"
        )
    print(formatted.to_string(index=False))


if __name__ == "__main__":
    main()
