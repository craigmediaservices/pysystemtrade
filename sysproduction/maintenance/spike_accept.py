"""
Drive the production tool interactive_manual_check_historical_prices with
pre-reviewed answers.

    python3 sysproduction/maintenance/spike_accept.py [--date YYYY-MM-DD]

Reads private/maintenance_work/spike_instruments_<date>.txt and
spike_candidates_<date>.txt (written by spike_review.py, then edited by
hand: delete any row you do NOT want accepted). For each instrument the real
tool is run; every spike prompt whose (instrument, contract, date) is on the
candidates list is accepted (<return>). Any prompt NOT on the list aborts
the run with Ctrl-C so nothing unreviewed is written. Uses pexpect.
"""
import datetime
import re
import sys

import pexpect

from sysproduction.maintenance import REPO_ROOT, work_path

RE_CONTRACT = re.compile(r"Manually checking prices for (\S+)/(\d{8})")
RE_SPIKE = re.compile(
    r"Value ([\d.]+) of FINAL on (\d{4}-\d{2}-\d{2}) [\d:]+ is a big change from previous value of ([\d.]+)"
)


def spike_accept(date: datetime.date):
    instruments = [
        l.strip()
        for l in open(work_path("spike_instruments_%s.txt" % date.isoformat()))
        if l.strip()
    ]
    approved = {
        tuple(l.strip().split(","))
        for l in open(work_path("spike_candidates_%s.txt" % date.isoformat()))
        if l.strip()
    }
    print("instruments:", instruments)
    print("approved rows:", len(approved))
    if not instruments:
        return

    child = pexpect.spawn(
        "python3 sysproduction/interactive_manual_check_historical_prices.py",
        cwd=REPO_ROOT,
        encoding="utf-8",
        timeout=900,
    )
    child.logfile_read = open(
        work_path("spike_accept_%s.transcript" % date.isoformat()), "w"
    )

    child.expect("Make changes\\?")
    child.sendline("n")

    accepted, current = [], (None, None)
    child.expect("Instrument code\\?")
    for ic in instruments:
        child.sendline(ic)
        while True:
            i = child.expect([RE_CONTRACT, RE_SPIKE, "Instrument code\\?"])
            if i == 0:
                current = (child.match.group(1), child.match.group(2))
            elif i == 1:
                val, day, prev = child.match.groups()
                key = (current[0], current[1], day)
                child.expect("<return> to accept")
                if key in approved:
                    accepted.append((key, prev, val))
                    print("ACCEPT %s: %s -> %s" % (key, prev, val))
                    child.sendline("")
                else:
                    print(
                        "UNEXPECTED SPIKE %s: %s -> %s  -- ABORTING" % (key, prev, val)
                    )
                    child.sendcontrol("c")
                    child.close(force=True)
                    print("accepted before abort:", len(accepted))
                    sys.exit(2)
            else:
                break
    child.sendline("")  # exit the tool
    child.expect(pexpect.EOF)
    child.close()
    print("\nACCEPTED %d rows; exit status %s" % (len(accepted), child.exitstatus))


if __name__ == "__main__":
    date = datetime.date.today()
    if "--date" in sys.argv:
        date = datetime.date.fromisoformat(sys.argv[sys.argv.index("--date") + 1])
    spike_accept(date)
