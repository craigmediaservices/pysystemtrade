"""
System health check. Read-only. Exit code 0 = green, 1 = something to look at.

Checks: daytime processes alive (by PID) and control-table status, IB
connection, broker-vs-DB and contract-vs-strategy position breaks, IB open
orders, and what is sitting on the order stacks.
"""
import datetime
import os
import sys

from sysdata.data_blob import dataBlob
from sysproduction.data.broker import dataBroker
from sysproduction.data.control_process import dataControlProcess, diagControlProcess
from sysproduction.data.orders import dataOrders
from sysproduction.data.positions import diagPositions

# processes that should be alive between their start and stop times
# processes that should be alive all day inside their window. run_capital_update is
# NOT here: since 2026-09-10 it is a cron one-shot (max_executions 1, 00/06/12/18:25)
# so "not running" is its normal state.
DAYTIME_PROCESSES = [
    "run_stack_handler",
    "run_daily_prices_updates",
    "run_daily_update_multiple_adjusted_prices",
    "run_systems",
    "run_strategy_order_generator",
]


def pid_alive(pid) -> bool:
    try:
        pid = int(float(pid))
    except BaseException:
        return False
    return pid > 0 and os.path.exists("/proc/%d" % pid)


LOG_FILE = os.path.expanduser("~/logs/pysystemtrade.log")
# a live process that has not logged for this long is treated as hung
SILENT_MINUTES = 15
# log 'type' tags per process, as they appear in the log line's dict
LOG_TAGS = {"run_stack_handler": "stack_handler"}


def minutes_since_last_log_line(tag: str, now: datetime.datetime):
    """
    Age in minutes of the last log line for a process, scanning the log file
    backwards. Returns None if nothing found. Added 2026-09-16 after
    run_stack_handler sat alive-but-hung for three hours (blocked on an IB
    request that never answered) with alive=True in this check.
    """
    try:
        size = os.path.getsize(LOG_FILE)
        with open(LOG_FILE, "rb") as f:
            chunk = 4 * 1024 * 1024
            offset = max(size - chunk, 0)
            f.seek(offset)
            text = f.read().decode("utf-8", errors="replace")
    except BaseException:
        return None
    needle = " %s " % tag
    lines = text.splitlines()
    for line in reversed(lines):
        if needle in line[:80]:
            try:
                stamp = datetime.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            return (now - stamp).total_seconds() / 60.0
    # not in the tail at all: silent for at least as long as the tail spans
    for line in lines[1:]:
        try:
            stamp = datetime.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        return (now - stamp).total_seconds() / 60.0
    return None


def should_be_running(control, name: str, now: datetime.datetime) -> bool:
    # start/stop times live on diagControlProcess (config), not dataControlProcess
    # (DB state). Until 2026-09-14 this called the wrong class, swallowed the
    # AttributeError and returned False, so nothing was ever flagged or restarted.
    try:
        diag = diagControlProcess(control.data)
        start = diag.get_start_time(name)
        stop = diag.get_stop_time(name)
    except BaseException as e:
        print("   cannot read window for %s: %s" % (name, e), file=sys.stderr)
        return False
    return start <= now.time() < stop


def health_check(verbose: bool = True) -> list:
    problems = []
    now = datetime.datetime.now()
    with dataBlob(log_name="Maintenance-Health-Check") as data:
        control = dataControlProcess(data)
        procs = control.get_dict_of_control_processes()
        if verbose:
            print("--- processes (%s) ---" % now.strftime("%Y-%m-%d %H:%M"))
        for name, c in procs.items():
            alive = pid_alive(c.process_id)
            expected = name in DAYTIME_PROCESSES and should_be_running(
                control, name, now
            )
            flag = ""
            if expected and not alive:
                flag = "  <-- NOT RUNNING (expected)"
                problems.append("%s not running" % name)
            if c.status != "GO":
                flag += "  <-- status %s" % c.status
                problems.append("%s status %s" % (name, c.status))
            if expected and alive and name in LOG_TAGS:
                age = minutes_since_last_log_line(LOG_TAGS[name], now)
                if age is not None and age > SILENT_MINUTES:
                    flag += "  <-- ALIVE BUT SILENT %d min (hung?)" % int(age)
                    problems.append(
                        "%s alive but no log line for %d min: probably hung, "
                        "kill -9 and restart" % (name, int(age))
                    )
            if verbose:
                print(
                    "   %-42s status=%-5s alive=%-5s start=%s end=%s%s"
                    % (
                        name,
                        c.status,
                        alive,
                        c.last_start_time.strftime("%m-%d %H:%M"),
                        c.last_end_time.strftime("%m-%d %H:%M"),
                        flag,
                    )
                )

        ib = data.ib_conn.ib
        connected = ib.isConnected()
        if verbose:
            print("--- IB connected:", connected)
        if not connected:
            problems.append("IB not connected")

        dp = diagPositions(data)
        db = dataBroker(data)
        b1 = db.get_list_of_breaks_between_broker_and_db_contract_positions()
        b2 = dp.get_list_of_breaks_between_contract_and_strategy_positions()
        if verbose:
            print("--- breaks broker vs DB:", b1)
            print("--- breaks contract vs strategy:", b2)
        if b1:
            problems.append("broker/DB breaks: %s" % b1)
        if b2:
            problems.append("contract/strategy breaks: %s" % b2)

        open_trades = ib.openTrades()
        if verbose:
            print("--- IB open orders: %d" % len(open_trades))
            for t in open_trades:
                print(
                    "   ",
                    t.contract.localSymbol or t.contract.symbol,
                    t.order.action,
                    t.order.totalQuantity,
                    t.order.orderType,
                    t.orderStatus.status,
                )

        do = dataOrders(data)
        if verbose:
            print("--- order stacks ---")
        for label, stack in [
            ("instrument", do.db_instrument_stack_data),
            ("contract", do.db_contract_stack_data),
            ("broker", do.db_broker_stack_data),
        ]:
            for oid in stack.get_list_of_order_ids():
                o = stack.get_order_with_id_from_stack(oid)
                if verbose:
                    print("   %-10s %s" % (label, o))

    if verbose:
        print("\nRESULT:", "GREEN" if not problems else "RED - " + "; ".join(problems))
    return problems


if __name__ == "__main__":
    sys.exit(1 if health_check() else 0)
