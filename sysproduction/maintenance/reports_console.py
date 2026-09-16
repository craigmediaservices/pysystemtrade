"""
Re-run selected production reports to the console (and a text file) so the
overnight emails can be reviewed without a mailbox.

    python3 sysproduction/maintenance/reports_console.py [--reports a,b,c]

Default set (cheap, read-only): status_report, reconcile_report,
stranded_legs_report, fx_balance_report, bond_ladder_report, trade_report,
roll_report. Any key from sysproduction.reporting.report_configs.all_configs
is accepted (the dict is report_config_defaults). Output is also written to
private/maintenance_work/reports_<date>.txt.
"""
import contextlib
import datetime
import io
import sys

from sysdata.data_blob import dataBlob
from sysproduction.maintenance import work_path
from sysproduction.reporting.report_configs import report_config_defaults as all_configs
from sysproduction.reporting.reporting_functions import run_report_with_data_blob

DEFAULT_REPORTS = [
    "status_report",
    "reconcile_report",
    "stranded_legs_report",
    "fx_balance_report",
    "bond_ladder_report",
    "trade_report",
    "roll_report",
]


def reports_console(names: list) -> str:
    out_path = work_path("reports_%s.txt" % datetime.date.today().isoformat())
    buffer = io.StringIO()
    with dataBlob(log_name="Maintenance-Reports") as data:
        for name in names:
            config = all_configs[name].new_config_with_modified_output("console")
            header = "\n\n%s\n# %s\n%s\n" % ("#" * 80, name, "#" * 80)
            print(header)
            buffer.write(header)
            captured = io.StringIO()
            try:
                with contextlib.redirect_stdout(captured):
                    run_report_with_data_blob(config, data)
            except Exception as e:
                captured.write("REPORT FAILED: %r\n" % e)
            text = captured.getvalue()
            print(text)
            buffer.write(text)
    with open(out_path, "w") as f:
        f.write(buffer.getvalue())
    print("\nwritten", out_path)
    return out_path


if __name__ == "__main__":
    names = DEFAULT_REPORTS
    if "--reports" in sys.argv:
        names = sys.argv[sys.argv.index("--reports") + 1].split(",")
    reports_console(names)
