"""
Reproduce today's price-spike flags against IB so they can be reviewed
before running the interactive checker.

    python3 sysproduction/maintenance/spike_review.py [--date YYYY-MM-DD] [--contracts A/20270100,B/20261200]

For every contract that logged "Spike found in prices" today (or the given
list): re-fetch from IB at both frequencies, run the production spike test,
and print every over-threshold row with previous value, new value, % change
and the next three values (persistence). Writes

    private/maintenance_work/spike_candidates_<date>.txt   instr,contract,date  (one per flagged row)
    private/maintenance_work/spike_instruments_<date>.txt  instruments touched

Review the candidates, delete any row that should NOT be accepted, then run
spike_accept.py. Decision rules: a re-mark that holds on the following days
is genuine (accept); a print that reverts next day is bad (previous value);
for illiquid / dividend / weather contracts compare to the prior-year analog,
not the contract's own short history.
"""
import datetime
import glob
import os
import re
import sys

import pandas as pd

from sysdata.data_blob import dataBlob
from sysdata.tools.cleaner import get_config_for_price_filtering
from syscore.dateutils import DAILY_PRICE_FREQ
from syscore.pandas.merge_data_keeping_past_data import (
    _calculate_change_in_vol_normalised_units,
    merge_newer_data_no_checks,
)
from sysobjects.contracts import futuresContract
from sysproduction.data.broker import dataBroker
from sysproduction.data.prices import diagPrices
from sysproduction.maintenance import work_path

pd.set_option("display.width", 250)

LOG_DIR = os.path.expanduser("~/logs")


def flagged_contracts_from_logs(date: datetime.date) -> list:
    pat = re.compile(r"Spike found in prices for ([A-Z_0-9-]+)/(\d{8})")
    files = [os.path.join(LOG_DIR, "pysystemtrade.log")] + glob.glob(
        os.path.join(LOG_DIR, "pysystemtrade.log.%s" % date.isoformat())
    )
    found = set()
    prefix = date.isoformat()
    for f in files:
        if not os.path.exists(f):
            continue
        with open(f, errors="ignore") as fh:
            for line in fh:
                if line.startswith(prefix) and "Spike found" in line:
                    m = pat.search(line)
                    if m:
                        found.add((m.group(1), m.group(2)))
    return sorted(found)


def review(contracts: list, date: datetime.date) -> list:
    candidates = []
    with dataBlob(log_name="Maintenance-Spike-Review") as data:
        cfg = get_config_for_price_filtering(data)
        broker = dataBroker(data)
        dp = diagPrices(data)
        freqs = [dp.get_intraday_frequency_for_historical_download(), DAILY_PRICE_FREQ]
        print("max_price_spike =", cfg.max_price_spike)
        for ic, cd in contracts:
            c = futuresContract(ic, cd)
            for freq in freqs:
                try:
                    new = broker.get_cleaned_prices_at_frequency_for_contract_object(
                        c, freq, cleaning_config=cfg
                    )
                except Exception:
                    print("\n===== %s/%s %s: no data from IB" % (ic, cd, freq))
                    continue
                old = dp.get_prices_at_frequency_for_contract_object(c, freq)
                if len(new) == 0:
                    continue
                merged = merge_newer_data_no_checks(
                    pd.DataFrame(old), pd.DataFrame(new)
                ).merged_data
                z = _calculate_change_in_vol_normalised_units(merged["FINAL"])
                join = old.index[-1] if len(old) else None
                zz = z if join is None else z[z.index > join]
                flagged = zz[zz > cfg.max_price_spike]
                print(
                    "\n===== %s/%s %s: stored %d rows (last %s); IB %s..%s; flagged %d"
                    % (
                        ic,
                        cd,
                        freq,
                        len(old),
                        join,
                        new.index[0],
                        new.index[-1],
                        len(flagged),
                    )
                )
                idx = list(merged.index)
                for dt, score in flagged.items():
                    i = idx.index(dt)
                    prev = merged["FINAL"].iloc[i - 1]
                    cur = merged["FINAL"].iloc[i]
                    nxt = merged["FINAL"].iloc[i + 1 : i + 4].tolist()
                    print(
                        "   %s score=%6.1f prev=%-10s new=%-10s chg=%+.1f%% next3=%s vol=%s"
                        % (
                            dt,
                            score,
                            prev,
                            cur,
                            100 * (cur / prev - 1),
                            nxt,
                            merged["VOLUME"].iloc[i],
                        )
                    )
                    candidates.append((ic, cd, dt.strftime("%Y-%m-%d")))
                if len(flagged):
                    first = idx.index(flagged.index[0])
                    print(merged.iloc[max(0, first - 4) : first + 3].to_string())

    candidates = sorted(set(candidates))
    instruments = []
    for ic, _, _ in candidates:
        if ic not in instruments:
            instruments.append(ic)
    cpath = work_path("spike_candidates_%s.txt" % date.isoformat())
    ipath = work_path("spike_instruments_%s.txt" % date.isoformat())
    with open(cpath, "w") as f:
        f.write("\n".join("%s,%s,%s" % c for c in candidates))
    with open(ipath, "w") as f:
        f.write("\n".join(instruments))
    print(
        "\n%d flagged rows across %d instruments" % (len(candidates), len(instruments))
    )
    print("candidates:", cpath)
    print("instruments:", ipath)
    return candidates


if __name__ == "__main__":
    date = datetime.date.today()
    if "--date" in sys.argv:
        date = datetime.date.fromisoformat(sys.argv[sys.argv.index("--date") + 1])
    if "--contracts" in sys.argv:
        contracts = [
            tuple(x.split("/"))
            for x in sys.argv[sys.argv.index("--contracts") + 1].split(",")
        ]
    else:
        contracts = flagged_contracts_from_logs(date)
    print("contracts to review:", contracts)
    if contracts:
        review(contracts, date)
