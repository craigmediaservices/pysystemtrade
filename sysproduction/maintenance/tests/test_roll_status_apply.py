"""
roll_status_apply must look at the contract order stack again immediately
before each Roll_Adjusted, not reuse a snapshot taken at start-up: the stack
handler keeps running while the tool works through the list, so an order can
land on a later instrument's priced contract after the run has started.
"""
from types import SimpleNamespace
from unittest import mock

from sysproduction.maintenance import roll_status_apply as rsa


class _Order:
    def __init__(self, instrument_code, contract_date):
        self.instrument_code = instrument_code
        self.contract_date = contract_date
        self.trade = [1]
        self.fill = [0]


class _Stack:
    """contract-order stack whose contents the test changes mid-run"""

    def __init__(self):
        self.orders = {}

    def get_list_of_order_ids(self):
        return list(self.orders)

    def get_order_with_id_from_stack(self, order_id):
        return self.orders[order_id]


def _run(argv, stack, on_modify):
    orders = SimpleNamespace(
        db_contract_stack_data=stack,
        get_historic_broker_order_ids_in_date_range=lambda *a: [],
    )
    roll_data = SimpleNamespace(
        original_roll_status=rsa.RollState.Roll_Adjusted,
        orphaned_contract_positions=[],
        has_orphaned_positions=False,
        allowable_roll_states_as_list_of_str=["Roll_Adjusted"],
    )
    contracts = SimpleNamespace(get_priced_contract_id=lambda ic: "20261200")
    positions = SimpleNamespace(
        get_position_for_contract=lambda contract: 0,
        get_roll_state=lambda ic: rsa.RollState.Roll_Adjusted,
    )
    modify = mock.Mock(side_effect=on_modify)
    with mock.patch.object(rsa, "dataBlob", mock.MagicMock()), mock.patch.object(
        rsa, "dataOrders", return_value=orders
    ), mock.patch.object(
        rsa, "dataContracts", return_value=contracts
    ), mock.patch.object(
        rsa, "diagPositions", return_value=positions
    ), mock.patch.object(
        rsa, "setup_roll_data_with_state_reporting", return_value=roll_data
    ), mock.patch.object(
        rsa, "modify_roll_state", modify
    ), mock.patch.object(
        rsa.sys, "argv", argv
    ):
        rsa.main()
    return [call.kwargs["instrument_code"] for call in modify.call_args_list]


def test_order_arriving_mid_run_blocks_the_later_roll():
    stack = _Stack()

    def stack_handler_adds_an_order(**kwargs):
        # while GOLD is being rolled, an order lands on PLAT's priced contract
        stack.orders[1] = _Order("PLAT", "20261200")

    rolled = _run(
        ["roll_status_apply.py", "--roll-adjusted", "GOLD,PLAT"],
        stack,
        stack_handler_adds_an_order,
    )
    assert rolled == ["GOLD"]


def test_clean_stack_rolls_every_instrument():
    rolled = _run(
        ["roll_status_apply.py", "--roll-adjusted", "GOLD,PLAT"],
        _Stack(),
        lambda **kwargs: None,
    )
    assert rolled == ["GOLD", "PLAT"]


def test_unfilled_order_on_priced_contract_blocks_the_roll():
    stack = _Stack()
    stack.orders[1] = _Order("GOLD", "20261200")
    rolled = _run(
        ["roll_status_apply.py", "--roll-adjusted", "GOLD"],
        stack,
        lambda **kwargs: None,
    )
    assert rolled == []
