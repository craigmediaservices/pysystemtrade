"""
Restart policy (2026-09-16): a process that is provably hung is killed and
restarted, restarts are budgeted so a process dying for a real reason is
left down with a CRITICAL, and log silence alone never counts as a hang.
"""
import datetime
from types import SimpleNamespace
from unittest import mock

from sysobjects.production.process_control import dictOfRunningMethods
from sysproduction.maintenance import health_check as hc
from sysproduction.maintenance import restart_crashed_processes as rcp


NOW = datetime.datetime(2026, 9, 16, 11, 0)


def _minutes_ago(m):
    return NOW - datetime.timedelta(minutes=m)


# --- budget ------------------------------------------------------------------


def test_first_restart_of_the_day_is_allowed():
    assert rcp.restart_allowed([], NOW) == (True, "")


def test_yesterdays_restarts_do_not_count():
    stamps = [NOW - datetime.timedelta(days=1, minutes=5)] * 5
    assert rcp.restart_allowed(stamps, NOW)[0]


def test_too_soon_after_previous_restart_is_refused():
    allowed, why = rcp.restart_allowed([_minutes_ago(5)], NOW)
    assert not allowed and "min ago" in why


def test_second_restart_after_cooldown_is_allowed():
    assert rcp.restart_allowed([_minutes_ago(rcp.MIN_MINUTES_BETWEEN + 1)], NOW)[0]


def test_daily_budget_is_enforced():
    stamps = [_minutes_ago(300), _minutes_ago(200)]
    allowed, why = rcp.restart_allowed(stamps, NOW)
    assert not allowed and "times today" in why


def test_history_round_trips_through_the_state_file(tmp_path):
    state = tmp_path / "restart_state.json"
    with mock.patch.object(rcp, "STATE_FILE", str(state)):
        rcp.save_restart_history({"run_stack_handler": [NOW]})
        assert rcp.load_restart_history() == {"run_stack_handler": [NOW]}


def test_missing_state_file_means_empty_history(tmp_path):
    with mock.patch.object(rcp, "STATE_FILE", str(tmp_path / "nope.json")):
        assert rcp.load_restart_history() == {}


# --- hang detection ------------------------------------------------------------


def _record(started_minutes_ago, running_for=None):
    methods = dictOfRunningMethods()
    if running_for is not None:
        methods.set_entry("process_fills_stack", [_minutes_ago(running_for), ""])
    return SimpleNamespace(
        last_start_time=_minutes_ago(started_minutes_ago), running_methods=methods
    )


def _judge(record, silent, pipeline):
    with mock.patch.object(
        hc, "minutes_since_last_log_line", return_value=silent
    ), mock.patch.object(hc, "minutes_since_any_log_line", return_value=pipeline):
        return hc.process_looks_hung("run_stack_handler", record, NOW)


def test_hung_when_silent_pipeline_alive_and_method_stuck():
    hung, detail = _judge(_record(180, running_for=170), silent=170, pipeline=1)
    assert hung and "170" in detail


def test_recently_started_process_is_never_hung():
    hung, _ = _judge(_record(3, running_for=3), silent=999, pipeline=1)
    assert not hung


def test_recently_logging_process_is_not_hung():
    hung, _ = _judge(_record(180, running_for=170), silent=2, pipeline=1)
    assert not hung


def test_dead_log_pipeline_means_cannot_judge():
    # nothing at all has logged for a while: the log server is the problem
    hung, detail = _judge(_record(180, running_for=170), silent=170, pipeline=40)
    assert not hung and "pipeline" in detail


def test_silence_without_a_stuck_method_is_not_a_hang():
    hung, detail = _judge(_record(180, running_for=None), silent=170, pipeline=1)
    assert not hung and "no method stuck" in detail


def test_short_running_method_is_not_stuck():
    hung, _ = _judge(_record(180, running_for=4), silent=170, pipeline=1)
    assert not hung


def test_processes_without_a_log_tag_are_not_judged():
    hung, _ = hc.process_looks_hung("run_systems", _record(600), NOW)
    assert not hung


def test_kill_refuses_a_pid_that_is_not_the_script():
    with mock.patch.object(rcp, "pid_runs_script", return_value=False), mock.patch(
        "os.kill"
    ) as kill:
        assert not rcp.kill_process(12345, "run_stack_handler")
        kill.assert_not_called()
