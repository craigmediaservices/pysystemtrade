"""
Equivalent of interactive_controls -> 4 (process control) -> 44 (mark all
dead processes as close), followed by restarting any daytime process that
should be running now but is not.

    python3 sysproduction/maintenance/restart_crashed_processes.py            # do it
    python3 sysproduction/maintenance/restart_crashed_processes.py --dry-run  # show only

Restart uses the same wrapper scripts as cron:
    . ~/.profile; nohup $SCRIPT_PATH/<script> >> $ECHO_PATH/<script>.txt 2>&1 &

Safe to run from cron every 10 minutes: a process that is alive but has not
logged for SILENT_MINUTES is killed first (it is hung on the broker), and
restarts are budgeted (MAX_RESTARTS_PER_DAY, MIN_MINUTES_BETWEEN) so a process
that keeps dying for a real reason is left down with a CRITICAL email rather
than restarted in a loop. Budget state: private/maintenance_work/restart_state.json
"""
import datetime
import json
import os
import signal
import subprocess
import sys
import time

from sysdata.data_blob import dataBlob
from sysproduction.data.control_process import dataControlProcess
from sysproduction.maintenance.health_check import (
    DAYTIME_PROCESSES,
    LOG_TAGS,
    SILENT_MINUTES,
    minutes_since_last_log_line,
    pid_alive,
    should_be_running,
)

# --- restart budget: a process that keeps dying has a real problem; restarting
# it blindly hides that (and can restart into a half-processed fill). So: at
# most MAX_RESTARTS_PER_DAY per process, never within MIN_MINUTES_BETWEEN,
# and once the budget is spent we log CRITICAL (emailed) instead.
STATE_FILE = os.path.expanduser(
    "~/pysystemtrade/private/maintenance_work/restart_state.json"
)
MAX_RESTARTS_PER_DAY = 2
MIN_MINUTES_BETWEEN = 20
# a freshly started process is given this long before a silent log counts as hung
HANG_GRACE_MINUTES = 10


def load_restart_history() -> dict:
    try:
        with open(STATE_FILE) as f:
            raw = json.load(f)
        return {
            name: [datetime.datetime.fromisoformat(t) for t in stamps]
            for name, stamps in raw.items()
        }
    except BaseException:
        return {}


def save_restart_history(history: dict):
    raw = {name: [t.isoformat() for t in stamps] for name, stamps in history.items()}
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(raw, f, indent=1)


def restart_allowed(stamps: list, now: datetime.datetime) -> tuple:
    """
    (allowed, reason). Pure, unit-tested. stamps = previous restart times.
    """
    today = [t for t in stamps if t.date() == now.date()]
    if len(today) >= MAX_RESTARTS_PER_DAY:
        return False, "already restarted %d times today" % len(today)
    if today:
        minutes = (now - max(today)).total_seconds() / 60.0
        if minutes < MIN_MINUTES_BETWEEN:
            return False, "last restart only %d min ago" % int(minutes)
    return True, ""


def looks_hung(last_start_time: datetime.datetime, name: str, now: datetime.datetime):
    """
    Inside its window with a live PID, but no log line for SILENT_MINUTES and
    started more than HANG_GRACE_MINUTES ago. Returns (hung, minutes_silent).
    """
    tag = LOG_TAGS.get(name)
    if tag is None:
        return False, None
    started_min_ago = (now - last_start_time).total_seconds() / 60.0
    if started_min_ago < HANG_GRACE_MINUTES:
        return False, None
    age = minutes_since_last_log_line(tag, now)
    if age is None or age <= SILENT_MINUTES:
        return False, age
    return True, age


def kill_process(pid) -> bool:
    try:
        pid = int(float(pid))
        os.kill(pid, signal.SIGKILL)
    except BaseException:
        return False
    for _ in range(20):
        if not pid_alive(pid):
            return True
        time.sleep(0.5)
    return not pid_alive(pid)


# control-table process name -> wrapper script name in sysproduction/linux/scripts
SCRIPT_FOR_PROCESS = {
    "run_stack_handler": "run_stack_handler",
    "run_capital_update": "run_capital_update",
    "run_daily_prices_updates": "run_daily_price_updates",
    "run_daily_update_multiple_adjusted_prices": "run_daily_update_multiple_adjusted_prices",
    "run_systems": "run_systems",
    "run_strategy_order_generator": "run_strategy_order_generator",
}


def restart_process(script_name: str):
    cmd = ". ~/.profile; nohup $SCRIPT_PATH/%s >> $ECHO_PATH/%s.txt 2>&1 &" % (
        script_name,
        script_name,
    )
    subprocess.Popen(["bash", "-c", cmd], cwd=os.path.expanduser("~"))


def restart_crashed_processes(dry_run: bool = False) -> list:
    now = datetime.datetime.now()
    restarted = []
    history = load_restart_history()
    with dataBlob(log_name="Maintenance-Restart-Processes") as data:
        control = dataControlProcess(data)

        # step 0: alive but hung (no log output) -> kill, so step 1 sees it dead
        procs = control.get_dict_of_control_processes()
        for name in DAYTIME_PROCESSES:
            c = procs.get(name)
            if c is None or not pid_alive(c.process_id):
                continue
            if not should_be_running(control, name, now):
                continue
            hung, silent = looks_hung(c.last_start_time, name, now)
            if not hung:
                continue
            allowed, why = restart_allowed(history.get(name, []), now)
            msg = "%s alive (pid %s) but no log line for %d min: hung" % (
                name,
                int(c.process_id),
                int(silent),
            )
            if not allowed:
                print("   %s - NOT killing: %s" % (msg, why))
                if not dry_run:
                    data.log.critical(
                        "%s; restart budget spent (%s). Needs a human." % (msg, why)
                    )
                continue
            print("   %s -> %s" % (msg, "would kill" if dry_run else "killing"))
            if not dry_run:
                data.log.critical("%s; killing and restarting" % msg)
                if not kill_process(c.process_id):
                    print("   could not kill pid %s" % c.process_id)

        # step 1: interactive_controls 4/44
        procs = control.get_dict_of_control_processes()
        dead = [
            n
            for n, c in procs.items()
            if c.status == "GO"
            and c.process_id
            and not pid_alive(c.process_id)
            and c.last_start_time > c.last_end_time
        ]
        print("processes with a dead PID still marked running:", dead or "none")
        if dead and not dry_run:
            control.check_if_pid_running_and_if_not_finish_all_processes()
            print("   marked as close")

        # step 2: restart what should be running now
        procs = control.get_dict_of_control_processes()
        for name in DAYTIME_PROCESSES:
            c = procs.get(name)
            if c is None:
                continue
            if pid_alive(c.process_id):
                continue
            if not should_be_running(control, name, now):
                print("   %s not running but outside its window - leave" % name)
                continue
            allowed, why = restart_allowed(history.get(name, []), now)
            if not allowed:
                print("   %s should be running - NOT restarting: %s" % (name, why))
                if not dry_run:
                    data.log.critical(
                        "%s is down and the restart budget is spent (%s). "
                        "It is probably failing for a real reason: needs a human."
                        % (name, why)
                    )
                continue
            script = SCRIPT_FOR_PROCESS[name]
            print(
                "   %s should be running -> %s $SCRIPT_PATH/%s"
                % (name, "would restart" if dry_run else "restarting", script)
            )
            if not dry_run:
                restart_process(script)
                restarted.append(name)
                history.setdefault(name, []).append(now)
                save_restart_history(history)

    if restarted:
        time.sleep(25)
        with dataBlob(log_name="Maintenance-Restart-Processes") as data:
            procs = dataControlProcess(data).get_dict_of_control_processes()
            for name in restarted:
                c = procs[name]
                print(
                    "   %s: pid %s alive=%s"
                    % (name, int(c.process_id), pid_alive(c.process_id))
                )
    return restarted


if __name__ == "__main__":
    restart_crashed_processes(dry_run="--dry-run" in sys.argv)
