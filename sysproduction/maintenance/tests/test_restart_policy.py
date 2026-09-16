"""
Restart policy for the maintenance restart tool (2026-09-16): a hung process
is killed and restarted, but restarts are budgeted so a process dying for a
real reason is left down with a CRITICAL rather than restarted in a loop.
"""
import datetime
from unittest import mock

from sysproduction.maintenance import restart_crashed_processes as rcp


NOW = datetime.datetime(2026, 9, 16, 11, 0)


def _minutes_ago(m):
    return NOW - datetime.timedelta(minutes=m)


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


def test_recently_started_process_is_never_called_hung():
    with mock.patch.object(rcp, "minutes_since_last_log_line", return_value=999):
        hung, _ = rcp.looks_hung(_minutes_ago(3), "run_stack_handler", NOW)
    assert not hung


def test_silent_process_is_hung():
    with mock.patch.object(rcp, "minutes_since_last_log_line", return_value=45.0):
        hung, silent = rcp.looks_hung(_minutes_ago(60), "run_stack_handler", NOW)
    assert hung and silent == 45.0


def test_recently_logging_process_is_not_hung():
    with mock.patch.object(rcp, "minutes_since_last_log_line", return_value=2.0):
        hung, _ = rcp.looks_hung(_minutes_ago(60), "run_stack_handler", NOW)
    assert not hung


def test_processes_without_a_log_tag_are_not_judged():
    hung, _ = rcp.looks_hung(_minutes_ago(600), "run_systems", NOW)
    assert not hung


def test_history_round_trips_through_the_state_file(tmp_path):
    state = tmp_path / "restart_state.json"
    with mock.patch.object(rcp, "STATE_FILE", str(state)):
        rcp.save_restart_history({"run_stack_handler": [NOW]})
        assert rcp.load_restart_history() == {"run_stack_handler": [NOW]}


def test_missing_state_file_means_empty_history(tmp_path):
    with mock.patch.object(rcp, "STATE_FILE", str(tmp_path / "nope.json")):
        assert rcp.load_restart_history() == {}
