# Nifty Interceptor

The strategy is **intraday Nifty option-premium Renko buying on Dhan**,
not the former Definedge straddle/PnF swing
strategy. Version: `premium-renko-levels-1pct-20-gain10-low5-2026-11-05`.

At cutover, suspend the old jobs and copy and verify trade history from
`nifty_swing_<user_name>` to `nifty_interceptor_<user_name>` while preserving the
old collection. The order collection, signal ID, strategy version and daily
trade keys are unchanged. Never run old and new live jobs together; copying
state is not synchronization.

Both jobs send strategy and broker-related Slack notifications to
`#nifty-interceptor`. The Slack bot must have access to that channel.

## Frozen rules

- At **09:30**, lock ATM from Nifty's completed **09:29 minute close**, rounded
  to the nearest50 strike. A restart uses that exact observation, never current
  spot. Same-day locks already published before this change are preserved.
- Select the nearest listed expiry **strictly more than6 calendar days away**,
  at that strike for both CE and PE. Freeze both contracts for the day.
- Use **available exact-contract candles from14 calendar days** of history,
  normal session09:15-15:29 only. Missing history is not forward-filled.
- Build **1% option-premium Renko** with the shared library.
- Buy the first observed CE/PE latest close strictly above its **prior20 bricks' high**.
  Exclude the current brick from the channel; use the last brick of a multi-brick
  minute. Only fresh signals at/after09:30 qualify.
- If both latest option levels qualify together, **skip the entire day**.
- Exit the bought option on a close strictly below the **prior20 bricks' low**,
  using the **prior5 bricks' low** instead when saved `max_pnl_reached` is
  **strictly greater than10%** of `total_investment`. Exactly10% still uses20.
  Exclude current brick; keep the post-entry brick condition.
  Exit at **15:15 IST** regardless. No target, fixed-rupee stop, partial booking, AFT/PCR,
  Supertrend filter or overnight holding.
- **At most one trade per user/day**, including restarts and duplicate job runs.
  Closed or aborted attempts consume the day.
- Last eligible signal minute15:13; an entry must occur in the next minute,
  strictly before15:15. After restart, evaluate the latest fresh levels, even if
  an earlier breakout was missed. No historical breakout/exit replay.

## November5 execution hardening

Both jobs pin `tamingnifty==2.4.0`, published from shared-library commit
`33ee1d2` and tag `v2.4.0`. The published wheel's broker source was verified
against that commit before pushing these consumer changes. Rebuild both images;
verify the actual Azure image configuration after the deployment workflow.

Live executor persists a prepared LIMIT request, correlation ID, fixed entry
price cap and deadline before sending. Initial BUY requires depth for the order,
spread <=0.75%, 0.1% price cushion and a cap 1% above initial depth price.
Entry expires after fifteen seconds or the existing entry-minute deadline, whichever
is earlier. Pending balance is cancelled and final fills confirmed; zero fills
abort with the daily slot consumed. A terminal partial entry is recorded at its
actual quantity and immediately scheduled for flattening, with actual-order
charges. Full fills racing cancellation are adopted, not ignored.

Shared library monitors/modifies the same order against depth roughly once per
two seconds. Exits retain closing state, alert after unresolved monitoring batches,
and resume monitoring until full fill or an error requiring reconciliation.
No automatic replacement follows rejection or an ambiguous submission. Lost
submission replies are recovered by correlation lookup; unresolved lookup blocks
resubmission. Legacy live intents without a saved execution policy require manual
reconciliation. Manual broker intervention must not compete with the executor.

Only pending/closing live states use a shorter outer-loop delay. Normal active
P&L/peak monitoring and paper mode retain the existing ten-second delay.
Signals, threshold semantics, daily cap and current-LTP paper fills are unchanged.
These are execution-policy changes, not results from the historical backtest.
There is no guarantee of a fill or a bounded exit loss. Verify market-depth
entitlement, account-wide rate limits, registered outgoing IP and actual installed
library in the image before a supervised pilot.

From the shared library repository, run `python test_execution.py --swing` for
offline library and swing-recovery checks.

## November5 profit-activated exit change

Owner requested the simpler implementation: the signal adds only prior5-low
for each option. Executor uses existing saved gross `max_pnl_reached` and
`total_investment` with ordinary conditionals: above10% use5-low,otherwise20.
No new trade-state field,activation helper or raw-minute fields.

Existing P&L monitoring already saves maxima with Mongo `$max`,so5 remains
selected after a profit decline or restart. Exit checks use the previously
saved peak;the existing subsequent LTP mark updates it for the next poll.
Accounting,one-trade cap,current-LTP paper fills and real-order mode are unchanged.
Peaks missed during an outage cannot be recovered.

This samples gross LTP P&L,not completed-minute closes. Earlier researched
results used completed-close activation and do not establish exact results
for this revised rule. Scalar database maxima do not record threshold-crossing
time,so they cannot retrospectively reconstruct its precise exit sequence.

Cut over **both jobs between sessions while flat**. Old-version open trades
are deliberately rejected, not silently migrated. A same-day old-version
signal lock is also rejected; next-session publication creates the new
version. No historical trade rewrites or live-mode/config changes are needed.
Deployment readiness must be verified for both job images after release;
changing `live_trading` alone does not update the deployed code.

Offline checks:

```powershell
python ..\nifty-spreads-backtest\tests\test_profit_exit.py
python ..\nifty-spreads-backtest\tests\test_current_option_strategy.py
python ..\nifty-spreads-backtest\tests\test_option_exits.py
```

The sections below describing October31 are historical change records.

## Signal flow simplified October31

The signal job now reads directly in `main()`: wait for09:30, load the saved
daily lock (or select ATM/CE/PE/expiry from09:29 close), calculate both Renko
charts, then update Mongo together. Removed the `settings`, `now_ist` and
`publish` wrappers; startup configuration, IST time and publication are inline.
Five functions remain: `main`, `notify`, `locked_contracts`, `session_candles`
and `option_levels`. The last three keep contract selection, candle safety and
Renko calculations separate from the job loop; their ASTs are unchanged.

Same-day restart locks, the executor-owned skip marker, atomic publication,
freshness/low-premium guards, monitoring and shutdown behavior are preserved.
The version, flat Mongo schema, >6-DTE expiry rule and executor are unchanged.
This is a readability change, not a new trading configuration or deployment.

Offline checks in the existing sibling research repository:

```powershell
python ..\nifty-spreads-backtest\tests\test_current_option_strategy.py
python -m compileall -q executor signal
```

Eighteen focused replay/signal checks pass, including five new mocked job-flow
tests for the09:30 gate, once-per-minute publication, exact flat fields,
executor compatibility, restart locks, failed-fetch retries, rollover, unsafe
data and concurrent writes. Twenty-nine checks pass with the related history
and downloader tests. No live broker/Mongo calls or orders were made.

## Preserved jobs and storage

| Path/job | Responsibility |
|---|---|
| `signal/signal_job.py` / signal job | From09:30, lock ATM from09:29 close/expiry, form CE/PE charts, publish Renko close/high20/low20/low5 |
| `executor/executor.py` / execution job | Read Mongo levels, decide breakouts/entries/exits, manage one trade and net P&L |

All strategy code lives in these **two existing scripts**, with no added shared
module or test/configuration files. The signal job makes no trading decisions.

Mongo database remains `Bots`, with `nifty_interceptor_<user_name>` and
`orders_nifty_swing_<user_name>`. Signal data uses **one reusable document** in
`supertrend`: **`_id = nifty_options_renko`**, shared by both jobs, with no user
name or date suffix. `user_name` still selects executor trade/order collections;
configure it correctly on Option Buy.

| Flat field | Meaning |
|---|---|
| `updated_at`, `strategy_version`, `broker` | Publication timestamp (also identifies session date) and identity |
| `ATM`, `STRIKE`, `expiry`, `dte` |09:29 Nifty close, rounded ATM strike and selected expiry (existing same-day locks retained) |
| `CE_symbol`, `PE_symbol` | Exact dated option symbols |
| `CE_security_id`, `PE_security_id` | Dhan security IDs |
| `CE_lot_size`, `PE_lot_size` | Contract lot sizes |
| `CE_close`, `PE_close` | Latest Renko closes |
| `CE_last_20_high`, `PE_last_20_high` | High of20 prior bricks, excluding current brick |
| `CE_last_20_low`, `PE_last_20_low` | Low of20 prior bricks, excluding current brick |
| `CE_last_5_low`, `PE_last_5_low` | Low of5 prior bricks, excluding current brick |
| `CE_brick_time`, `PE_brick_time` | Latest Renko brick timestamps |
| `CE_data_asof`, `PE_data_asof` | Latest completed source-candle timestamps |
| `skip_day` | Executor-owned marker if both latest options qualify; cleared next day |

Both options refresh together each completed minute. No chart-history arrays or
stored first decision. Levels persist without new bricks; channels remain
`null` until enough bricks exist. Executor compares the flat close/high/low
fields. Entry needs a post09:30 brick and fresh completed source data, but the
brick itself may be older than the latest source minute.

On a new day, publisher starts at09:30, locks ATM from09:29 close/expiry and replaces all session fields
on the same document only after both charts succeed. Until then yesterday's
`updated_at` date/freshness blocks execution. No separate `day` field is stored.
Same-day restarts retain the contract lock. Old daily-ID
documents are left untouched; no automatic deletion or production migration.

The idle executor starts broker/entry checks at09:31; existing positions still
recover before then. Bricks before09:30 never qualify for entry, so09:30
publication primes the charts, and the first eligible entry remains09:31.
During the09:31 minute only, no document or a previous-day document is a
console-logged waiting state, not Slack error/recovery spam. At09:32 those
conditions raise the existing error. Malformed, incompatible, future-dated or
stale-today publications still raise, and active positions never use this grace.
Jobs must be scheduled to run by their respective gates; cloud schedules and
workflow are not changed here. Prior eight-session research used09:30 open,
not the new09:29 close; its results do not validate this changed reference.

The daily trade ID is deterministic and atomically inserted. Paper BUY/SELL order
IDs are deterministic too, so a saved fill is reused if a process crashes before
updating the trade. A failed/expired pending entry cannot cause another buy.
Historical trades/orders are not deleted or rewritten. An open legacy/partial
position or different-mode position **blocks the new strategy** with an alert; reconcile
it explicitly before cutover.

## Execution and accounting

**One executor switch:** `live_trading=false` (default) records simulated fills;
`live_trading=true` submits Dhan orders. Invalid values are rejected. Keep the
container job set to `false` for the current forward test. The signal job does
not use the flag. Mode is recorded on each trade; changing the job's mode while
a trade is open stops execution with a reconciliation alert, not a mode conversion.
Dhan authentication uses the shared token-aware `login_to_dhan`, never a forced
fresh token on every poll.

Only completed one-minute candles form signals. After observing the signal, the
executor records the **current Dhan option LTP** as the paper fill during the next
minute. It does **not** backdate a fill to the historical next-minute open.
The quote receipt time is recorded; a slow BUY response that crosses its entry
minute or15:15 is rejected rather than recorded with an earlier timestamp.
There is no extra synthetic slippage haircut in simulated fills: LTP still
does not model bid/ask or executable liquidity, and Dhan's LTP helper does not
provide a quote timestamp. Research results are not promised to reproduce.

Live execution uses shared `place_order`/`wait_for_fill`, with actual average
fill prices only after `TRADED` and full `filledQty` validation. Before sending,
an atomic order intent reserves the deterministic BUY/SELL ID. A saved broker
ID is polled after restart, never resubmitted. An intent without a broker ID
(including an ambiguous timeout/crash) requires manual broker reconciliation.
Rejected, cancelled or partial fills alert and block rather than creating a
duplicate order or claiming a full position/closure; a partial position may still
be exposed and requires prompt manual attention. There is no automatic residual
order or partial-fill accounting/final P&L. Exit intent/reason persists so restart
continues that exit even if its channel signal disappears. Actual fills may occur
after the entry window; the live check limits **submission**, not exchange fill
time. Recorded live entry/exit timestamps are local confirmed-fill observation
times, not backdated broker placement times.

`pnl` remains gross. `tamingnifty.charges` supplies itemized `charges`, and
`net_pnl = pnl - charges.total`. An active trade has zero realized gross and its
entry fees; `running_pnl` is the current gross mark, not realized P&L. Closed
records include both executed orders' fees; no flat fee, charge schedule or
estimate metadata fields are added.

## Monitoring

- Each job combines startup/public IP into one short alert. Hourly state,
  changed errors immediately, repeated errors every15 minutes and recovery remain.
  Executor mode appears only at startup.
- Signal alerts show ATM/expiry and update time, not every channel value. Full
  close/high20/low20/source details remain in console logs and Mongo.
- Executor alerts show symbol/quantity, entry time/price, estimated net and net
  max/min. Final summary adds exit time/price, realized net, gross, charges and
  reason. Gross extrema and full timestamps remain stored, not repeated in Slack.
- `running_net_pnl` estimates liquidation net using filled entry plus a hypothetical
  exit's charges. It does not replace realized `net_pnl`. Persistent
  `max_pnl_reached`/`min_pnl_reached` track gross marks; `max_net_pnl_reached`/
  `min_net_pnl_reached` track estimated net marks. They include entry and final
  fills, survive restarts, and reflect **observed** polling marks, not true
  tick-by-tick extremes. Errors/stale signals can leave gaps; heartbeat errors
  show the current error rather than repeating an old position snapshot.

Both jobs exit once today's trade is confirmed `closed` and no pending, active,
partial or closing record remains. Executor exits immediately after its final
summary; signal checks Mongo every10 seconds and sends one short stopped alert.
Same-day restarts detect completion before broker login/chart fetch. Signal's
`user_name` must match executor's trade-collection user for this shutdown check.
No new completion document/field is needed. Yesterday's closed record does not
stop today's jobs. No-trade/aborted/skip days keep existing scheduled shutdown;
unresolved orders never count as a completed trade. Normal closing-bell alerts
are sent only when the jobs reach scheduled shutdown, not again after a trade.

Stale/missing signals block entry and ordinary channel exits and alert explicitly.
**15:15 closure does not depend on the signal job**; if a quote/API fails the
executor keeps monitoring/reconciling rather than marking the position closed.
Live submission uncertainty still requires manual reconciliation, not resubmission.
It stays running
after15:16 while any pending/open position exists. An overdue new-era position is
closed at the next available quote; this is recovery, not an overnight rule.
Neither the exact15:15 fill time nor a maximum monetary loss is guaranteed.

## Configuration and pipeline

The planned Azure jobs are `nifty-interceptor` and `nifty-interceptor-signal`;
image names match. Push-to-master/workflow-dispatch triggers and restart steps
remain. Provision the new jobs and suspend the old jobs before deployment.
Build from the repository root:

```powershell
docker build -t nifty-interceptor:test .\executor
docker build -t nifty-interceptor-signal:test .\signal
```

Dockerfiles copy **only** their existing script and `requirements.txt`, never
`.env` or other source-directory files. Never bake credentials into either image.

The GitHub workflow targets the new jobs and image names. Keep its existing
Azure/ACR credentials and resource-group secret; **do not add Dhan credentials to GitHub**.
Configure `DHAN_CLIENT_ID`, `DHAN_PIN`, `DHAN_TOTP` directly on **both container
jobs**, for the same account used by the shared token store. Prefer container-job
secret references for sensitive values. Set `live_trading=false` on Option Buy
only for the current test; use `true` there only when intentionally enabling
real orders. The signal job does not use this flag.
The pipeline updates images only, not job environment variables or secrets.

Both jobs still need `CONNECTION_STRING`, `user_name`, `slack_token` in their
existing environments. The executor also requires an explicit positive integer
`quantity`, validated against the selected contract's lot size; **one lot is now
allowed**, with no even-lot/half-book restriction. `instrument_name`, if supplied,
must be `NIFTY`. No local `.env`, cloud setting or repository secret is modified
by the source rewrite.

Before deploying, ensure the jobs start before09:31, the executor remains
available through15:15 (including its configured timeout), and any legacy open
positions are reconciled. Both container jobs must have the Dhan environment
variables before starting; Definedge-only credentials cannot run this port.

## Validation and release status

```powershell
python -m pip install -r executor\requirements.txt
python -m compileall -q executor signal
```

Both dependency manifests pin `tamingnifty==2.4.0`. Before the fixed-document change,
after simplifying to two
scripts,28 offline checks pass, including exact published levels, strict
breakout comparisons in the executor, updates without new bricks, insufficient
history, candle causality, ATM/expiry, ties, daily cap, restart recovery, EOD,
fees and image/workflow wiring. The check harness was run outside the strategy
repository to respect the no-new-files request; it is not shipped or a pipeline
test suite. The original CI workflow has no added validation steps; run the
compilation command above locally. The subsequent fixed-document/latest-only
change passed offline checks for exact flat Renko levels, atomic new-day refresh,
failed-fetch preservation, latest-level restart behavior, ties, daily cap,
paper entry/exit and fees. The earlier28-check result predates this schema change.

Before simplification, the1%/prior40 first-option entry times matched the research
harness on all five saved pilot sessions. This is not P&L proof: paper fills use
current LTP and the new requested EOD is15:15 rather than the pilot's15:20.

### October29 update (local, not pushed)

Owner selected1%20/20 after the seven-session grid; this is a forward-test choice,
not proof of edge. Eight offline regression groups cover prior20 causality,
freshness/ties, both execution modes, confirmed live fills (mocked only),
ambiguous submissions, partial/rejected fills, order/restart recovery, daily cap,
EOD, extrema/net fees, IP/status/error/recovery/closing-bell notifications and
same-day signal-lock preservation. No real orders were sent during testing.

Updated the shared Mongo snapshot with recomputed20-highs (CE376.62/PE200.20)
from its exact cached contracts at the original October28 cutoff; removed40-high
fields. Preserved contracts, ATM/expiry and `updated_at`2026-10-28 15:15:00.879127.
This intentionally remains stale until the next successful daily publication.
Trades/orders and container variables were not changed.

**Deployment readiness must be verified separately.**
Pushing master triggers the unchanged deployment workflow. Configure Dhan
variables on both container jobs before starting the new images. Update both
jobs together: an old publisher can overwrite the shared document with its old
version/fields, which the new executor will reject.
Docker daemon was unavailable, so actual image builds/Azure deployment are not
validated locally. No strategy loop was started, no broker order was sent, no
token rotated during this update. The snapshot migration above is the only
production Mongo change.
The new era needs its own forward sample; historical swing results and the
coverage-limited premium study do not validate its profitability.