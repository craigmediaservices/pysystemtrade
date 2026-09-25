"""
Retire an earlier instrument order that a new order from the same strategy
supersedes, when the earlier order has not gone anywhere near the broker.

Why. The instrument order stack nets a new order against whatever is already
on the stack for the same strategy and instrument ("residual = wanted -
existing"), and the residual can change sign. So if the generator placed
"sell 1" at 02:51, the market was shut, and at 10:51 the generator wants no
trade after all, the stack does not cancel the sell: it adds a "buy 1" next
to it. Both later execute, paying two spreads to end up flat. Measured over
a year of the order database (2026-09-25): 165 such pairs, 12.6% of all
contracts traded, about 11% of total trading cost. In 92% of the pairs the
earlier order was pure database state - no broker order existed.

What this does. Before the new order is netted, look at the strategy's
existing orders for the instrument. If every one of them is untouched
(zero fill, no broker child anywhere, no algo working it), retire them with
a zero fill through the same completion path that the end-of-day stack
clean-up uses for every unfilled order, and let the new order go on fresh.
Anything else - a fill, a broker order, an algo in control, a lock, a manual
limit order - is left exactly as it was, and the stack's residual logic runs
as before.

Race with the stack handler. It may start trading a contract order at any
moment while the market is open. It only takes contract orders with no
controlling algo, and it records its algo on the order before sending
anything, so the sequence is: claim each contract order with our own marker
(fails if an algo already has it), re-read the family, and only then retire
it. If the claim or the re-read fails, release and fall back to the residual
behaviour; if retiring fails part-way, the caller must not place the new
order this run (the family self-heals: the next run finds it again, and the
end-of-day clean-up archives it regardless).
"""
from sysexecution.orders.base_orders import Order
from sysexecution.orders.instrument_orders import instrumentOrder
from sysexecution.orders.named_order_objects import missing_order
from sysexecution.stack_handler.completed_orders import stackHandlerForCompletions

CANCEL_REF = "cancel_unsubmitted"


class orderInFlight(Exception):
    pass


def family_is_unsubmitted(
    instrument_order: Order,
    contract_orders: list,
    broker_orders: list,
    new_order: instrumentOrder,
    own_ref: str = CANCEL_REF,
) -> bool:
    """
    Pure. True only if nothing has happened to this instrument order beyond
    (possibly) spawning contract children that were never sent anywhere.

    contract_orders: the instrument order's children as read from the stack
        (missing_order for any id that could not be found)
    broker_orders: every broker order whose parent is one of those children,
        whether or not the child knows about it yet
    """
    if instrument_order is missing_order:
        return False
    if not instrument_order.active:
        return False
    if not instrument_order.fill_equals_zero():
        return False
    if instrument_order.is_order_locked():
        return False
    # only the generator's own kind of order: a manual limit or market order
    # placed under the strategy's name is a deliberate instruction
    if str(instrument_order.order_type) != str(new_order.order_type):
        return False
    if len(broker_orders) > 0:
        return False

    for contract_order in contract_orders:
        if contract_order is missing_order:
            return False
        if not contract_order.fill_equals_zero():
            return False
        if contract_order.is_order_locked():
            return False
        if not contract_order.no_children():
            return False
        if contract_order.is_order_controlled_by_algo():
            # our own marker left by an earlier attempt is fine
            if contract_order.reference_of_controlling_algo != own_ref:
                return False

    return True


class unsubmittedOrderCanceller(object):
    def __init__(self, data):
        # stackHandlerCore only builds the three stacks; the broker
        # connection is a lazy property that nothing here touches
        self._completions = stackHandlerForCompletions(data)

    @property
    def completions(self) -> stackHandlerForCompletions:
        return self._completions

    @property
    def instrument_stack(self):
        return self.completions.instrument_stack

    @property
    def contract_stack(self):
        return self.completions.contract_stack

    @property
    def broker_stack(self):
        return self.completions.broker_stack

    @property
    def log(self):
        return self.completions.log

    def cancel_orders_superseded_by(self, new_order: instrumentOrder) -> list:
        """
        Returns the ids of the instrument orders retired. An empty list means
        the stack's normal residual logic should run. Raises only if retiring
        failed part-way, in which case the caller must NOT place new_order.
        """
        families = self._untouched_families_for(new_order)
        if len(families) == 0:
            return []

        claimed = self._claim_contract_orders(families, new_order)
        if not claimed:
            return []

        retired = []
        for instrument_order in families:
            self.completions.handle_completed_instrument_order(
                instrument_order.order_id, allow_zero_completions=True
            )
            retired.append(instrument_order.order_id)
            self.log.warning(
                "Retired unfilled order %s (never sent to the broker) because the "
                "strategy now wants %s; placing that instead of netting the two"
                % (str(instrument_order), str(new_order.trade)),
                **new_order.log_attributes(),
                method="temp",
            )

        return retired

    def _untouched_families_for(self, new_order: instrumentOrder) -> list:
        existing_ids = self.instrument_stack._get_list_of_orderids_with_same_tradeable_object_on_stack(
            new_order
        )
        if existing_ids is missing_order:
            return []

        families = []
        for order_id in existing_ids:
            instrument_order = self.instrument_stack.get_order_with_id_from_stack(
                order_id
            )
            if not self._family_is_unsubmitted(instrument_order, new_order):
                # something is in flight for this instrument: leave ALL of it
                # to the residual logic, which knows how to net against it
                return []
            families.append(instrument_order)

        return families

    def _family_is_unsubmitted(
        self, instrument_order: Order, new_order: instrumentOrder
    ) -> bool:
        contract_orders = self._contract_children(instrument_order)
        broker_orders = self._broker_orders_with_parent_in(contract_orders)
        return family_is_unsubmitted(
            instrument_order, contract_orders, broker_orders, new_order
        )

    def _contract_children(self, instrument_order: Order) -> list:
        if instrument_order is missing_order or instrument_order.no_children():
            return []
        return [
            self.contract_stack.get_order_with_id_from_stack(child_id)
            for child_id in instrument_order.children
        ]

    def _broker_orders_with_parent_in(self, contract_orders: list) -> list:
        parent_ids = set(
            contract_order.order_id
            for contract_order in contract_orders
            if contract_order is not missing_order
        )
        if len(parent_ids) == 0:
            return []
        # the broker stack only ever holds today's orders, so scanning it is cheap;
        # this catches a broker order created before its parent was told about it
        return [
            broker_order
            for broker_order in self.broker_stack.get_list_of_orders(
                exclude_inactive_orders=False
            )
            if broker_order.parent in parent_ids
        ]

    def _claim_contract_orders(
        self, families: list, new_order: instrumentOrder
    ) -> bool:
        """
        Mark every contract child with our own controlling-algo reference so
        the stack handler will not start trading it, then check nothing
        changed underneath us. False (after releasing) means fall back.
        """
        claimed = []
        try:
            for instrument_order in families:
                for contract_order in self._contract_children(instrument_order):
                    self.contract_stack.add_controlling_algo_ref(
                        contract_order.order_id, CANCEL_REF
                    )
                    claimed.append(contract_order.order_id)

            for instrument_order in families:
                fresh = self.instrument_stack.get_order_with_id_from_stack(
                    instrument_order.order_id
                )
                if not self._family_is_unsubmitted(fresh, new_order):
                    raise orderInFlight(str(fresh))
        except Exception as e:
            self.log.warning(
                "Not retiring earlier order(s) %s, will net against them instead: %s"
                % ([o.order_id for o in families], str(e))
            )
            self._release(claimed)
            return False

        return True

    def _release(self, claimed: list):
        for contract_order_id in claimed:
            try:
                self.contract_stack.release_order_from_algo_control(contract_order_id)
            except Exception as e:
                self.log.warning(
                    "Could not release %s from %s: %s"
                    % (contract_order_id, CANCEL_REF, str(e))
                )
