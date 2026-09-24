"""
spike_accept drives the real interactive tool with pexpect, so its prompt
regexes ARE the interface. A pattern that fails to match does not error - it
blocks until the 900s timeout with the row unaccepted (2026-09-24, PIPELINE).
Lines below are copied verbatim from a real transcript.
"""
from sysproduction.maintenance.spike_accept import RE_SPIKE, RE_CONTRACT


def test_matches_a_negative_value_and_negative_previous():
    # PIPELINE and the other differential markets quote around zero
    line = (
        "Value -0.210000 of FINAL on 2026-09-23 23:00:00 is a big change "
        "from previous value of -0.142500"
    )
    assert RE_SPIKE.search(line).groups() == ("-0.210000", "2026-09-23", "-0.142500")


def test_matches_a_positive_value():
    line = (
        "Value 147.000000 of FINAL on 2026-09-23 23:00:00 is a big change "
        "from previous value of 149.000000"
    )
    assert RE_SPIKE.search(line).groups() == ("147.000000", "2026-09-23", "149.000000")


def test_matches_a_sign_change():
    line = (
        "Value 0.050000 of FINAL on 2026-09-23 23:00:00 is a big change "
        "from previous value of -0.142500"
    )
    assert RE_SPIKE.search(line).groups() == ("0.050000", "2026-09-23", "-0.142500")


def test_contract_pattern_takes_lower_case_and_hyphens():
    # same class of bug as the spike_review regex fixed 2026-09-18
    for code in ("PIPELINE", "US20-new", "US2Y_micro", "HEAT-DEG-LON"):
        line = "Manually checking prices for %s/20261000" % code
        assert RE_CONTRACT.search(line).groups() == (code, "20261000")
