"""
Interactive helper to sweep large non-base currency cash balances back to base.

This is a thin wrapper around the existing broker FX plumbing:
 - balances come from dataBroker.broker_fx_balances()
 - market sweeps go through dataBroker.broker_fx_market_order() (same path as
   interactive_order_stack -> create FX trade)
 - limit-at-bid/ask sweeps are placed directly via the IB connection
   (ib_insync LimitOrder) so no changes to the core broker code are needed

Nothing is ever placed without an explicit "Y" confirmation per trade, and
there's a dry-run mode that just prints what it would do. Manual run only -
this is NOT wired into any automated process.

Run via: interactive_fx_sweep   (linux/scripts launcher, on PATH)
      or: python -m sysproduction.interactive_fx_sweep
"""

from syscore.constants import arg_not_supplied
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
)

# how long to wait for an FX quote when pricing a limit order
QUOTE_WAIT_SECONDS = 4


def interactive_fx_sweep(data: dataBlob = arg_not_supplied):
    if data is arg_not_supplied:
        data = dataBlob()

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

    suggestions_df = get_fx_sweep_suggestions(balances_df, base_currency, threshold)
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

    default_account = data_broker.get_broker_account()
    broker_account = get_input_from_user_and_convert_to_type(
        "Account ID",
        type_expected=str,
        allow_default=True,
        default_value=default_account,
    )

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

        if use_limit:
            _place_fx_limit_order(
                data=data,
                ccy1=ccy1,
                ccy2=ccy2,
                trade_qty=trade_qty,
                account_id=broker_account,
            )
        else:
            result = data_broker.broker_fx_market_order(
                trade_qty, ccy1, account_id=broker_account, ccy2=ccy2
            )
            print("Submitted: %s" % str(result))

    print(
        "\nDone. Check the broker for fills. Re-run this tool (or the FX balance "
        "report) afterwards to confirm balances.\n"
    )
    return None


def _place_fx_limit_order(
    data: dataBlob, ccy1: str, ccy2: str, trade_qty: int, account_id: str
):
    """
    Place a spot FX limit order directly via the IB connection.

    Priced at the current bid when selling ccy1, the current ask when buying -
    i.e. a passive order resting on our side of the book.
    """
    # imported here so the module still imports if ib_insync isn't installed
    from ib_insync import Forex, LimitOrder

    ib = data.ib_conn.ib

    contract = Forex(ccy1 + ccy2)
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        print("Could not qualify IB contract for %s%s - skipped." % (ccy1, ccy2))
        return None
    contract = qualified[0]

    ticker = ib.reqMktData(contract, "", False, False)
    ib.sleep(QUOTE_WAIT_SECONDS)
    bid = ticker.bid
    ask = ticker.ask
    ib.cancelMktData(contract)

    def _bad(px):
        return px is None or px != px or px <= 0  # None or NaN or non-positive

    side = "SELL" if trade_qty < 0 else "BUY"
    limit_price = bid if side == "SELL" else ask
    if _bad(limit_price):
        print(
            "No usable %s quote for %s%s (bid=%s ask=%s) - skipped, do it manually."
            % ("bid" if side == "SELL" else "ask", ccy1, ccy2, bid, ask)
        )
        return None

    print(
        "Current %s%s quote: bid=%s ask=%s -> limit %s @ %s"
        % (ccy1, ccy2, bid, ask, side, limit_price)
    )
    if not true_if_answer_is_yes(
        "Confirm LIMIT %s %s %s%s @ %s? (y/n) "
        % (side, format(abs(trade_qty), ","), ccy1, ccy2, limit_price)
    ):
        print("Skipped.")
        return None

    order = LimitOrder(side, abs(trade_qty), limit_price)
    order.account = account_id
    trade = ib.placeOrder(contract, order)
    print("Submitted limit order: %s" % str(trade))
    return None


if __name__ == "__main__":
    interactive_fx_sweep()
