"""
Read-only: compare roll dates under two roll-config CSVs before changing
RollOffsetDays, so a change cannot strand a held contract behind a roll date
that is already in the past.

    python3 sysproduction/maintenance/roll_offset_check.py \\
        --before ~/pysystemtrade/data/futures/csvconfig/rollconfig.csv \\
        --after /path/to/branch/data/futures/csvconfig/rollconfig.csv \\
        [--instruments PALLAD,PLAT,GOLD]

Production reads roll parameters from rollconfig.csv (csvRollParametersData),
not the database, so there is nothing to write: this only reports.
Defaults to every instrument whose RollOffsetDays differs between the files.
"""
import datetime
import sys

from syscore.exceptions import ContractNotFound
from sysdata.csv.csv_roll_parameters import allRollParameters
from sysdata.data_blob import dataBlob
from sysobjects.rolls import contractDateWithRollParameters
from sysproduction.data.contracts import dataContracts
from sysproduction.data.positions import diagPositions


def _arg(flag: str, default=None):
    if flag not in sys.argv:
        return default
    return sys.argv[sys.argv.index(flag) + 1]


def changed_instruments(before: allRollParameters, after: allRollParameters) -> list:
    return sorted(
        ic
        for ic in after.index
        if ic in before.index
        and before.loc[ic].RollOffsetDays != after.loc[ic].RollOffsetDays
    )


def report_instrument(data, ic, before, after, today):
    dc, dp = dataContracts(data), diagPositions(data)
    old_rp = before.get_roll_parameters_for_instrument(ic)
    new_rp = after.get_roll_parameters_for_instrument(ic)
    print(
        "\n== %s RollOffsetDays %d -> %d  (roll state %s)"
        % (
            ic,
            old_rp.roll_offset_day,
            new_rp.roll_offset_day,
            dp.get_name_of_roll_state(ic),
        )
    )
    positions = {
        c.date_str: dp.get_position_for_contract(c)
        for c in dc.get_all_contract_objects_for_instrument_code(ic)
    }
    # the next three held contracts from the priced one, plus anything we hold
    priced = dc.get_priced_contract_id(ic)
    chain = contractDateWithRollParameters(
        dc.get_contract_from_db_given_code_and_id(ic, priced).contract_date, new_rp
    )
    to_show = []
    for _ in range(3):
        to_show.append(chain.contract_date.date_str)
        chain = chain.next_held_contract()
    to_show += [cd for cd, pos in positions.items() if pos and cd not in to_show]

    flags = []
    for contract_id in sorted(to_show):
        try:
            contract = dc.get_contract_from_db_given_code_and_id(ic, contract_id)
        except ContractNotFound:
            print("   %s not in the database yet" % contract_id)
            continue
        old = contractDateWithRollParameters(contract.contract_date, old_rp)
        new = contractDateWithRollParameters(contract.contract_date, new_rp)
        position = positions.get(contract_id, 0)
        past = new.desired_roll_date.date() < today
        print(
            "   %s expiry %s  roll %s -> %s  position %d%s"
            % (
                contract_id,
                contract.expiry_date.date(),
                old.desired_roll_date.date(),
                new.desired_roll_date.date(),
                position,
                "  ** NEW ROLL DATE IN THE PAST **" if past else "",
            )
        )
        if past and position:
            flags.append("%s/%s" % (ic, contract_id))
    return flags


def main():
    before = allRollParameters.read_from_file(_arg("--before"))
    after = allRollParameters.read_from_file(_arg("--after"))
    instruments = _arg("--instruments")
    instruments = (
        instruments.split(",") if instruments else changed_instruments(before, after)
    )
    today = datetime.date.today()
    flags = []
    with dataBlob(log_name="roll_offset_check") as data:
        for ic in instruments:
            flags += report_instrument(data, ic, before, after, today)
    print("\nREAD ONLY - nothing written.")
    print("Held contracts stranded by the change: %s" % (flags or "none"))


if __name__ == "__main__":
    main()
