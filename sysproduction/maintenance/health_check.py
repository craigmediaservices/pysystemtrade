"""
System health check. Read-only. Exit code 0 = green, 1 = something to look at.

Checks: daytime processes alive (by PID) and control-table status, IB
connection, broker-vs-DB and contract-vs-strategy position breaks, IB open
orders, and what is sitting on the order stacks.
"""
import datetime
import os
import socket
import sys

from sysbrokers.IB.ib_connection import connectionIB
from sysdata.data_blob import dataBlob
from sysdata.mongodb.mongo_IB_client_id import mongoIbBrokerClientIdData
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


# a freshly started process is given this long before silence counts
HANG_GRACE_MINUTES = 10
# the log server every process writes through (syslogging/logging_prod.yaml)
LOG_SERVER = ("localhost", 6020)


def log_server_reachable(address=LOG_SERVER, timeout_seconds: float = 2.0) -> bool:
    """
    Every process logs through a socket handler to this server. If it is
    down, silence in the log file means nothing, so the hang detector must
    not judge. (An mtime-based 'did anyone log recently' gate is blind in
    the evening, when the stack handler is the only writer.)
    """
    try:
        with socket.create_connection(address, timeout=timeout_seconds):
            return True
    except OSError:
        return False


def pid_runs_script(pid, script_name: str) -> bool:
    try:
        with open("/proc/%d/cmdline" % int(float(pid)), "rb") as f:
            cmdline = f.read().decode("utf-8", errors="replace")
    except (OSError, ValueError, TypeError):
        return False
    return script_name in cmdline


def longest_running_method_minutes(control_record, now: datetime.datetime):
    """
    Minutes the longest currently-running method of a process has been
    running per the control table, or None if nothing is marked running.
    """
    methods = control_record.running_methods
    longest = None
    for name in list(methods.as_dict().keys()):
        if not methods.currently_running(name):
            continue
        started = methods.when_last_start_run(name)
        minutes = (now - started).total_seconds() / 60.0
        if longest is None or minutes > longest:
            longest = minutes
    return longest


def process_looks_hung(name: str, control_record, now: datetime.datetime) -> tuple:
    """
    (hung, detail). Hung = expected to be logging, alive, started more than
    HANG_GRACE_MINUTES ago, no log line for SILENT_MINUTES, WHILE the log
    server is reachable AND the control table shows a method stuck running
    for at least as long. Log silence alone is not enough: a dead log
    server must not get a healthy process killed. Added after 2026-09-16
    (three hours alive-but-hung).
    """
    tag = LOG_TAGS.get(name)
    if tag is None:
        return False, "no log tag"
    started_min_ago = (now - control_record.last_start_time).total_seconds() / 60.0
    if started_min_ago < HANG_GRACE_MINUTES:
        return False, "started %d min ago" % int(started_min_ago)
    silent = minutes_since_last_log_line(tag, now)
    if silent is None or silent <= SILENT_MINUTES:
        return False, "logged %s min ago" % ("?" if silent is None else int(silent))
    if not log_server_reachable():
        return False, "log server unreachable: cannot judge"
    stuck = longest_running_method_minutes(control_record, now)
    if stuck is None or stuck < SILENT_MINUTES:
        return False, "silent %d min but no method stuck (%s)" % (
            int(silent),
            "none running" if stuck is None else "%d min" % int(stuck),
        )
    return True, "no log line for %d min, method running %d min" % (
        int(silent),
        int(stuck),
    )


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


# processes that run a fixed number of times (max_executions in the control config)
# and then exit cleanly well before their stop time: run_systems after the 10:05 run,
# the order generator after 11:30. Gone after that is normal, not a crash.
FINITE_RUN_PROCESSES = ["run_systems", "run_strategy_order_generator"]


def finished_for_the_day(name: str, record, now: datetime.datetime) -> bool:
    """
    Pure, unit-tested. A finite-run process that started today, has a later end
    time and was not closed by the crash monitor has done its runs for the day.
    """
    if name not in FINITE_RUN_PROCESSES:
        return False
    start, end = record.last_start_time, record.last_end_time
    if not start or not end or start.date() != now.date():
        return False
    if getattr(record, "recently_crashed", False):
        return False
    return end >= start


def connect_ib_once(data: dataBlob) -> tuple:
    """
    (connected, reason). ONE connection attempt, logged at WARNING on failure.

    dataBlob.ib_conn retries six times and connectionIB logs each failure as
    CRITICAL (= an email), so a blocked gateway turned every health check into
    a dozen emails in a minute (2026-09-16). A read-only probe reports the
    failure itself as a RED line; it does not need to page anyone.
    """
    data.add_class_object(mongoIbBrokerClientIdData)
    client_id = int(data.db_ib_broker_client_id.return_valid_client_id())
    try:
        conn = connectionIB(
            client_id, log_name=data.log_name, critical_on_failure=False
        )
    except Exception as e:
        data.db_ib_broker_client_id.release_clientid(client_id)
        return False, str(e) or type(e).__name__
    # hand it to the blob so dataBroker etc. reuse it and close() releases it
    data._ib_conn = conn
    return True, ""


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
            if expected and not alive and finished_for_the_day(name, c, now):
                flag = "  (done for the day)"
            elif expected and not alive:
                flag = "  <-- NOT RUNNING (expected)"
                problems.append("%s not running" % name)
            if c.status != "GO":
                flag += "  <-- status %s" % c.status
                problems.append("%s status %s" % (name, c.status))
            if expected and alive and name in LOG_TAGS:
                hung, detail = process_looks_hung(name, c, now)
                if hung:
                    flag += "  <-- HUNG: %s" % detail
                    problems.append(
                        "%s alive but hung (%s): kill -9 and restart" % (name, detail)
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

        connected, why = connect_ib_once(data)
        if verbose:
            print("--- IB connected:", connected, "" if connected else "(%s)" % why)
        if not connected:
            problems.append("IB not connected (%s): broker checks skipped" % why)

        dp = diagPositions(data)
        b2 = dp.get_list_of_breaks_between_contract_and_strategy_positions()
        if connected:
            db = dataBroker(data)
            b1 = db.get_list_of_breaks_between_broker_and_db_contract_positions()
            if verbose:
                print("--- breaks broker vs DB:", b1)
            if b1:
                problems.append("broker/DB breaks: %s" % b1)
        if verbose:
            print("--- breaks contract vs strategy:", b2)
        if b2:
            problems.append("contract/strategy breaks: %s" % b2)

        if connected:
            open_trades = data.ib_conn.ib.openTrades()
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
