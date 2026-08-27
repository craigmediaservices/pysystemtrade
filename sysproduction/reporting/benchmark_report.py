"""
Benchmark report: compares my live strategy against competitor managed-futures
funds / CTA ETFs, both as-is (not vol-adjusted) and rescaled to a common vol.

Run on demand:
    from sysproduction.reporting.benchmark_report import benchmark_report
    benchmark_report()

Needs a (free) Tiingo token to fetch competitor data - set env TIINGO_API_KEY or
add `tiingo_token:` to private_config.yaml. Falls back to yfinance only if that
package is installed. See sysproduction/reporting/data/benchmarks.py.
"""

import textwrap

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from syscore.constants import arg_not_supplied
from sysdata.data_blob import dataBlob
from sysproduction.data.capital import dataCapital

from sysproduction.reporting.reporting_functions import (
    header,
    body_text,
    PdfOutputWithTempFileName,
)
from sysproduction.reporting.data.benchmarks import (
    DEFAULT_BENCHMARKS,
    DEFAULT_REFERENCE,
    CONTEXT_BENCHMARKS,
    SP500_TICKER,
    get_tiingo_token,
    update_benchmark_cache,
    get_benchmark_returns,
    performance_stats,
    vol_adjusted_returns,
    relative_stats,
    correlation_matrix,
)

OWN_LABEL = "My Strategy (live)"
DEFAULT_TARGET_VOL_PCT = 15.0

INTRO_STR = (
    "Compares the live strategy against competitor managed-futures funds / CTA "
    "ETFs. 'My Strategy' returns are ACCOUNT-LEVEL: daily P&L over the actual "
    "broker NAV (time-weighted), so they tie out to IBKR's reported return and "
    "your annual review - NOT the capped notional capital. Stats are COMPOUNDED "
    "(geometric total return, the basis IBKR and fund fact-sheets quote). Two views: "
    "'as-is' raw; 'rescaled to common vol' puts every fund at the same annualised "
    "risk for a like-for-like read. ALL funds are measured over my fund's window "
    "(same start date); columns show all-time (since my start), YTD and 12-month. "
    "'Corr S&P' = correlation to the S&P 500 (lower = better diversifier). Chart "
    "lines are labelled at their right end, stacked by final value (top = best), "
    "so no colour is needed to read them. A final pair of charts shows me vs "
    "traditional assets (SPY/AGG). DBMF is the investable proxy for the SG CTA Index."
)
INTRO_TEXT = body_text(INTRO_STR)


def _no_data_message(token, statuses):
    lines = [
        "No competitor data is available yet, so the comparison cannot be built.",
        "",
        "To enable it:",
        "  1. Get a free Tiingo token at https://www.tiingo.com (covers daily EOD).",
        "  2. Set it via env TIINGO_API_KEY=... or add `tiingo_token: <token>` to",
        "     private_config.yaml.",
        "  (Optional no-token path: `pip install yfinance` - NOT recommended on the",
        "   shared live env as it pulls extra dependencies.)",
        "",
        "Tiingo token currently detected: %s" % ("yes" if token else "NO"),
    ]
    if statuses:
        lines.append("")
        lines.append("Fetch attempt status:")
        for tkr, st in statuses.items():
            lines.append("  %s: %s" % (tkr, st))
    return body_text("\n".join(lines))


def _own_daily_returns(data):
    # Account-level time-weighted return: daily P&L / prior-day actual BROKER NAV.
    # NB the usual get_daily_perc_pandl divides by the *capped notional* capital
    # ('Actual', held near the $1M cap by the half-compounding method), which
    # path-distorts the cumulative total vs reality. Dividing by the real broker
    # account value ('Broker') reproduces IBKR's reported return (YTD ties to ~0.2pp).
    cap = dataCapital(data)
    g = cap.get_series_of_all_global_capital()
    if g is None or len(g) == 0 or "Broker" not in getattr(g, "columns", []):
        return pd.Series(dtype="float64", name=OWN_LABEL)
    g = g.sort_index()
    g.index = pd.to_datetime(g.index)
    daily = g.groupby(g.index.normalize()).last()
    pnl = daily["Accumulated"].diff()  # pure daily P&L $ (excludes deposits)
    nav_prev = daily["Broker"].shift(1)  # actual account value at start of day
    r = (pnl / nav_prev).replace([np.inf, -np.inf], np.nan).dropna()
    return r.rename(OWN_LABEL)


def _build_panel(data):
    own = _own_daily_returns(data)
    bench = get_benchmark_returns()
    if bench.empty:
        return pd.DataFrame(), {}
    bench = bench.rename(columns={c: c for c in bench.columns})
    panel = pd.concat([own, bench], axis=1)
    label_map = {OWN_LABEL: OWN_LABEL}
    label_map.update(DEFAULT_BENCHMARKS)
    # column order: my strategy first
    cols = [OWN_LABEL] + [c for c in panel.columns if c != OWN_LABEL]
    return panel[cols], label_map


# Okabe-Ito colour-blind-safe palette; My Strategy is always black + bold. Lines
# also vary by style so they stay distinguishable without relying on colour.
_PALETTE = [
    "#E69F00",
    "#56B4E9",
    "#009E73",
    "#F0E442",
    "#0072B2",
    "#D55E00",
    "#CC79A7",
    "#999999",
]
_LINESTYLES = ["-", "--", "-.", ":"]
_HIGHLIGHT = "#fde9d9"  # My Strategy row shade in tables
_HEADER_BG = "#2c3e50"


def _short(label):
    return str(label).split(" (")[0]


def _assign_styles(columns):
    """Per-fund (colour, linestyle); My Strategy is black/solid/bold."""
    styles, i = {}, 0
    for col in columns:
        if col == OWN_LABEL:
            styles[col] = dict(color="#000000", ls="-", lw=3.4, z=10)
        else:
            styles[col] = dict(
                color=_PALETTE[i % len(_PALETTE)],
                ls=_LINESTYLES[(i // len(_PALETTE)) % len(_LINESTYLES)],
                lw=1.7,
                z=3,
            )
            i += 1
    return styles


def _plot_series(returns_df, styles, transform):
    """Plot each transformed series; return [(final_value, x, y, ticker, color, is_own)]."""
    ends = []
    for col in returns_df.columns:
        series = transform(returns_df[col].dropna())
        if series is None or series.empty:
            continue
        st = styles[col]
        plt.plot(
            series.index,
            series.values,
            color=st["color"],
            linestyle=st["ls"],
            linewidth=st["lw"],
            zorder=st["z"],
        )
        ends.append(
            (
                float(series.values[-1]),
                series.index[-1],
                float(series.values[-1]),
                _short(col),
                st["color"],
                col == OWN_LABEL,
            )
        )
    return ends


def _label_lines(ends):
    """Ticker labels at each line's right end, nudged apart vertically so the
    top-to-bottom order = final-value ranking (winner on top). Colour-blind safe:
    the text itself identifies the line, no legend/colour-matching needed."""
    if not ends:
        return
    ax = plt.gca()
    ymin, ymax = ax.get_ylim()
    gap = (ymax - ymin) * 0.045
    last_y = None
    for fv, x, y, tick, color, is_own in sorted(ends, key=lambda e: e[2]):
        ty = y if last_y is None else max(y, last_y + gap)
        last_y = ty
        ax.annotate(
            tick,
            xy=(x, y),
            xytext=(x, ty),
            textcoords="data",
            va="center",
            ha="left",
            fontsize=8.5,
            fontweight="bold" if is_own else "normal",
            color=color,
            annotation_clip=False,
        )


def _finish_chart(fig, title, ylabel, ends, logscale=False):
    ax = plt.gca()
    ax.set_title(title, fontsize=13, fontweight="bold", pad=10)
    ax.set_ylabel(ylabel, fontsize=10)
    if logscale:
        ax.set_yscale("log")
    ax.grid(True, which="both", alpha=0.25, linewidth=0.5)
    _label_lines(ends)
    # leave room on the right for the end-of-line labels
    fig.subplots_adjust(left=0.07, right=0.86, top=0.91, bottom=0.1)


def _figure_cumulative(data, returns_df, styles, title, logscale=False):
    pdf_output = PdfOutputWithTempFileName(data)
    fig = plt.figure(figsize=(12, 6.5))

    def cum(r):
        if len(r) < 2:
            return None
        # 0-based cumulative return %: every line starts at exactly 0 at the
        # window start (rebased), compounded (geometric) growth of returns
        g = (1.0 + r).cumprod()
        return (g / g.iloc[0] - 1.0) * 100.0

    ends = _plot_series(returns_df, styles, cum)
    plt.axhline(0.0, color="grey", linewidth=0.8, zorder=1)
    _finish_chart(
        fig, title, "Cumulative return % (0 = start of window)", ends, logscale=logscale
    )
    return pdf_output.save_chart_close_and_return_figure()


def _figure_text_page(
    data, title, body, fontsize=11, figsize=(12, 6), mono=False, wrap_width=None
):
    """Render a title + text block as a PDF page (intro / status)."""
    pdf_output = PdfOutputWithTempFileName(data)
    fig = plt.figure(figsize=figsize)
    if wrap_width:
        body = "\n".join(
            textwrap.fill(line, wrap_width) if line.strip() else line
            for line in body.split("\n")
        )
    fig.text(0.06, 0.9, title, fontsize=15, fontweight="bold", va="top")
    fig.text(
        0.06,
        0.78,
        body,
        fontsize=fontsize,
        va="top",
        family=("monospace" if mono else "sans-serif"),
    )
    plt.axis("off")
    return pdf_output.save_chart_close_and_return_figure()


def _figure_corr_matrix(data, corr_df, title):
    """Correlation grid. Greyscale shading (luminance, colour-blind safe) with the
    numbers printed so colour is never required to read it."""
    pdf_output = PdfOutputWithTempFileName(data)
    n = len(corr_df)
    fig, ax = plt.subplots(figsize=(2.0 + 0.85 * n, 1.8 + 0.7 * n))
    ax.set_title(title, fontsize=13, fontweight="bold", pad=14)
    vals = corr_df.values.astype(float)
    ax.imshow(vals, cmap="Greys", vmin=0.0, vmax=1.0, aspect="auto")
    ax.set_xticks(range(n))
    ax.set_xticklabels(corr_df.columns, rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(n))
    ax.set_yticklabels(corr_df.index, fontsize=8)
    for i in range(n):
        for j in range(n):
            v = vals[i, j]
            ax.text(
                j,
                i,
                "%.2f" % v,
                ha="center",
                va="center",
                fontsize=8,
                color="white" if v > 0.6 else "black",
            )
    fig.subplots_adjust(left=0.14, right=0.99, top=0.88, bottom=0.16)
    return pdf_output.save_chart_close_and_return_figure()


def _figure_table_page(data, title, df, fontsize=9):
    """Render a DataFrame as a clean grid table on a PDF page."""
    pdf_output = PdfOutputWithTempFileName(data)
    disp = df.copy()
    disp.index = [_short(i) for i in disp.index]
    n_rows = len(disp)
    fig, ax = plt.subplots(figsize=(13, 1.8 + 0.5 * max(n_rows, 1)))
    ax.axis("off")
    ax.set_title(title, fontsize=13, fontweight="bold", loc="left", pad=20)

    cell_text = [["" if pd.isna(v) else str(v) for v in row] for row in disp.values]
    tbl = ax.table(
        cellText=cell_text,
        rowLabels=disp.index.tolist(),
        colLabels=disp.columns.tolist(),
        cellLoc="center",
        rowLoc="left",
        loc="center",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(fontsize)
    tbl.scale(1.0, 1.6)
    try:
        tbl.auto_set_column_width(col=list(range(len(disp.columns))))
    except Exception:
        pass

    row_of = {name: i for i, name in enumerate(disp.index.tolist())}
    for (r, c), cell in tbl.get_celld().items():
        cell.set_edgecolor("#cccccc")
        if r == 0:
            cell.set_facecolor(_HEADER_BG)
            cell.set_text_props(color="white", fontweight="bold")
        elif c == -1:
            cell.set_text_props(fontweight="bold")

    own_short = _short(OWN_LABEL)
    if own_short in row_of:
        r = row_of[own_short] + 1  # +1 for header row
        for c in range(-1, len(disp.columns)):
            try:
                tbl[r, c].set_facecolor(_HIGHLIGHT)
            except KeyError:
                pass

    return pdf_output.save_chart_close_and_return_figure()


def benchmark_report(
    data: dataBlob = arg_not_supplied,
    target_vol_pct: float = DEFAULT_TARGET_VOL_PCT,
):
    if data is arg_not_supplied:
        data = dataBlob()

    token = get_tiingo_token(data)
    statuses = update_benchmark_cache(token=token)

    panel, label_map = _build_panel(data)
    if panel.empty or OWN_LABEL not in panel.columns or panel[OWN_LABEL].dropna().empty:
        # all-text report (framework allows all-text OR all-figure, never mixed)
        return [
            header("Benchmark vs competitors report"),
            INTRO_TEXT,
            _no_data_message(token, statuses),
        ]

    # only keep benchmark columns that actually have data
    panel = panel[[c for c in panel.columns if panel[c].dropna().shape[0] >= 2]]

    reference = DEFAULT_REFERENCE if DEFAULT_REFERENCE in panel.columns else None
    if reference is None:
        non_own = [c for c in panel.columns if c != OWN_LABEL]
        reference = non_own[0] if non_own else OWN_LABEL

    # common window: truncate every peer to my live period -> apples-to-apples
    own_valid = panel[OWN_LABEL].dropna()
    live_start, live_end = own_valid.index.min(), own_valid.index.max()
    common = panel.loc[live_start:live_end]

    # traditional-asset context (SPY/AGG): their own chart set + SPY corr column
    ctx_returns = get_benchmark_returns(tickers=list(CONTEXT_BENCHMARKS.keys()))
    spy_ret = (
        ctx_returns[SP500_TICKER]
        if (not ctx_returns.empty and SP500_TICKER in ctx_returns.columns)
        else None
    )
    ctx_panel = pd.concat([common[OWN_LABEL], ctx_returns], axis=1).loc[
        live_start:live_end
    ]
    ctx_panel = ctx_panel[
        [OWN_LABEL] + [c for c in CONTEXT_BENCHMARKS if c in ctx_panel.columns]
    ]
    ctx_label_map = {OWN_LABEL: OWN_LABEL}
    ctx_label_map.update(CONTEXT_BENCHMARKS)
    ctx_scaled = vol_adjusted_returns(ctx_panel, target_vol=target_vol_pct / 100.0)

    # EVERYTHING is measured over my fund's window (no mixed per-fund start dates)
    common_scaled = vol_adjusted_returns(common, target_vol=target_vol_pct / 100.0)
    common_stats = performance_stats(common, label_map=label_map, corr_to=spy_ret)
    scaled_stats = performance_stats(common_scaled, label_map=label_map)
    rel = relative_stats(common, reference, label_map=label_map)
    corr_mat = correlation_matrix(common, label_map=label_map)

    fetch_lines = [
        "Data fetch status (Tiingo token detected: %s):" % ("yes" if token else "NO")
    ]
    for tkr, st in statuses.items():
        fetch_lines.append("  %s: %s" % (tkr, st))

    # last-12-months cumulative charts: rebased to 100 twelve months ago, so every
    # fund starts at the SAME 0-based point (unlike a rolling trailing-return line,
    # which sits at each fund's own return level).
    cutoff_12m = live_end - pd.Timedelta(days=365)
    last12 = common.loc[cutoff_12m:]
    last12_scaled = vol_adjusted_returns(last12, target_vol=target_vol_pct / 100.0)
    styles = _assign_styles(panel.columns)
    ctx_styles = _assign_styles(ctx_panel.columns)

    # The framework merges a report into ONE emailed PDF only if EVERY item is a
    # figure (it rejects mixed figures+tables). So render intro/tables/status as
    # PDF pages too -> a single attached PDF, exactly like the account curve report.
    return [
        _figure_text_page(
            data,
            "Benchmark vs competitors report",
            INTRO_STR,
            fontsize=11,
            wrap_width=95,
        ),
        _figure_table_page(
            data,
            "Performance - my fund's window (%s to %s), all funds same dates"
            % (live_start.date(), live_end.date()),
            common_stats,
        ),
        _figure_table_page(
            data,
            "Performance - rescaled to %.0f%% annual vol (comparable risk), my fund's window"
            % target_vol_pct,
            scaled_stats,
        ),
        _figure_table_page(
            data,
            "Correlation / beta / tracking error vs %s"
            % label_map.get(reference, reference),
            rel,
        ),
        _figure_corr_matrix(
            data,
            corr_mat,
            "Correlation matrix - how alike the funds are (my fund's window)",
        ),
        _figure_cumulative(
            data,
            common,
            styles,
            "Cumulative return vs MF peers - as-is (from 0%% at %s)"
            % live_start.date(),
        ),
        _figure_cumulative(
            data,
            common_scaled,
            styles,
            "Cumulative return vs MF peers - all rescaled to %.0f%% vol"
            % target_vol_pct,
        ),
        _figure_cumulative(
            data,
            last12,
            styles,
            "Last 12 months vs MF peers - as-is (from 0%% at %s)" % cutoff_12m.date(),
        ),
        _figure_cumulative(
            data,
            last12_scaled,
            styles,
            "Last 12 months vs MF peers - rescaled to %.0f%% vol (from 0%% at %s)"
            % (target_vol_pct, cutoff_12m.date()),
        ),
        _figure_cumulative(
            data,
            ctx_panel,
            ctx_styles,
            "Cumulative return vs traditional assets (SPY/AGG) - as-is",
        ),
        _figure_cumulative(
            data,
            ctx_scaled,
            ctx_styles,
            "Cumulative return vs traditional assets (SPY/AGG) - rescaled to %.0f%% vol"
            % target_vol_pct,
        ),
        _figure_text_page(
            data,
            "Data fetch status",
            "\n".join(fetch_lines),
            fontsize=10,
            mono=True,
        ),
    ]


if __name__ == "__main__":
    benchmark_report()
