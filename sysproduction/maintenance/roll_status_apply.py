"""
Apply reviewed roll-state decisions through the production tool's own
modify_roll_state (what menu option 3 does after you confirm), with a fresh
position / order-stack snapshot immediately before every Roll_Adjusted.

    python3 sysproduction/maintenance/roll_status_apply.py \\
        --roll-adjusted BTP5,KOSPI --force TOPIX,AUD --force-outright KOSDAQ \\
        --close SOMETHING --no-open INR,COTTON --limit TOPIX:10 [--dry-run]

Roll_Adjusted preconditions (all must hold, else the instrument is skipped):
  * position in the priced contract == 0
  * no orphaned positions in other contracts
  * no UNFILLED order on the contract stack touching the priced contract
  * no fill today that OPENED a position in the priced contract
Force / Force_Outright / Close / No_Open are applied only if the tool lists
them as allowable for the current state. --limit raises a 1-day trade limit.
"""
import datetime
import sys

import pandas as pd

from sysdata.data_blob import dataBlob
from sysobjects.contracts import futuresContract
from sysobjects.production.roll_state import RollState
from sysproduction.data.contracts import dataContracts
from sysproduction.data.controls import dataTradeLimits
from sysproduction.data.orders import dataOrders
from sysproduction.data.positions import diagPositions
from sysproduction.interactive_update_roll_status import (
    modify_roll_state,
    setup_roll_data_with_state_reporting,
)

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", None)


def _arg_list(flag: str) -> list:
    if flag not in sys.argv:
        return []
    return [x for x in sys.argv[sys.argv.index(flag) + 1].split(",") if x]


def _stack_snapshot(do: dataOrders) -> dict:
    """instrument -> list of (contract_date, unfilled?) for contract-stack orders"""
    out = {}
    for oid in do.db_contract_stack_data.get_list_of_order_ids():
        o = do.db_contract_stack_data.get_order_with_id_from_stack(oid)
        unfilled = list(o.fill) != list(o.trade)
        out.setdefault(o.instrument_code, []).append((str(o.contract_date), unfilled))
    return out


def _fills_today(do: dataOrders) -> dict:
    today = datetime.datetime.combine(datetime.date.today(), datetime.time())
    out = {}
    for oid in do.get_historic_broker_order_ids_in_date_range(
        today, datetime.datetime.now()
    ):
        o = do.get_historic_broker_order_from_order_id(oid)
        out.setdefault(o.instrument_code, []).append(
            (str(o.contract_date), list(o.fill))
        )
    return out


def apply_roll_adjusted(data, ic, stack, fills, dry_run):
    dp, dc = diagPositions(data), dataContracts(data)
    rd = setup_roll_data_with_state_reporting(data, ic)
    priced = dc.get_priced_contract_id(ic)
    pos_priced = int(dp.get_position_for_contract(futuresContract(ic, priced)))
    unfilled_priced = [cd for cd, unf in stack.get(ic, []) if unf and priced in cd]
    opened_priced_today = [
        f for f in fills.get(ic, []) if f[0] == priced and pos_priced != 0
    ]
    print(
        "\n=== %s: state=%s priced=%s pos_priced=%d orphans=%s unfilled_in_priced=%s fills_today=%s"
        % (
            ic,
            rd.original_roll_status.name,
            priced,
            pos_priced,
            rd.orphaned_contract_positions,
            unfilled_priced,
            fills.get(ic),
        )
    )
    if (
        pos_priced != 0
        or rd.has_orphaned_positions
        or unfilled_priced
        or opened_priced_today
    ):
        print("   SKIP - precondition failed")
        return False
    if "Roll_Adjusted" not in rd.allowable_roll_states_as_list_of_str:
        print(
            "   SKIP - Roll_Adjusted not allowable from", rd.original_roll_status.name
        )
        return False
    if dry_run:
        print("   would Roll_Adjusted")
        return True
    modify_roll_state(
        data=data,
        instrument_code=ic,
        original_roll_state=rd.original_roll_status,
        roll_state_required=RollState.Roll_Adjusted,
        confirm_adjusted_price_change=False,
    )
    print(
        "   -> state %s, priced now %s"
        % (dp.get_roll_state(ic).name, dc.get_priced_contract_id(ic))
    )
    return True


def apply_state(data, ic, state: RollState, dry_run):
    dp = diagPositions(data)
    rd = setup_roll_data_with_state_reporting(data, ic)
    print(
        "\n=== %s: state=%s pos_priced=%d relvol=%.3f absvol=%d days_exp=%d -> %s"
        % (
            ic,
            rd.original_roll_status.name,
            rd.position_priced_contract,
            rd.relative_volume,
            rd.absolute_forward_volume,
            rd.days_until_expiry,
            state.name,
        )
    )
    if state.name == rd.original_roll_status.name:
        print("   already")
        return
    if state.name not in rd.allowable_roll_states_as_list_of_str:
        print("   SKIP - not allowable:", rd.allowable_roll_states_as_list_of_str)
        return
    if dry_run:
        print("   would set", state.name)
        return
    modify_roll_state(
        data=data,
        instrument_code=ic,
        original_roll_state=rd.original_roll_status,
        roll_state_required=state,
        confirm_adjusted_price_change=False,
    )
    print("   -> state now", dp.get_roll_state(ic).name)


def main():
    dry_run = "--dry-run" in sys.argv
    plan = [
        (RollState.Force, _arg_list("--force")),
        (RollState.Force_Outright, _arg_list("--force-outright")),
        (RollState.Close, _arg_list("--close")),
        (RollState.No_Open, _arg_list("--no-open")),
        (RollState.Passive, _arg_list("--passive")),
        (RollState.No_Roll, _arg_list("--no-roll")),
    ]
    roll_adj = _arg_list("--roll-adjusted")
    limits = _arg_list("--limit")

    with dataBlob(log_name="Interactive_Update-Roll-Status") as data:
        do = dataOrders(data)
        stack = _stack_snapshot(do)
        fills = _fills_today(do)
        print("fills today:", fills)
        print("contract stack:", stack)

        for spec in limits:
            ic, n = spec.split(":")
            print("\ntrade limit %s (1 day) -> %s" % (ic, n))
            if not dry_run:
                dataTradeLimits(data).update_instrument_limit_with_new_limit(
                    ic, 1, int(n)
                )

        print("\n########## ROLL_ADJUSTED ##########")
        for ic in roll_adj:
            apply_roll_adjusted(data, ic, stack, fills, dry_run)

        for state, names in plan:
            if not names:
                continue
            print("\n########## %s ##########" % state.name)
            for ic in names:
                apply_state(data, ic, state, dry_run)
    print("\nDONE%s" % (" (dry run)" if dry_run else ""))


if __name__ == "__main__":
    main()
