"""
Equivalent of interactive_controls -> 4 (process control) -> 44 (mark all
dead processes as close), followed by restarting any daytime process that
should be running now but is not.

    python3 sysproduction/maintenance/restart_crashed_processes.py            # do it
    python3 sysproduction/maintenance/restart_crashed_processes.py --dry-run  # show only

Restart uses the same wrapper scripts as cron:
    . ~/.profile; nohup $SCRIPT_PATH/<script> >> $ECHO_PATH/<script>.txt 2>&1 &
"""
import datetime
import os
import subprocess
import sys
import time

from sysdata.data_blob import dataBlob
from sysproduction.data.control_process import dataControlProcess
from sysproduction.maintenance.health_check import (
    DAYTIME_PROCESSES,
    pid_alive,
    should_be_running,
)

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
    with dataBlob(log_name="Maintenance-Restart-Processes") as data:
        control = dataControlProcess(data)

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
            script = SCRIPT_FOR_PROCESS[name]
            print(
                "   %s should be running -> %s $SCRIPT_PATH/%s"
                % (name, "would restart" if dry_run else "restarting", script)
            )
            if not dry_run:
                restart_process(script)
                restarted.append(name)

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
