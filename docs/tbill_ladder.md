# T-bill ladder report

A read-only report on Treasury bill / bond holdings in the IBKR account, to
support a manual monthly T-bill ladder. It never trades — placing and rolling
bills stays a manual task you do in the IBKR ticket as part of your monthly
admin session (roll futures → sweep FX → check cash → check/redeploy bills).

## The ladder we're aiming at

**Standard 6-month ladder.** Buy one ~6-month T-bill each month. After ~6
months a rung matures every month, average duration ~3 months, essentially
zero rate risk, and you capture roughly the 6-month bill rate (usually the
steepest part of the front curve). Great liquidity — if you need collateral
back, a rung is always close to maturing.

The report doesn't enforce this — it just shows you what you hold so the
monthly buy decision is easy.

## What the report shows

- **Treasury holdings table** — one row per bill/bond: symbol/CUSIP, secType,
  maturity date, days to maturity, face value, mark price, market value, and
  an *approximate* bond-equivalent yield (computed from price + days for
  zero-coupon bills; blank for coupon bonds — coupon rate is shown instead).
  Sorted by days to maturity, so the next rung to mature is at the top.
- **Total face / market value / rung count.**
- **Cash balance (USD)** — idle securities-segment USD cash is roughly what's
  available to fund the next rung. (Note: this is the total USD cash balance
  from the broker; if your account is segmented you may want to eyeball the
  securities-segment figure in TWS rather than trust this exactly.)
- **MATURING SOON** — any rung maturing within `tbill_maturing_soon_days`
  (default 35), so a maturing rung shows up in the nightly email *before* the
  cash lands and you can line up the replacement.
- **Ladder shape** — face by maturity month, so gaps in the ladder are
  obvious at a glance.
- **Short-term Treasury ETFs** — VGSH, SGOV, BIL, SHV, etc. are listed as
  "near-cash" (currently you hold VGSH and no bills; over time you may want to
  rotate the ETF into the bill ladder). Edit `NEAR_CASH_ETF_SYMBOLS` in
  `sysproduction/reporting/data/bond_holdings.py` to add tickers.

## Running it

It's registered in `report_config_defaults` and added to `run_reports` in
`private_control_config.yaml`, so it goes out with the nightly emailed report
batch automatically — nothing to set up.

Ad hoc:

```bash
bond_ladder_report         # launcher on PATH (sysproduction/linux/scripts)
# press Enter at the "Argument data" prompt
```

or:

```bash
cd ~/pysystemtrade && python3 -m sysproduction.reporting.bond_ladder_report
```

It needs the IB gateway up (it reads `ib.portfolio()` and contract details);
it does not need bond trading permissions — purely read-only.

## Configuration

In `private/private_config.yaml` (gitignored — local only):

```yaml
# T-bill ladder report: flag bills maturing within this many days. Default: 35
tbill_maturing_soon_days: 35
```

In `private/private_control_config.yaml`, under
`process_configuration_methods: run_reports:`:

```yaml
    bond_ladder_report:
      max_executions: 1
```

## What is deliberately NOT built

- **No auto-roll / scheduled bond buying.** A maturing bill isn't time-
  critical to the minute, tenor choice and the new-issue auction schedule are
  judgment calls, and a bad automated bond order is a worse failure mode than
  a bad automated FX sweep. ~15 min/month manually is the right answer.
- **No order wrapper (yet).** IBKR's Treasury order ticket is fine for one
  buy a month, and `interactive_order_stack` has no bond support to wrap. If
  this ever changes (e.g. you want a guided "buy a ~6-month bill of face $X"
  helper), it would slot in next to `interactive_fx_sweep` — but it's not
  worth it for ~12 orders a year.

## Files

| File | Purpose |
| --- | --- |
| `sysproduction/reporting/bond_ladder_report.py` | the emailed/ad-hoc report |
| `sysproduction/reporting/data/bond_holdings.py` | IB position fetch + ladder/yield helpers (math is unit-tested) |
| `sysproduction/reporting/data/tests/test_bond_holdings.py` | tests for the date/yield/ladder math |
| `sysproduction/reporting/report_configs.py` | registers `bond_ladder_report_config` |
| `sysproduction/linux/scripts/bond_ladder_report` | PATH launcher |
| `private/private_config.yaml` | `tbill_maturing_soon_days` (local, not in git) |
| `private/private_control_config.yaml` | adds `bond_ladder_report` to `run_reports` (local, not in git) |
