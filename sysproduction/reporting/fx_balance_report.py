from sysdata.data_blob import dataBlob

from syscore.constants import arg_not_supplied
from sysproduction.data.broker import dataBroker
from sysproduction.data.currency_data import dataCurrency
from sysproduction.reporting.api import reportingApi
from sysproduction.reporting.reporting_functions import table, body_text
from sysproduction.reporting.data.fx_balances import (
    get_fx_balances_as_df,
    get_fx_sweep_suggestions,
    get_fx_balance_alert_threshold,
    get_fx_balance_buffers,
)


def fx_balance_report(data: dataBlob = arg_not_supplied):
    """
    Reports broker cash balances by currency, valued in base currency, and
    flags any non-base balance large enough that you probably want to trade it
    back to base. Doesn't trade anything - use interactive_fx_sweep (or
    interactive_order_stack -> create FX trade) to actually do it.
    """
    if data is arg_not_supplied:
        data = dataBlob()

    reporting_api = reportingApi(data)
    currency_data = dataCurrency(data)
    data_broker = dataBroker(data)

    base_currency = currency_data.get_base_currency()
    threshold = get_fx_balance_alert_threshold(data)
    buffers = get_fx_balance_buffers(data)
    balances_df = get_fx_balances_as_df(data)
    suggestions_df = get_fx_sweep_suggestions(
        balances_df, base_currency, threshold, buffers=buffers
    )

    try:
        margin_used = data_broker.get_margin_used_in_base_currency()
        margin_text = (
            "Margin currently used: %s %s. Keep enough non-base cash around to cover "
            "variation margin / IB will charge interest on negative currency balances."
            % (base_currency, format(round(margin_used), ","))
        )
    except BaseException:
        margin_text = (
            "Could not read margin used - keep enough non-base cash around to cover "
            "variation margin / IB will charge interest on negative currency balances."
        )

    formatted_output = []
    formatted_output.append(reporting_api.terse_header("FX balance report"))
    formatted_output.append(
        body_text(
            "Base currency: %s. Alert threshold (config 'fx_balance_alert_threshold'): "
            "%s %s." % (base_currency, base_currency, format(round(threshold), ","))
        )
    )
    if buffers:
        buffers_text = ", ".join(
            "%s %s %s" % (ccy, base_currency, format(round(v), ","))
            for ccy, v in sorted(buffers.items())
        )
        formatted_output.append(
            body_text(
                "Per-currency margin buffers (config 'fx_balance_buffers'; long "
                "balances up to this much are treated as intentional and not "
                "flagged): %s." % buffers_text
            )
        )
    formatted_output.append(body_text(margin_text))

    formatted_output.append(table("Broker cash balances by currency", balances_df))

    if len(suggestions_df) == 0:
        formatted_output.append(
            body_text(
                "ACTION: none. No non-%s balance exceeds the alert threshold."
                % base_currency
            )
        )
    else:
        formatted_output.append(
            body_text(
                "ACTION REQUIRED: the balances below have excess (over their margin "
                "buffer) larger than the alert threshold - consider trading the "
                "excess back to %s. Suggested approx_trade_qty is the *excess* only; "
                "the buffer stays intact. Run interactive_fx_sweep (dry-run first) "
                "or interactive_order_stack -> create FX trade. A negative "
                "approx_trade_qty means SELL that currency vs %s."
                % (base_currency, base_currency)
            )
        )
        formatted_output.append(
            table("Suggested sweeps back to %s" % base_currency, suggestions_df)
        )

    formatted_output.append(reporting_api.footer())

    return formatted_output


if __name__ == "__main__":
    fx_balance_report()
