# Offline parked-cash experiment

This folder preserves the isolated research permission for parked quote cash to
fund selected native buy purposes. It has no production auto-import, exchange
client, credential handling or live order endpoint. Nothing in `spectra_core` or
the production entry point imports this folder.

`release_adapter.py`, `run_comparison.py`, `run_enabled_matrix.py`, and
`protocol.json` are unchanged copies of the frozen local experiment. The saved
output auditor is also copied unchanged. The native integration test changes
only external input paths and its separate receipt destination.

The runner includes the separately documented
[observation-only revision](../../docs/research-results/parked-cash-runtime-observation-2026-10-09.md).
Its funding adapter and protocol retain their original pins.

## Policy

| Arm | Parked-cash permission | Outstanding cap |
| --- | --- | --- |
| OFF | None; original hydrated kernel | $0 |
| ENTRY_ONLY | Original native entries | $100 |
| RECOVERY_ONLY | Original native recovery buys | $100 |
| BOTH | Native recovery then native entries | $100 shared |
| BOTH_TIGHT | Native recovery then native entries | $50 shared |

These are **outstanding caps**: surviving parked-funded acquisition basis plus
pending parked-cash claims. They are not lifetime-turnover limits. Disposal
retires acquisition basis once; returned proceeds need a fresh authorization.
Ordinary cash is used first. Claims never increase physical wallet cash. The
native 25% cash reserve, 5% per-asset basis cap, 50-asset maximum, fee model,
execution floors, recovery/tranche planners and mutation ordering remain fixed.

The original selector ranks causal movement and recorded volume. It is **not a
predictive winner selector**. This comparison does not add new signal features,
asset-specific ladders, wallet freshness rules, DTSS, DTSSR or PSSM.

All net recovery-partial proceeds return to PARK. A whole sale returns the exact
parked-funded ownership share to PARK; its ordinary-funded remainder remains
productive. Source ownership follows actual native quantities and fee-inclusive
lot basis, rather than treating the whole parent as one indistinguishable pool.

## External dependencies

The native research archive, wrapper, bridge, native planner source, hydration
adapter, canonical NumPy tape, metadata and exact June checkpoint are **not
included in Git**. Restore them from the pinned evidence archives documented in
[the evidence-reference manifest](../../docs/research-protocols/activation-evidence-2026-10-09.json).

The wrapper expects its frozen archive-relative bridge/source layout. Supplying
a wrapper file alone is insufficient. The external native runner and integration
tests require NumPy. The hydration adapter is archived research infrastructure,
not a production restart API. Keep the checkpoint beside its original
`hard_100_park_fills.csv` and `hard_100_park_equity.csv`; OFF uses those files for
the exact archived comparison.

The authoritative evidence-reference path from the repository root is:

`docs/research-protocols/activation-evidence-2026-10-09.json`

The frozen protocol contains native strategy, checkpoint and canonical tape
hashes. Do not edit it while a matrix is running. The runner records its executed
source and checks pins; a changed policy or input belongs to a separate trial.

## Explicit offline commands

Run from the repository root. The following paths are placeholders for restored
external inputs; they are not supplied by this checkout.

```bash
python research/parked_cash/run_comparison.py \
  --arm OFF \
  --wrapper /archive/continuation-v3/continuation_wrapper.py \
  --hydration /archive/hydration.py \
  --checkpoint /archive/hard_100_park/boundary_state.json.gz \
  --prepared /archive/prepared \
  --protocol research/parked_cash/protocol.json \
  --output /results/parked-cash/OFF \
  --seconds 420
```

Every output directory must be new. The archived OFF control must pass before
the sequential enabled matrix starts:

```bash
python research/parked_cash/run_enabled_matrix.py \
  --wrapper /archive/continuation-v3/continuation_wrapper.py \
  --hydration /archive/hydration.py \
  --checkpoint /archive/hard_100_park/boundary_state.json.gz \
  --prepared /archive/prepared \
  --results /results/parked-cash
```

The matrix uses its sibling frozen protocol and stops on failed execution,
invalid accounting or missing actual parked-funded activation. The per-arm
simulation budget is 420 seconds; input verification and checkpoint restoration
are measured separately. The selected July window was already inspected and is
not an untouched performance holdout.

Audit completed saved outputs without executing or rehydrating the strategy:

```bash
python research/parked_cash/audit_release_runs.py \
  --protocol research/parked_cash/protocol.json \
  --run /results/parked-cash/OFF \
  --run /results/parked-cash/ENTRY_ONLY \
  --output /results/parked-cash/independent-audit.json
```

Repeat `--run` for each completed arm. The auditor uses only the Python standard
library and reconstructs exact source ownership, cash, claims and final account
rows from saved evidence. It does not perform a second historical replay.

## Tests and CI

The mandatory research-integrity CI runs eight standard-library archive tests:

```bash
python -m unittest discover -s research/tests \
  -p 'test_verify_diagnostic_evidence.py' -v
```

The separate optional integration suite contains 16 constructed-money mechanics
tests against authentic frozen native lot and execution methods. To execute it:

```bash
SPECTRA_NATIVE_WRAPPER=/archive/continuation-v3/continuation_wrapper.py \
  python research/parked_cash/tests/test_release_adapter_native.py
```

Without `SPECTRA_NATIVE_WRAPPER`, unittest discovery reports an explicit SKIP
stating that the frozen archive is required. It does not report 16 passing
integration tests. A supplied but invalid wrapper or missing dependency fails
instead of silently skipping. The direct script writes its separately named
`Ported_Allocator_Mechanics_Receipt.json`; the original independent test receipt
and original test-source hash remain preserved in the external evidence.

Passing these mechanics tests does not establish historical profitability,
production execution equivalence, live readiness or a deployment decision.
