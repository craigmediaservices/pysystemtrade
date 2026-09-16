"""
Read-only dry run of interactive_update_roll_status "auto decide" logic for
every instrument within N days of expiry, plus the full roll report.

    python3 sysproduction/maintenance/roll_status_dryrun.py [--days 10]

Prints one line per instrument and an ACTIONABLE section (suggestion differs
from current state, position in the priced contract, 'Ask', or Roll_Adjusted).
Writes the actionable rows to private/maintenance_work/roll_actions_<date>.json
for review before roll_status_apply.py.
"""
import datetime
import json
import sys

import pandas as pd

from sysdata.data_blob import dataBlob
from sysproduction.interactive_update_roll_status import (
    get_auto_roll_parameters_potentially_using_default,
    get_list_of_instruments_to_auto_cycle,
    setup_roll_data_with_state_reporting,
    suggest_roll_state_for_instrument,
)
from sysproduction.maintenance import work_path
from sysproduction.reporting.api import reportingApi
from sysproduction.reporting.report_configs import roll_report_config
from sysproduction.reporting.reporting_functions import run_report_with_data_blob

pd.set_option("display.max_rows", None)
pd.set_option("display.max_columns", None)
pd.set_option("display.width", 250)


def roll_status_dryrun(days_ahead: int = 10, run_full_report: bool = True) -> list:
    actionable = []
    with dataBlob(log_name="Maintenance-Roll-Dryrun") as data:
        params = get_auto_roll_parameters_potentially_using_default(
            data, use_default=True
        )
        print("auto roll parameters:", params)
        instruments = get_list_of_instruments_to_auto_cycle(data, days_ahead=days_ahead)
        print("\n=== per-instrument (READ ONLY) ===")
        for ic in instruments:
            try:
                rd = setup_roll_data_with_state_reporting(data, ic)
                sug = suggest_roll_state_for_instrument(rd, params)
            except Exception as e:
                print("%s ERROR %r" % (ic, e))
                continue
            sug_name = getattr(sug, "name", str(sug))
            row = dict(
                instrument=ic,
                state=rd.original_roll_status.name,
                pos_priced=rd.position_priced_contract,
                days_roll=rd.days_until_roll,
                days_exp=rd.days_until_expiry,
                relvol=round(rd.relative_volume, 3),
                absvol=rd.absolute_forward_volume,
                orphans=rd.orphaned_contract_positions,
                allowed=rd.allowable_roll_states_as_list_of_str,
                suggest=sug_name,
            )
            line = (
                "%-16s state=%-14s pos_priced=%4d days_roll=%4d days_exp=%4d "
                "relvol=%.3f absvol=%6d orphans=%s -> %s"
                % (
                    ic,
                    row["state"],
                    row["pos_priced"],
                    row["days_roll"],
                    row["days_exp"],
                    row["relvol"],
                    row["absvol"],
                    row["orphans"],
                    sug_name,
                )
            )
            print(line)
            if (
                sug_name != row["state"]
                or row["pos_priced"] != 0
                or sug_name in ("Ask", "Roll_Adjusted")
                or row["orphans"]
            ):
                actionable.append(row)

        print("\n=== ACTIONABLE (%d of %d) ===" % (len(actionable), len(instruments)))
        for r in actionable:
            print(
                "%-16s %-14s pos=%3d exp=%3d roll=%3d relvol=%.3f absvol=%6d allowed=%s -> %s"
                % (
                    r["instrument"],
                    r["state"],
                    r["pos_priced"],
                    r["days_exp"],
                    r["days_roll"],
                    r["relvol"],
                    r["absvol"],
                    r["allowed"],
                    r["suggest"],
                )
            )
        out = work_path("roll_actions_%s.json" % datetime.date.today().isoformat())
        with open(out, "w") as f:
            json.dump(actionable, f, indent=1, default=str)
        print("\nwritten", out)

        if run_full_report:
            print("\n=== ROLL REPORT ===")
            config = roll_report_config.new_config_with_modified_output("console")
            config.modify_kwargs(reporting_api=reportingApi(data))
            run_report_with_data_blob(config, data)
    return actionable


if __name__ == "__main__":
    days = 10
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    roll_status_dryrun(days_ahead=days, run_full_report="--no-report" not in sys.argv)
