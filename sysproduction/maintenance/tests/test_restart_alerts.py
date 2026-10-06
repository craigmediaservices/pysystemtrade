"""
Refusal alerts (2026-10-06): while a crashed process stays down with its
restart budget spent, the 10-minute cron used to log CRITICAL (an email) on
every pass. Now each crashed run alerts once; a cooldown refusal is a
WARNING, once.
"""
import datetime
from types import SimpleNamespace
from unittest import mock

from sysproduction.maintenance import restart_crashed_processes as rcp

NAME = "run_stack_handler"


def _cron_pass(tmp_path, restart_stamps, log, started):
    rec = SimpleNamespace(
        status="GO",
        process_id=4242,  # dead pid still marked running -> crashed
        last_start_time=started,
        last_end_time=started - datetime.timedelta(hours=1),
        recently_crashed=False,
    )
    control = mock.Mock()
    control.get_dict_of_control_processes.return_value = {NAME: rec}
    blob = mock.MagicMock()
    blob.__enter__.return_value = SimpleNamespace(log=log)
    state = tmp_path / "restart_state.json"
    with mock.patch.object(rcp, "STATE_FILE", str(state)):
        rcp.save_restart_history({NAME: restart_stamps})
    with mock.patch.object(rcp, "STATE_FILE", str(state)), mock.patch.object(
        rcp, "ALERT_STATE_FILE", str(tmp_path / "restart_alerts.json"), create=True
    ), mock.patch.object(rcp, "dataBlob", return_value=blob), mock.patch.object(
        rcp, "dataControlProcess", return_value=control
    ), mock.patch.object(
        rcp, "DAYTIME_PROCESSES", [NAME]
    ), mock.patch.object(
        rcp, "pid_alive", return_value=False
    ), mock.patch.object(
        rcp, "should_be_running", return_value=True
    ), mock.patch.object(
        rcp, "restart_process"
    ) as restart:
        rcp.restart_crashed_processes()
    restart.assert_not_called()


def test_spent_budget_emails_once_across_cron_passes(tmp_path):
    now = datetime.datetime.now()
    log = mock.Mock()
    for _ in range(3):
        _cron_pass(tmp_path, [now, now], log, started=now)
    assert log.critical.call_count == 1
    assert "budget is spent" in log.critical.call_args.args[0]


def test_cooldown_is_a_single_warning_not_a_critical(tmp_path):
    now = datetime.datetime.now()
    log = mock.Mock()
    for _ in range(3):
        _cron_pass(tmp_path, [now], log, started=now)
    log.critical.assert_not_called()
    assert log.warning.call_count == 1


# --- the pure policy -------------------------------------------------------------

NOW = datetime.datetime(2026, 10, 6, 11, 0)
SPENT = [NOW - datetime.timedelta(hours=2), NOW - datetime.timedelta(hours=1)]
STARTED = NOW - datetime.timedelta(minutes=50)


def test_first_refusal_with_budget_spent_is_critical():
    alerts = {}
    assert rcp.refusal_alert(alerts, NAME, "down", SPENT, STARTED, NOW) == "critical"


def test_same_crash_is_not_alerted_again():
    alerts = {}
    rcp.refusal_alert(alerts, NAME, "down", SPENT, STARTED, NOW)
    later = NOW + datetime.timedelta(minutes=10)
    assert rcp.refusal_alert(alerts, NAME, "down", SPENT, STARTED, later) is None


def test_a_new_crash_after_a_fresh_start_alerts_again():
    alerts = {}
    rcp.refusal_alert(alerts, NAME, "down", SPENT, STARTED, NOW)
    restarted_by_hand = NOW + datetime.timedelta(minutes=30)
    later = NOW + datetime.timedelta(minutes=60)
    assert (
        rcp.refusal_alert(alerts, NAME, "down", SPENT, restarted_by_hand, later)
        == "critical"
    )


def test_cooldown_refusal_is_a_warning():
    alerts = {}
    one = [NOW - datetime.timedelta(minutes=5)]
    assert rcp.refusal_alert(alerts, NAME, "down", one, STARTED, NOW) == "warning"
    assert rcp.refusal_alert(alerts, NAME, "down", one, STARTED, NOW) is None


def test_next_day_alerts_again_and_old_keys_are_dropped():
    alerts = {}
    rcp.refusal_alert(alerts, NAME, "down", SPENT, STARTED, NOW)
    tomorrow = NOW + datetime.timedelta(days=1)
    stamps = [tomorrow, tomorrow]
    assert (
        rcp.refusal_alert(alerts, NAME, "down", stamps, STARTED, tomorrow) == "critical"
    )
    assert all(k.startswith(tomorrow.date().isoformat()) for k in alerts[NAME])


def test_other_processes_alert_independently():
    alerts = {}
    rcp.refusal_alert(alerts, NAME, "down", SPENT, STARTED, NOW)
    assert (
        rcp.refusal_alert(alerts, "run_capital_update", "down", SPENT, STARTED, NOW)
        == "critical"
    )


def test_alert_state_round_trips_atomically(tmp_path):
    path = tmp_path / "restart_alerts.json"
    with mock.patch.object(rcp, "ALERT_STATE_FILE", str(path), create=True):
        alerts = {}
        rcp.refusal_alert(alerts, NAME, "down", SPENT, STARTED, NOW)
        rcp.save_alert_state(alerts)
        assert rcp.load_alert_state() == alerts
    assert not (tmp_path / "restart_alerts.json.tmp").exists()


def test_unreadable_alert_state_means_alerting_again(tmp_path, capsys):
    path = tmp_path / "restart_alerts.json"
    path.write_text("{not json")
    with mock.patch.object(rcp, "ALERT_STATE_FILE", str(path), create=True):
        assert rcp.load_alert_state() == {}
    assert "unreadable" in capsys.readouterr().out
