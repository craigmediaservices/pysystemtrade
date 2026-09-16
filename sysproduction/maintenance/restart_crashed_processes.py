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
from sysproduction.maintenance import work_path
from sysproduction.maintenance.health_check import (
    DAYTIME_PROCESSES,
    pid_alive,
    pid_runs_script,
    process_looks_hung,
    should_be_running,
)

# --- restart budget: a process that keeps dying has a real problem; restarting
# it blindly hides that (and can restart into a half-processed fill). So: at
# most MAX_RESTARTS_PER_DAY per process, never within MIN_MINUTES_BETWEEN,
# and once the budget is spent we log CRITICAL (emailed) instead.
STATE_FILE = work_path("restart_state.json")
MAX_RESTARTS_PER_DAY = 2
MIN_MINUTES_BETWEEN = 20


def load_restart_history() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            raw = json.load(f)
        return {
            name: [datetime.datetime.fromisoformat(t) for t in stamps]
            for name, stamps in raw.items()
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        # fail loud: an unreadable file would otherwise silently reset the budget
        print("   WARNING restart history unreadable (%s): budget reset" % e)
        return {}


def save_restart_history(history: dict):
    raw = {name: [t.isoformat() for t in stamps] for name, stamps in history.items()}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(raw, f, indent=1)
    os.replace(tmp, STATE_FILE)


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


def kill_process(pid, script_name: str) -> bool:
    """SIGKILL, but only a pid whose command line is the expected script."""
    if not pid_runs_script(pid, script_name):
        return False
    try:
        os.kill(int(float(pid)), signal.SIGKILL)
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
            hung, detail = process_looks_hung(name, c, now)
            if not hung:
                continue
            allowed, why = restart_allowed(history.get(name, []), now)
            msg = "%s alive (pid %s) but hung: %s" % (name, int(c.process_id), detail)
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
                # step 1 marks the dead pid closed, step 2 restarts within budget
                if not kill_process(c.process_id, SCRIPT_FOR_PROCESS[name]):
                    data.log.error(
                        "%s: could not kill pid %s (not the expected script?); "
                        "no restart" % (name, c.process_id)
                    )

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

    if restarted:
        save_restart_history(history)
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
