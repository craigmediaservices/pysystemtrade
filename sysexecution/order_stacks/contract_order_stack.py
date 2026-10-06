from copy import copy

from sysexecution.orders.named_order_objects import missing_order
from sysexecution.order_stacks.order_stack import orderStackData, missingOrder

from sysexecution.orders.contract_orders import contractOrder


class contractOrderStackData(orderStackData):
    def _name(self):
        return "Contract order stack"

    def add_controlling_algo_ref(self, order_id: int, control_algo_ref: str):
        """
        Claim the order for an algo (or for the order generator's canceller).

        The claim is a compare-and-set in the store, not read-check-overwrite:
        the stack handler and the order generator can try to claim the same
        contract order within milliseconds, and with a plain overwrite both
        would 'succeed' (a possible double trade). Losing the race raises,
        exactly as finding the order already controlled does.

        :param order_id: int
        :param control_algo_ref: str or None
        :return:
        """
        if control_algo_ref is None:
            return self.release_order_from_algo_control(order_id)

        existing_order = self.get_order_with_id_from_stack(order_id)
        if existing_order is missing_order:
            error_msg = (
                "Can't add controlling algo as order %d doesn't exist" % order_id
            )
            self.log.warning(error_msg)
            raise missingOrder(error_msg)

        try:
            # same checks as before, on what we read (fast, clear message)...
            modified_order = copy(existing_order)
            modified_order.add_controlling_algo_ref(control_algo_ref)
            if existing_order.is_order_locked():
                raise Exception("Can't change locked order %s" % str(existing_order))

            # ... then the claim itself, which only succeeds if the order is
            # still unclaimed (or already ours) and unlocked in the store
            claimed = self._claim_order_for_algo_if_unclaimed(
                order_id, control_algo_ref
            )
            if not claimed:
                current_order = self.get_order_with_id_from_stack(order_id)
                current_ref = (
                    "unknown (order gone)"
                    if current_order is missing_order
                    else current_order.reference_of_controlling_algo
                )
                raise Exception(
                    "Already controlled by %s (claimed concurrently)" % current_ref
                )
        except Exception as e:
            error_msg = "%s couldn't add controlling algo %s to order %d" % (
                str(e),
                control_algo_ref,
                order_id,
            )
            self.log.warning(
                error_msg,
                **existing_order.log_attributes(),
                method="temp",
            )
            raise Exception(error_msg)

    def release_order_from_algo_control(self, order_id: int):
        existing_order = self.get_order_with_id_from_stack(order_id)
        if existing_order is missing_order:
            error_msg = (
                "Can't add controlling algo as order %d doesn't exist" % order_id
            )
            self.log.warning(error_msg)
            raise missingOrder(error_msg)

        order_is_not_controlled = not existing_order.is_order_controlled_by_algo()
        if order_is_not_controlled:
            # No change required
            return None

        try:
            modified_order = copy(existing_order)
            modified_order.release_order_from_algo_control()
            self._change_order_on_stack(order_id, modified_order)
        except Exception as e:
            error_msg = "%s couldn't remove controlling algo from order %d" % (
                str(e),
                order_id,
            )
            self.log.warning(
                error_msg,
                **existing_order.log_attributes(),
                method="temp",
            )
            raise Exception(error_msg)

    def _claim_order_for_algo_if_unclaimed(
        self, order_id: int, control_algo_ref: str
    ) -> bool:
        """
        MUST be atomic in the data implementation: set the order's
        reference_of_controlling_algo to control_algo_ref only if, in the
        store, the order exists, is not locked, and is either uncontrolled or
        already controlled by control_algo_ref. Change nothing else. Return
        True if the condition held (the order is now ours), else False.
        """
        raise NotImplementedError

    def get_order_with_id_from_stack(self, order_id: int) -> contractOrder:
        # probably will be overridden in data implementation
        # only here so the appropriate type is shown as being returned

        order = self.stack.get(order_id, missing_order)

        return order

    def does_stack_have_orders_for_instrument_code(self, instrument_code: str) -> bool:
        orders_with_instrument_code = self.list_of_orders_with_instrument_code(
            instrument_code
        )
        return len(orders_with_instrument_code) > 0

    def list_of_orders_with_instrument_code(self, instrument_code: str) -> list:
        list_of_orders = self.get_list_of_orders()
        list_of_orders = [
            order
            for order in list_of_orders
            if order.instrument_code == instrument_code
        ]

        return list_of_orders
