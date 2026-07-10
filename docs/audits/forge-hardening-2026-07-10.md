# Forge correctness and hardening audit — 2026-07-10

## Scope and method

Audited the strategy lifecycle from code intake through quick screen, gauntlet,
paper, and live admission, including the backend status model and Forge UI. The
review used an isolated worktree based on `origin/main` at `741db95e`. Findings
below are new; the six issues identified as already fixed in the mission were
verified where adjacent code was touched and are not re-reported.

Severity is based on gate impact: whether a defect can execute untrusted code,
admit an unverified strategy, corrupt operator-owned state, archive a valid
strategy, or materially misstate lifecycle truth.

## Findings

### CRITICAL

#### F-01 — Generated custom-strategy intake imported untrusted code in the API process

**Status:** CONFIRMED — fixed.

**Mechanism:** Both targeted AI Drop Zone registration and bulk custom scanning
eventually imported `forven.strategies.custom.*` in the trusted parent process.
The AST scan reduced known syntax risks but did not make Python module execution
a safe trust boundary. A generated file's top-level code ran with the API
process's filesystem, environment, and in-memory privileges before the strategy
became sandbox-only.

**Concrete failure scenario:** A generated strategy with a static-guard bypass or
an allowed import containing a hostile side effect executes that side effect at
registration time, before any sandboxed backtest occurs.

**Fix:** Bulk discovery excludes custom imports and consumes only isolated-worker
metadata (`forven/strategies/intake.py:210`, `forven/strategies/intake.py:261`).
Targeted and bulk registration now converge on a sandbox-only path
(`forven/strategies/intake.py:546`), move accepted source into the imported
quarantine (`forven/strategies/intake.py:607`), and persist a separate sandbox
runtime type (`forven/strategies/intake.py:1105`). Sandbox-only container creation
also avoids trusted registry introspection (`forven/db.py:4867`). Export resolves
the persisted source without rediscovering/importing the class
(`forven/strategy_lifecycle.py:1683`).

**Regression evidence:** `tests/test_ai_dropzone_intake.py:105`,
`tests/test_ai_dropzone_intake.py:144`, `tests/test_strategy_portability.py:207`.

### HIGH

#### F-02 — Empty `gauntlet.required_tests` disabled the entire required battery

**Status:** CONFIRMED — fixed.

**Mechanism:** Settings documented an empty list as “enforce all,” but the policy
converted it to an empty set and therefore required no persisted robustness
verdicts. A gauntlet strategy with otherwise acceptable aggregate metrics could
enter paper without the five-test battery.

**Concrete failure scenario:** An operator leaves `required_tests=[]` at its
documented default; a strategy with no walk-forward, Monte Carlo, parameter
jitter, cost-stress, or regime-split artifacts is treated as having no missing
required tests.

**Fix:** Empty configuration expands to the canonical validation set in policy
(`forven/policy.py:3738`) and in the status API (`forven/gauntlet/status.py:200`).

**Regression evidence:** `tests/test_gauntlet_paper_bypass_fix.py:175`,
`tests/test_gauntlet_workflow_status.py:92`.

#### F-03 — Concurrent lifecycle transitions could both pass a stale stage check

**Status:** CONFIRMED — fixed.

**Mechanism:** `transition_stage` read the current stage, evaluated gates, then
wrote later without serializing the final read/check/write. Two workers could
evaluate the same old stage and both emit events; conflicting targets were
last-writer-wins.

**Concrete failure scenario:** Two workflow workers concurrently promote the same
strategy. Both observe `gauntlet`, both pass, and each records a transition or one
silently overwrites the other's target.

**Fix:** After gate evaluation, the commit phase takes `BEGIN IMMEDIATE`, re-reads
stage/version, treats an identical completed target as idempotent, and blocks a
conflicting stale transition (`forven/brain.py:1861`, `forven/brain.py:1881`).

**Regression evidence:** `tests/test_forge_lifecycle_hardening.py:119` uses two
real threads and asserts one committed event.

#### F-04 — Admission dependencies failed open on runtime errors

**Status:** CONFIRMED — fixed.

**Mechanism:** Exceptions from runtime loadability, exact-duplicate detection, or
WIP-capacity checks could be converted into an implicit pass. The enabled
Deflated-Sharpe gate also skipped rejection if its computation raised.

**Concrete failure scenario:** A transient registry or database error occurs
during paper/live admission. The candidate is admitted even though the system
could not prove it can execute, does not duplicate active exposure, fits capacity,
or meets an explicitly enabled DSR threshold.

**Fix:** Each dependency now returns a blocking reason when unavailable
(`forven/brain.py:1647`, `forven/brain.py:1685`, `forven/brain.py:1718`,
`forven/policy.py:3991`).

**Regression evidence:** `tests/test_forge_lifecycle_hardening.py:45`,
`tests/test_forge_lifecycle_hardening.py:68`,
`tests/test_forge_lifecycle_hardening.py:92`,
`tests/test_gauntlet_paper_bypass_fix.py:359`.

#### F-05 — Evidence-currentness and input-data errors could fall back to a pass

**Status:** CONFIRMED — fixed.

**Mechanism:** A data-provenance exception left the result type available for an
older or cached passing payload; data-quality and availability probe errors could
allow scoring/backtesting or recovery to continue on unknown input coverage.

**Concrete failure scenario:** The newest walk-forward artifact cannot be checked
against current data semantics, so a stale cached PASS fills its place. Separately,
an enrichment-catalog error lets a strategy backtest against zero-filled missing
feeds and later be judged on a meaningless result.

**Fix:** Provenance errors claim the artifact type and force re-validation
(`forven/policy.py:2048`). Data-quality errors stay retryable and do not become
merit failures (`forven/gauntlet/tasks.py:193`). The shared feed precheck and its
backtest/creation callers now block or park on unknown availability
(`forven/strategies/data_availability.py:502`,
`forven/strategies/backtest.py:8512`, `forven/routers/backtesting.py:208`).
Research recovery likewise stays parked until the check succeeds
(`forven/brain.py:4105`).

**Regression evidence:** `tests/test_forge_verdict_fail_closed.py:10`,
`tests/test_edge_data_runs.py:171`, `tests/test_data_availability.py:99`,
`tests/test_create_strategy_fail_closed.py:202`.

#### F-06 — Strict-live evidence and source reconciliation were optional on error

**Status:** CONFIRMED — fixed.

**Mechanism:** Strict robustness extraction returned “no rejection” when its
artifact read raised or yielded no payloads. Cross-venue source reconciliation
defaulted to allow missing/stale evidence, and an invalid timestamp could still
use the payload.

**Concrete failure scenario:** A paper strategy with sufficient forward trades is
evaluated while its gauntlet artifact store is unavailable, or before Binance ↔
HyperLiquid divergence has been measured. The live-capital gate silently omits
the unknown safety dimension.

**Fix:** Strict-live extraction now rejects unavailable/empty evidence
(`forven/policy.py:3625`) and is invoked even when the strategy metrics blob is
empty. Missing/stale source reconciliation blocks by default while retaining the
operator's explicit `block_when_missing=false` opt-out
(`forven/policy.py:2208`, `forven/policy.py:2221`,
`forven/dataeng/settings.py:84`). Invalid timestamps are treated as missing.

**Regression evidence:** `tests/test_two_tier_gate.py:94`,
`tests/test_two_tier_gate.py:116`,
`tests/test_source_reconciliation_gate.py:207`,
`tests/test_source_reconciliation_gate.py:248`.

#### F-07 — Operator-owned metric/parameter freeze had check-then-write races

**Status:** CONFIRMED — fixed.

**Mechanism:** Several automated writers checked the stage before writing but did
not include the freeze predicate in the write itself. A paper promotion between
those operations allowed stale backtest, robustness, optimization, or profile
selection work to overwrite frozen parameters/metrics.

**Concrete failure scenario:** A gauntlet optimizer reads an unlocked strategy;
another worker promotes it to paper; the optimizer then writes new defaults into
the now operator-owned row.

**Fix:** Automated updates use write-time stage predicates and row-count checks in
backtest sync (`forven/strategies/backtest.py:1763`), robustness sync
(`forven/routers/robustness.py:2034`), optimization/default application
(`forven/gauntlet/tasks.py:911`), and execution-profile selection
(`forven/gauntlet/tasks.py:1458`). Parameter updates re-check ownership at write
time; optimization-lock failures block application
(`forven/strategies/optimization_acceptance.py:425`).

**Regression evidence:** `tests/test_paper_param_lock.py:113`,
`tests/test_paper_param_lock.py:173`, `tests/test_optimization_acceptance.py:299`.

### MEDIUM

#### F-08 — Strategy exceptions could evade the lookahead probe

**Status:** CONFIRMED — fixed.

**Mechanism:** Any exception during truncation-invariance probing was treated as
inconclusive. A strategy could intentionally raise only on the synthetic probe
frame and avoid a lookahead rejection.

**Concrete failure scenario:** A future-reading `generate_signals` detects probe
shape/columns, raises, and then executes normally on real data; registration sees
no lookahead reason.

**Fix:** Exceptions originating in strategy code are now rejections, while genuine
probe-infrastructure faults remain inconclusive
(`forven/strategies/lookahead_probe.py:149`,
`forven/strategies/lookahead_probe.py:198`).

**Regression evidence:** `tests/test_lookahead_leak_gates.py:188`.

#### F-09 — Forge status could show an older PASS over newer failure and derive readiness client-side

**Status:** CONFIRMED — fixed.

**Mechanism:** A passed workflow step could override a newer persisted failed
artifact without proving both referred to the same result. The component also
assembled its own “ready” state from partial fields, allowing UI truth to diverge
from the authoritative policy gate.

**Concrete failure scenario:** Re-validation writes a newer failed Monte Carlo
result, but the detail page continues showing the older workflow PASS and a green
paper-readiness banner.

**Fix:** Workflow status only overrides the matching result ID; deterministic
newest-artifact ordering is preserved. The status endpoint calls
`evaluate_promotion(..., record_rejection=False)` and returns authoritative
`ready_for_paper` plus `promotion_reason` (`forven/gauntlet/status.py:340`). The
component renders those fields directly
(`frontend/src/lib/components/robustness/GauntletStatusCard.svelte:399`,
`frontend/src/lib/components/robustness/GauntletStatusCard.svelte:413`).

**Regression evidence:** `tests/test_gauntlet_workflow_status.py:113`,
`tests/test_gauntlet_workflow_status.py:162`,
`frontend/src/tests/strategyDetailRoute.test.ts`.

### PLAUSIBLE / deferred

#### F-10 — Legacy operator-authored custom classes still have an in-process compatibility path

**Status:** PLAUSIBLE — deferred.

Generated/drop-zone/imported code is now sandbox-only, but pre-existing custom
classes that an operator deliberately installed as runtime modules can still be
discovered and executed in process. If another ingress can write such a legacy
module while making it appear operator-installed, the same Python trust risk
would return. No bypass of the new generated/imported paths was reproduced.

Removing the compatibility path would strand existing local strategies and
requires a migration that assigns sandbox runtime types and validates export,
optimization, recovery, and paper execution. That migration is intentionally
deferred rather than silently changing established operator-authored runtimes in
this PR.

## Verification

- Ruff: `python -m ruff check forven tests` — passed.
- Backend collection: 5,438 tests collected; collection passed.
- Curated CI release group — 118 passed.
- Curated CI data-engine group — 252 passed (two existing pandas deprecation
  warnings).
- Curated CI security group — 185 passed.
- Final Forge-focused regression group — 133 passed.
- Data/source/strict-live focused groups — 37 passed and 31 passed.
- Frontend unit tests — 65 files / 411 tests passed.
- `npm run check` — zero errors; two existing accessibility warnings.
- `npm run test:e2e` — passed against the isolated test backend.
- The three failing source-reconciliation producer tests were run in a second,
  untouched `origin/main` worktree and failed identically there on this Windows
  dependency set (`insufficient_overlap` instead of `ok`/`fetch_error`). They are
  baseline environment failures, not regressions in the gate changes.

## Left alone deliberately

- No simulation-kernel, sizing-number, drawdown/Sharpe, WFO, or simultaneous-
  position semantics changed. Those remain in the engine-version-bump batch.
- The lean quick-screen/paper-entry versus strict-live philosophy is preserved.
  Required tests were restored; thresholds were not raised or bypassed.
- DSR remains observational by default. Only an explicitly enabled DSR gate now
  fails closed when computation is unavailable.
- Paper opening remains independent of kill switches/halts.
- No cross-provider fallback was added. Unknown data/source evidence is parked;
  operator configuration remains authoritative.
- Gate thresholds and the explicit source-reconciliation missing-evidence opt-out
  remain editable in Settings.
- Paper/live stored metrics and parameters remain frozen against automated work;
  explicit operator writes are still allowed.
- Beta paper-mode clamps were not changed.
- Revival's crash-probe infrastructure faults remain inconclusive instead of
  merit failures. This avoids archiving a good strategy because the probe itself
  failed; downstream gates still cannot promote absent evidence.
- The one-shot migration/restore utilities retain their direct database writers.
  They are explicit operator recovery tools with snapshots/events, not routine
  lifecycle paths; routing them through promotion gates would break restoration.
- Execution-profile selection remains best-effort because it chooses among valid
  risk profiles and is not an eligibility gate. Failure leaves the valid default
  profile and does not weaken robustness evidence.
- Existing dependency-audit findings and the reproduced baseline Windows source-
  reconciliation producer failures were not changed; neither is a Forge gate
  regression introduced by this work.
