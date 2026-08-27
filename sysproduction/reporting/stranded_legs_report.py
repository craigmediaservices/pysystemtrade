"""
Stranded legs report: flags positions held in contracts that are BEHIND the
roll (neither priced nor forward contract).

These arise when a roll advances the priced contract past a position that is
still sitting in the old near contract (e.g. a Roll_Adjusted racing a
same-morning fill). They are dangerous because:
  - the strategy net position still matches, so no position-break alert fires
  - the contract is behind the roll, so the roll report ignores it
  - if physically deliverable, IB force-liquidates at First Notice Day
    (~1 month before expiry for NYMEX metals/ags/energy)

Incidents: PLAT + SOYOIL stranded 2026-04-07, found 2026-06-26; PALLAD
stranded 2026-08-07, found 2026-08-27 via IB liquidation notice. This report
exists so the next one is caught the day it happens.

Also lists positions split across multiple contracts (normal mid-roll, but a
NET ZERO split is the precursor state to stranding).

Run on demand:
    from sysproduction.reporting.stranded_legs_report import stranded_legs_report
    stranded_legs_report()
"""

import datetime

import pandas as pd

from syscore.constants import arg_not_supplied
from sysdata.data_blob import dataBlob
from sysproduction.data.positions import diagPositions
from sysproduction.data.contracts import dataContracts
from sysproduction.reporting.reporting_functions import header, body_text, table

INTRO_TEXT = body_text(
    "A STRANDED LEG is a position in a contract that is neither the priced nor "
    "the forward contract, i.e. behind the roll. The system will never trade it "
    "(strategy net matches, so no break fires; roll report ignores it), but if "
    "deliverable IB will force-liquidate it at First Notice Day, roughly a "
    "month BEFORE expiry for NYMEX metals/ags/energy. Anything listed under "
    "STRANDED needs manual action: roll or close it in TWS, then book balance "
    "trades. Net-zero splits are the precursor state and worth watching."
)


def _scan(data: dataBlob):
    diag = diagPositions(data)
    dc = dataContracts(data)
    today = datetime.datetime.now().date()

    instruments = sorted(diag.get_list_of_instruments_with_any_position())
    all_pos = diag.get_all_current_contract_positions()

    stranded_rows = []
    split_rows = []
    for instr in instruments:
        legs = [
            (str(e.contract.contract_date), e.position)
            for e in all_pos
            if e.contract.instrument_code == instr and e.position != 0
        ]
        if not legs:
            continue
        try:
            priced = dc.get_priced_contract_id(instr)
            fwd = dc.get_forward_contract_id(instr)
        except Exception as ex:
            priced = fwd = "(lookup failed: %s)" % ex

        for contract_date, position in legs:
            if contract_date in {priced, fwd}:
                continue
            try:
                expiry = dc.get_actual_expiry(instr, contract_date)
                if hasattr(expiry, "as_date"):
                    expiry = expiry.as_date()
                expiry_date = expiry.date() if hasattr(expiry, "date") else expiry
                days_to_expiry = (expiry_date - today).days
            except Exception:
                expiry_date = "?"
                days_to_expiry = "?"
            stranded_rows.append(
                dict(
                    instrument=instr,
                    contract=contract_date,
                    position=position,
                    priced=priced,
                    forward=fwd,
                    expiry=expiry_date,
                    days_to_expiry=days_to_expiry,
                )
            )

        if len(legs) > 1:
            net = sum(position for _, position in legs)
            split_rows.append(
                dict(
                    instrument=instr,
                    legs=str(legs),
                    net=net,
                    priced=priced,
                    forward=fwd,
                    net_zero="YES <- watch" if net == 0 else "",
                )
            )

    return len(instruments), stranded_rows, split_rows


def stranded_legs_report(data: dataBlob = arg_not_supplied):
    if data is arg_not_supplied:
        data = dataBlob()

    instrument_count, stranded_rows, split_rows = _scan(data)

    report = [header("Stranded legs report"), INTRO_TEXT]

    if stranded_rows:
        report.append(
            body_text(
                "*** ACTION REQUIRED: %d stranded leg(s) found. Check FND / "
                "deliverability and clear manually (see "
                "scan_stranded_legs.py docstring). ***" % len(stranded_rows)
            )
        )
        report.append(
            table("Stranded legs (behind the roll)", pd.DataFrame(stranded_rows))
        )
    else:
        report.append(
            body_text(
                "No stranded legs found (%d instruments with positions scanned)."
                % instrument_count
            )
        )

    if split_rows:
        report.append(
            table(
                "Positions split across >1 contract (mid-roll)",
                pd.DataFrame(split_rows),
            )
        )
    else:
        report.append(body_text("No positions split across multiple contracts."))

    return report
