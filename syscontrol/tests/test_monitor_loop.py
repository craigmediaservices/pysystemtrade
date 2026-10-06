"""
syscontrol/monitor.py (2026-10-06): one failed pass (Mongo blip, file error)
used to end the monitor until the next reboot. Now it logs the traceback and
carries on; only a run of failures is escalated to CRITICAL.
"""
from types import SimpleNamespace
from unittest import mock

import pytest

from syscontrol import monitor as mon


class _StopLoop(BaseException):
    pass


def test_one_failed_pass_does_not_kill_the_monitor():
    passes = []

    def check(_observatory):
        passes.append(1)
        if len(passes) == 1:
            raise RuntimeError("mongo blip")

    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            raise _StopLoop()

    log = mock.Mock()
    blob = mock.MagicMock()
    blob.__enter__.return_value = SimpleNamespace(log=log)
    with mock.patch.object(mon, "dataBlob", return_value=blob), mock.patch.object(
        mon, "processMonitor"
    ), mock.patch.object(
        mon, "check_if_pid_running_and_if_not_finish", side_effect=check
    ), mock.patch.object(
        mon, "generate_html"
    ), mock.patch.object(
        mon.time, "sleep", side_effect=fake_sleep
    ):
        with pytest.raises(_StopLoop):
            mon.monitor()

    assert len(passes) == 3
    assert "mongo blip" in log.error.call_args_list[0].args[0]  # traceback logged
    log.critical.assert_not_called()


def _failing_observatory():
    obs = mock.Mock()
    obs.update_all_status_with_process_control.side_effect = OSError("disk")
    return obs


def test_repeated_failures_escalate_to_critical_only_periodically():
    log = mock.Mock()
    obs = _failing_observatory()
    failures = 0
    with mock.patch.object(mon, "check_if_pid_running_and_if_not_finish"):
        for _ in range(
            mon.FAILURES_BEFORE_CRITICAL + mon.FAILURES_BETWEEN_REPEAT_CRITICALS
        ):
            failures = mon.monitor_pass(obs, log, failures)
    assert log.critical.call_count == 2
    assert log.error.call_count + log.critical.call_count == failures


def test_recovery_resets_the_count_and_says_so():
    log = mock.Mock()
    with mock.patch.object(
        mon, "check_if_pid_running_and_if_not_finish"
    ), mock.patch.object(mon, "generate_html"):
        assert mon.monitor_pass(mock.Mock(), log, 3) == 0
    assert "recovered" in log.warning.call_args.args[0]
