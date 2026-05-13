# Upgrade plan: merge `pst-group/develop` into local `develop`

**Branch:** `sync/upgrade-to-pst-group` (worktree at `~/pysystemtrade-merge`)
**Status:** in progress
**Date started:** 2026-05-13

## Goal

Bring the live trading server up to date with the canonical upstream
(`pst-group/pysystemtrade`). This is **170 commits behind** when this plan
was written. The headline item is the **`ib_insync` → `ib_async` migration**
(upstream PR #1620), since `ib_insync` is no longer maintained (archived
late 2023) and the live system will eventually break on a newer TWS/Gateway
or Python version without it.

Lots of smaller bug fixes ride along: KRWUSD CCY1/CCY2 fix, IB order
rejection parsing, IBOXX roll cycle, SGX STI multiplier, prod-logging fixes,
parquet access optimisation, NumPy/pandas deprecation cleanups, etc.

## Done-state definition

- `git log develop..pst-group/develop` returns nothing (we are at parity).
- Local-only commits (commission fixes, IG/HIGHYIELD rollconfig customisations,
  date-parsing fix in `ib_contracts_client.py`) either kept or explicitly
  superseded by upstream equivalents.
- Our recently-added features still work after the migration:
  - `fx_balance_report` runs and produces sensible output
  - `bond_ladder_report` runs and produces sensible output
  - `interactive_fx_sweep` imports without error (live placement not tested
    as part of this — done separately)
- `python3 -m pytest sysproduction/reporting/data/tests/` is green.
- `pip show ib_async` returns a version; `ib_insync` no longer referenced
  in any first-party module under `sysbrokers/`, `sysproduction/`,
  `sysexecution/`.
- Live IB connection: at least one read-only smoke test passes (run
  `fx_balance_report` against the live gateway and confirm it returns
  balances).

## Strategy

A **single merge** of `pst-group/develop` into `sync/upgrade-to-pst-group`,
not a 170-commit cherry-pick. We bring everything across at once, then
spend the time on conflict resolution and post-merge fix-up. This is
isolated to the new worktree (`~/pysystemtrade-merge`) so it cannot disturb
the running trader.

The user's working-tree-only modifications in `~/pysystemtrade` (171 dirty
files at start of work) are preserved — they live in the main worktree and
git never touches them during work in the merge worktree.

## Known conflicts going in

From the diffs already inspected:

| File | Why it conflicts | Resolution |
| --- | --- | --- |
| `sysproduction/reporting/data/commissions.py` | Local has our own commission-restricted-instruments filter (commits `06558555`, `947af1d1`); upstream has a squashed/cleaned-up version of the same fix (`ab37ef62`). | **Take upstream** — it's our own PR after a rebase by a maintainer, cleaner style (proper logger vs `print`). Drop the local commits' contribution to this file. |
| `sysbrokers/IB/config/ib_config_spot_FX.csv` | Local KRWUSD row is `KRW,USD,NO`; upstream is `USD,KRW,YES`. | **Take upstream** — IB now quotes the pair as `USD.KRW`; the local `NO` (no-invert) flag is also stale. |
| `data/futures/csvconfig/rollconfig.csv` | Local has `GOLD_micro=-30` and `IG` Priced=`HMUZ` (from local commit `8c8cea70`); upstream has `GOLD_micro=-40` (Rob's change `f693be18`) and IG Priced=`FGHJKMNQUVXZ`. | **GOLD_micro:** take upstream (-40 confirmed OK by user). **IG Priced:** the local change was put in to fix "can't find contract" errors — possibly correct, possibly the wrong workaround. **Take local** for now; revisit once we have the migration in and can test which behaves. Open a follow-up: re-check IG roll cycle after migration. |
| `sysbrokers/IB/client/ib_contracts_client.py` | Local has date-parsing fix from `8c8cea70`; upstream has different version because of the ib_async migration. | **Hand-merge.** Keep the local date-parse fix logic but apply on top of the ib_async-migrated version of the file. |
| `sysbrokers/IB/ib_broker_commissions.py` | Local has broker-account-in-test-orders fix from `8c8cea70`; upstream has ib_async-migrated version. | **Hand-merge** — keep the local logic. |
| `CLAUDE.md` | Local added a 124-line file; upstream doesn't have it. | **Keep local** — it's our project-specific guidance to Claude Code. |

There will almost certainly be more conflicts surfaced by the actual merge.
Each one gets logged in the "merge log" section below as it's encountered.

## What needs adapting AFTER the merge (in this branch)

These files were authored by us with `from ib_insync import …`; they need
the imports renamed to `ib_async` once the migration lands locally:

- `sysproduction/interactive_fx_sweep.py` — `from ib_insync import Forex, LimitOrder`
- `sysproduction/reporting/data/bond_holdings.py` — `from ib_insync import Contract`

Both are deferred imports inside functions, so the impact is localised.

## Test plan (before declaring this branch ready)

In the `~/pysystemtrade-merge` worktree:

1. `pip install ib_async`; confirm `ib_insync` can be uninstalled cleanly
   (or just left in place — the migration only changes imports).
2. Static checks: `python3 -c "import sysbrokers.IB.client.ib_client"` and
   a handful of other top-of-tree imports must succeed.
3. `python3 -m pytest sysproduction/reporting/data/tests/` — must pass.
4. Run the report smoke tests (with IB gateway up; uses a fresh client ID):
   - `python3 -m sysproduction.reporting.fx_balance_report` (run via
     `run_report` console form) — confirm balances come back, BASE filtered,
     EUR within buffer not flagged, KRW now valued correctly.
   - `python3 -m sysproduction.reporting.bond_ladder_report` — confirm VGSH
     ETF still shows up, cash line populated.
5. `python3 -c "from sysproduction.interactive_fx_sweep import interactive_fx_sweep"`
   and `from sysproduction.reporting.data.bond_holdings import get_bond_holdings_df`
   — both must import.
6. **Do NOT** run anything that places an order.

## Deployment (NOT done in this session)

Out of scope here. The user does this separately, in an off-hours window:

1. Stop the live trading processes (`run_stack_handler`, etc.).
2. `git checkout develop && git merge --ff-only sync/upgrade-to-pst-group`
   (after they've reviewed this branch).
3. `pip install ib_async`.
4. Bring the main worktree's local file modifications into harmony (some
   may now conflict with upstream files; some may be obsolete).
5. Run a final dry-run of reports against IB.
6. Restart trading processes.
7. Watch the logs.
8. Have a rollback path: `git reset --hard <pre-merge-sha>` and revert the
   pip install if anything goes sideways.

## Merge log

`git merge pst-group/develop` into clean `sync/upgrade-to-pst-group` branch
surfaced **3 conflicts** (much fewer than expected). All resolved:

| Conflict | Resolution | Notes |
| --- | --- | --- |
| `sysproduction/reporting/data/commissions.py` | `git checkout --theirs` (take upstream). | Upstream's `ab37ef62` is our own PR #1596 after a maintainer rebased it cleaner — proper logger instead of `print()`, `%` formatting, dropped unused `numpy` import. Local commits `06558555` + `947af1d1` are now superseded. |
| `data/futures/csvconfig/rollconfig.csv` (`IG` row only) | Took upstream: `IG,HMUZ,-5,1,HMUZ,3`. | Local had `HMUZ,...,FGHJKMNQUVXZ` (quarterly Hold but all-months Priced). User's earlier "can't find contract" note for IG is most consistent with Priced=FGHJKMNQUVXZ being wrong; restricting Priced to quarterly-only (HMUZ) matches what we already did for HIGHYIELD in `8c8cea70`. `GOLD_micro=-40` was auto-merged (both sides already converged). |
| `sysbrokers/IB/ib_broker_commissions.py` | Took upstream's version + removed unused `from sysbrokers.IB.ib_connection import get_broker_account` import that was only there for the local code path. | Same fix, cleaner: upstream uses `self.data.config.get_element("broker_account")` directly; local had a try/except wrapper around `get_broker_account()`. Functionally equivalent, upstream is tighter. |
| `sysbrokers/IB/client/ib_contracts_client.py` | Auto-merged. Cleaned up afterwards. | Local `8c8cea70` added a fix for "YYYYMMDD HH:MM:SS TZ" expiry-date strings; the upstream file independently got a similar split-on-space fix at a different call site (line ~671). Both survive. Removed the local DEBUG `print(...)` statements that were left in — they would spam echo files in production. |

## Post-merge state

- `git log develop..sync/upgrade-to-pst-group` → 169 commits ahead (the
  pst-group/develop history + our merge commit `8eed092a`).
- `pip show ib_async` → 2.1.0 installed locally.
- `pip show ib_insync` → still installed (0.9.86); we'll uninstall after the
  feature branches are rebased and confirmed clean.
- `python3 -m pytest` → **94 passed, 40 skipped, 3 xfailed** (22 sec).
- All key modules import: `sysbrokers.IB.*`, `sysproduction.run_reports`,
  `sysproduction.run_stack_handler`, `sysproduction.interactive_order_stack`.
- `ib_async.Trade` resolves correctly to `ib_async.order.Trade`.
- Live IB smoke test deferred — needs the user's `private/` config which
  isn't in the merge worktree (gitignored). Will happen as part of the
  deployment step in the user's main worktree.

## Outstanding follow-ups (not in this branch)

1. **Rebase the two stacked feature branches** onto this branch and update
   their `from ib_insync import …` lines to `from ib_async import …`:
   - `feature/fx-balance-sweep`: `sysproduction/interactive_fx_sweep.py`
     (`from ib_insync import Forex, LimitOrder`).
   - `feature/tbill-ladder-report`: `sysproduction/reporting/data/bond_holdings.py`
     (`from ib_insync import Contract`).
2. **Stale comment cleanup** in `syslogging/filters.py` — comment still
   references `ib_insync` (the code uses `ib_async`). Cosmetic only.
3. **Uninstall `ib_insync`** from the venv after step 1 is verified.
4. **Re-check the IG roll cycle** in production: the merge changed it from
   Priced=FGHJKMNQUVXZ to Priced=HMUZ. If it was the right fix, "can't find
   contract" errors should disappear. If wrong, will manifest as missing
   non-quarterly price samples.
5. **The old `sync/upstream-bug-fixes` branch** (with just the KRWUSD
   cherry-pick) is now redundant; KRWUSD is in pst-group/develop. Delete
   after merge confirmed.
