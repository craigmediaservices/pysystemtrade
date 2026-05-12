# FX balance report & interactive FX sweep

Futures trades settle variation margin in the contract's currency, so an
IBKR account that trades non-USD futures slowly accumulates foreign cash
balances. These two tools help you notice and clear them.

Nothing here is automated trading: the report only *reports*, and the sweep
tool never places an order without an explicit `y` confirmation per trade.

## 1. FX balance report (`fx_balance_report`)

Lists broker cash balances by currency, valued in base currency (USD), shows
the margin currently in use, and flags any non-base balance worth more than
`fx_balance_alert_threshold` (see config below) as an **ACTION REQUIRED** row
with the trade that would flatten it.

It is registered in `report_config_defaults` and added to `run_reports` in
`private_control_config.yaml`, so it goes out with the nightly emailed report
batch alongside the P&L / risk / etc. reports — no extra setup.

Run it ad hoc:

```bash
fx_balance_report          # launcher on PATH (sysproduction/linux/scripts)
# press Enter at the "Argument data" prompt to use a fresh dataBlob
```

or:

```bash
cd ~/pysystemtrade && python3 -m sysproduction.reporting.fx_balance_report
```

Note: the report values balances using the *database* FX rates (the ones
updated nightly by `update_fx_prices`), so expect minor drift vs the live
broker quote. Good enough for an alert.

## 2. Interactive FX sweep (`interactive_fx_sweep`)

A wrapper that walks you through trading foreign balances back to USD.

```bash
interactive_fx_sweep       # launcher on PATH
# or: cd ~/pysystemtrade && python3 -m sysproduction.interactive_fx_sweep
```

What it does, in order:

1. Prints current FX balances and the margin currently used.
2. Asks for a threshold (defaults to `fx_balance_alert_threshold`) — only
   balances worth more than this in USD are offered for sweeping.
3. Shows the suggested sweep trades. A **negative** `approx_trade_qty` means
   *sell* that currency vs USD; positive means *buy* it (to clear a negative
   balance).
4. Asks **"Dry run only?"** — answer `y` to just see what it would do and
   stop. **Do a dry run first.**
5. Asks for the account ID (defaults to your configured broker account).
6. If not a dry run, asks **"Use LIMIT orders at current bid/ask instead of
   MARKET?"**
   - **MARKET** (answer `n`): placed via `dataBroker.broker_fx_market_order()`
     — the same code path as `interactive_order_stack` → create FX trade.
   - **LIMIT** (answer `y`): for each trade it reads the live `CCYUSD`
     bid/ask, prices a passive limit order at the **bid when selling** /
     **ask when buying**, shows it, and asks you to confirm the price. If
     there's no usable quote it skips that one and tells you to do it
     manually. Limit orders are placed directly via the IB connection
     (`ib_insync.LimitOrder`) — no changes to the core broker code.
7. For each suggested trade it asks **"Place this order?"** (and, for limit
   orders, a second confirmation showing the exact limit price). Anything you
   answer `n` to is skipped.

After it finishes, re-run `interactive_fx_sweep` (or `fx_balance_report`) to
confirm the balances came down.

### Caveats

- **Leave enough non-base cash for margin.** If you flatten a currency
  completely and that currency's futures take variation margin the next day,
  the balance goes negative and IBKR charges debit interest. The tool prints
  the margin in use so you can judge how much foreign cash to keep — when in
  doubt, sweep *less* than the full balance (you can edit the qty by skipping
  the suggestion and using `interactive_order_stack` → create FX trade for a
  custom amount).
- **Small FX trades are inefficient.** Below roughly USD 25k, IBKR's
  effective spread / minimum commission make the trade relatively expensive.
  Consider setting the threshold to 25000 so you only ever sweep in real lot
  sizes.
- Limit orders rest on the book and may not fill — check the broker and
  cancel/replace if needed. The tool places the order and returns; it does
  not babysit it.

## 3. Configuration

In `private/private_config.yaml` (gitignored — local only):

```yaml
# flag / offer to sweep any non-base (non-USD) balance worth more than this
# many base-currency units. If this line is absent, defaults to 10000.
fx_balance_alert_threshold: 10000
```

In `private/private_control_config.yaml`, under `process_configuration_methods:
run_reports:` (added so the report is emailed nightly):

```yaml
    fx_balance_report:
      max_executions: 1
```

## 4. Files

| File | Purpose |
| --- | --- |
| `sysproduction/reporting/fx_balance_report.py` | the emailed/ad-hoc report |
| `sysproduction/reporting/data/fx_balances.py` | balance/sweep helper functions (unit-tested) |
| `sysproduction/reporting/data/tests/test_fx_balances.py` | tests for the sweep arithmetic |
| `sysproduction/reporting/report_configs.py` | registers `fx_balance_report_config` |
| `sysproduction/interactive_fx_sweep.py` | the interactive sweep wrapper |
| `sysproduction/linux/scripts/fx_balance_report` | PATH launcher for the report |
| `sysproduction/linux/scripts/interactive_fx_sweep` | PATH launcher for the sweep tool |
| `private/private_config.yaml` | `fx_balance_alert_threshold` (local, not in git) |
| `private/private_control_config.yaml` | adds `fx_balance_report` to `run_reports` (local, not in git) |
