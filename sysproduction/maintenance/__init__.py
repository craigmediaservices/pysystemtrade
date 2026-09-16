"""
Daily maintenance helpers for a live pysystemtrade server.

These back the `/pst-daily-maintenance` Claude Code skill (see
.claude/skills/pst-daily-maintenance/SKILL.md). Each script is standalone:

  health_check.py               processes, breaks, stack, IB - exit 1 if red
  restart_crashed_processes.py  interactive_controls 4/44 + restart daytime jobs
  roll_status_dryrun.py         read-only roll decisions (auto rules) + roll report
  roll_status_apply.py          apply reviewed roll-state changes
  spike_review.py               reproduce today's price-spike flags against IB
  spike_accept.py               drive interactive_manual_check_historical_prices
  reports_console.py            re-run overnight reports to a text file

Work files go to private/maintenance_work/ (gitignored).
"""
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORK_DIR = os.path.join(REPO_ROOT, "private", "maintenance_work")


def work_path(name: str) -> str:
    os.makedirs(WORK_DIR, exist_ok=True)
    return os.path.join(WORK_DIR, name)
