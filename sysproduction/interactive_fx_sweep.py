"""
Interactive helper to sweep large non-base currency cash balances back to base.

This is a thin wrapper around the existing broker FX plumbing:
 - balances come from dataBroker.broker_fx_balances()
 - orders are placed directly via the IB connection (ib_async LimitOrder /
   MarketOrder) on whichever direction of the pair IB lists (EUR.USD but
   USD.JPY), so no changes to the core broker code are needed

Nothing is ever placed without an explicit "Y" confirmation per trade, and
there's a dry-run mode that just prints what it would do. Manual run only -
this is NOT wired into any automated process.

Run via: interactive_fx_sweep   (linux/scripts launcher, on PATH)
      or: python -m sysproduction.interactive_fx_sweep
"""

from syscore.interactive.input import (
    get_input_from_user_and_convert_to_type,
    true_if_answer_is_yes,
)
from syscore.interactive.display import set_pd_print_options

from sysdata.data_blob import dataBlob
from sysproduction.data.broker import dataBroker
from sysproduction.data.currency_data import dataCurrency
from sysproduction.reporting.data.fx_balances import (
    get_fx_balances_as_df,
    get_fx_sweep_suggestions,
    get_fx_balance_alert_threshold,
    get_fx_balance_buffers,
)

# how long to wait for an FX quote when pricing a limit order
QUOTE_WAIT_SECONDS = 4


def interactive_fx_sweep():
    # no arguments: the linux/scripts launcher (run.py) prompts for every
    # function argument, so like the other interactive_* tools we build the
    # dataBlob ourselves
    with dataBlob(log_name="Interactive-FX-Sweep") as data:
        _interactive_fx_sweep(data)


def _interactive_fx_sweep(data: dataBlob):
    set_pd_print_options()

    data_broker = dataBroker(data)
    currency_data = dataCurrency(data)
    base_currency = currency_data.get_base_currency()

    print("\n=== FX sweep helper (base currency: %s) ===\n" % base_currency)

    balances_df = get_fx_balances_as_df(data)
    print("Current broker cash balances:")
    print(balances_df)
    print("")

    try:
        margin_used = data_broker.get_margin_used_in_base_currency()
        print(
            "Margin currently used: %s %s - leave enough non-base cash for "
            "variation margin (IB charges interest on negative balances)."
            % (base_currency, format(round(margin_used), ","))
        )
    except BaseException:
        print(
            "Could not read margin used - leave enough non-base cash for variation "
            "margin (IB charges interest on negative balances)."
        )
    print("")

    default_threshold = get_fx_balance_alert_threshold(data)
    threshold = get_input_from_user_and_convert_to_type(
        "Only suggest sweeping balances worth more than this (in %s)" % base_currency,
        type_expected=float,
        allow_default=True,
        default_value=default_threshold,
    )

    buffers = get_fx_balance_buffers(data)
    if buffers:
        print(
            "Per-currency margin buffers (long balances up to this much are kept intact):"
        )
        for ccy, v in sorted(buffers.items()):
            print("  %s: %s %s" % (ccy, base_currency, format(round(v), ",")))
        print("")

    suggestions_df = get_fx_sweep_suggestions(
        balances_df, base_currency, threshold, buffers=buffers
    )
    if len(suggestions_df) == 0:
        print(
            "\nNothing to do - no non-%s balance exceeds %s %s.\n"
            % (base_currency, base_currency, format(round(threshold), ","))
        )
        return None

    print("\nSuggested sweeps back to %s:" % base_currency)
    print(suggestions_df)
    print("(negative approx_trade_qty = SELL that currency vs %s)\n" % base_currency)

    dry_run = true_if_answer_is_yes("Dry run only (just print, place nothing)? (y/n) ")

    # same account the whole system trades on (broker_account in private_config)
    broker_account = data_broker.get_broker_account()
    print("Account: %s" % broker_account)

    use_limit = False
    if not dry_run:
        use_limit = true_if_answer_is_yes(
            "Use LIMIT orders at current bid/ask instead of MARKET? (y/n) "
        )

    for currency, row in suggestions_df.iterrows():
        ccy1 = currency
        ccy2 = base_currency
        trade_qty = int(row["approx_trade_qty"])

        side = "SELL" if trade_qty < 0 else "BUY"
        order_type = "LIMIT" if use_limit else "MARKET"
        print(
            "\n--- %s %s %s of %s vs %s (account %s) ---"
            % (
                order_type,
                side,
                format(abs(trade_qty), ","),
                ccy1,
                ccy2,
                broker_account,
            )
        )

        if dry_run:
            print("DRY RUN - not placed.")
            continue

        if not true_if_answer_is_yes("Place this order? (y/n) "):
            print("Skipped.")
            continue

        _place_fx_order(
            data=data,
            ccy1=ccy1,
            ccy2=ccy2,
            trade_qty=trade_qty,
            account_id=broker_account,
            use_limit=use_limit,
        )

    print(
        "\nDone. Check the broker for fills. Re-run this tool (or the FX balance "
        "report) afterwards to confirm balances.\n"
    )
    return None


def resolve_fx_order(
    ccy1: str, ccy2: str, trade_qty: int, inverted: bool, bid: float, ask: float
) -> tuple:
    """
    Pure helper (unit-tested). trade_qty is in ccy1 units: negative = sell ccy1
    for ccy2, positive = buy ccy1 with ccy2.

    IB only lists one direction of each pair (EUR.USD but USD.JPY). When the
    pair we can trade is the inverse (inverted=True, i.e. contract is ccy2.ccy1)
    the order is expressed in ccy2 units and the side flips: buying 2.8M JPY
    with USD is SELL USD.JPY for 2.8M / price USD.

    Returns (pair_symbol, action, quantity, limit_price). limit_price is the
    passive side of the book: bid when selling the pair, ask when buying.
    """
    if not inverted:
        action = "SELL" if trade_qty < 0 else "BUY"
        quantity = abs(int(trade_qty))
        pair = ccy1 + ccy2
    else:
        # we want to BUY ccy1 -> SELL the ccy2.ccy1 pair (and vice versa)
        action = "BUY" if trade_qty < 0 else "SELL"
        pair = ccy2 + ccy1
        mid = (bid + ask) / 2.0
        quantity = int(round(abs(trade_qty) / mid))
    limit_price = bid if action == "SELL" else ask
    return pair, action, quantity, limit_price


def _qualify_fx_contract(ib, ccy1: str, ccy2: str):
    """Return (contract, inverted). Tries ccy1.ccy2 then ccy2.ccy1."""
    from ib_async import Forex

    for inverted, symbol in ((False, ccy1 + ccy2), (True, ccy2 + ccy1)):
        contract = Forex(symbol)
        try:
            qualified = ib.qualifyContracts(contract)
        except BaseException:
            qualified = []
        if qualified and qualified[0] is not None and qualified[0].conId:
            return qualified[0], inverted
    return None, False


def _place_fx_order(
    data: dataBlob,
    ccy1: str,
    ccy2: str,
    trade_qty: int,
    account_id: str,
    use_limit: bool,
):
    """
    Place a spot FX order directly via the IB connection, on whichever
    direction of the pair IB lists. Limit orders rest on our side of the book
    (bid when selling, ask when buying); market orders use the same quote only
    to size inverted pairs.
    """
    # imported here so the module still imports if ib_async isn't installed
    from ib_async import LimitOrder, MarketOrder

    ib = data.ib_conn.ib

    contract, inverted = _qualify_fx_contract(ib, ccy1, ccy2)
    if contract is None:
        print(
            "IB lists neither %s%s nor %s%s - skipped, do it manually in TWS."
            % (ccy1, ccy2, ccy2, ccy1)
        )
        return None

    ticker = ib.reqMktData(contract, "", False, False)
    ib.sleep(QUOTE_WAIT_SECONDS)
    bid = ticker.bid
    ask = ticker.ask
    ib.cancelMktData(contract)

    def _bad(px):
        return px is None or px != px or px <= 0  # None or NaN or non-positive

    if _bad(bid) or _bad(ask):
        print(
            "No usable quote for %s (bid=%s ask=%s) - skipped, do it manually."
            % (contract.localSymbol, bid, ask)
        )
        return None

    pair, action, quantity, limit_price = resolve_fx_order(
        ccy1, ccy2, trade_qty, inverted, bid, ask
    )
    if inverted:
        print(
            "IB quotes this as %s, so %s %s of %s becomes %s %s %s"
            % (
                contract.localSymbol,
                "SELL" if trade_qty < 0 else "BUY",
                format(abs(trade_qty), ","),
                ccy1,
                action,
                format(quantity, ","),
                ccy2,
            )
        )
    print("Current %s quote: bid=%s ask=%s" % (contract.localSymbol, bid, ask))

    if use_limit:
        order = LimitOrder(action, quantity, limit_price)
        description = "LIMIT %s %s %s @ %s" % (
            action,
            format(quantity, ","),
            contract.localSymbol,
            limit_price,
        )
    else:
        order = MarketOrder(action, quantity)
        description = "MARKET %s %s %s" % (
            action,
            format(quantity, ","),
            contract.localSymbol,
        )

    if not true_if_answer_is_yes("Confirm %s? (y/n) " % description):
        print("Skipped.")
        return None

    order.account = account_id
    trade = ib.placeOrder(contract, order)
    ib.sleep(2)
    print(
        "Submitted %s: status=%s filled=%s"
        % (description, trade.orderStatus.status, trade.orderStatus.filled)
    )
    return trade


if __name__ == "__main__":
    interactive_fx_sweep()
