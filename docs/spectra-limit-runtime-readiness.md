# LIMIT runtime readiness

This is a verification-only follow-up to draft PR #31 at
`24b152457d9a8b3e007e69e7879bab38d1beb254`. It does not install, rebuild,
restart, merge, cancel or place an order. The production target remains 174 bps.
The production stack lock remains PARTIAL with live approval disabled.

## Evidence boundaries

The previous 788-test run used a GitHub-hosted Ubuntu runner. It verifies the
reviewed Spectra source, not the bedside Bazzite deployment. Previously reported
local endpoints (`localhost:8000` and `localhost:8088`) do not provide access
from this workspace. No authenticated host route is available. The connector
does not expose runner inventory or workflow dispatch; that limitation does not
prove that a self-hosted runner does not exist.

The source references are immutable:
- Spectra execution candidate: `24b152457d9a8b3e007e69e7879bab38d1beb254`.
- API: `84426ffbf9ba09522b4444664f9770236205ae29`.
- Examined Hummingbot core: `9af100d6822da7d2d0291a906c730ef172284ee2`.

The core reference is **not an approved deployment target**. A source hash
match proves only that the selected files match; it does not attest the entire
package, compiled components, runtime configuration, build provenance or exchange
behavior. `spectra-runtime.contract.json` records these distinctions.

The API source still installs an unpinned `hummingbot` dependency and its compose
configuration uses `hummingbot/hummingbot-api:latest`. Its
`REQUIRED_CORE_SURFACE` is empty. Successful startup therefore does not establish
that the required config, accounting and connector surfaces are present.

## Reproduced source risks

The committed probes use byte-verified upstream source and inert dependencies.
Selected method bodies run offline; no exchange, credentials, real connector
instance or installed compiled backend is used. Passing a reproduction test
means the described source behavior was observed. It does not mean that a
vulnerability was fixed or that the deployed system passed.

| Condition | Source consequence | Required deployment protection |
| --- | --- | --- |
| A single order is canceled while its executor remains RUNNING | The next control tick can place the remaining LIMIT again | Expiry must stop the executor, then reconcile terminal orders and trades |
| A fill arrives after cancellation and replacement sizing | The replacement can exceed the originally authorized aggregate quantity | Bind every attempt to the original parcel; retain all fills and block uncertain replacement |
| A cancellation/stop snapshot precedes a late fill | Terminal held exposure may omit that fill | Durable order/trade evidence must finish reconciliation before releasing cash or ownership |
| An order remains indeterminate at shutdown | Bounded shutdown can stop without proving venue absence | Keep the claim and authorization unresolved; do not equate FAILED with no exposure |
| AddOrder succeeds but its response is ambiguous | OpenOrders alone can miss already filled orders; retry may duplicate placement | Query exact accepted/closed orders and trades; freeze automatic retry while uncertain |

These are backend deployment blockers. Spectra's truthful-fill accounting and
cash holds remain necessary, but detecting a breach after a fill does not prevent
an excessive replacement order from being submitted upstream. No core fix is
claimed in this verification-only change.

## Fee proof

The pinned Kraken connector calculates LIMIT as a taker fee estimate and
LIMIT_MAKER as maker. Its `_update_trading_fees` method is a no-op; a configured
estimate is not proof of the current account's pair-specific fee tier.
The installed account's taker rates must be compared with the explicit protected
fee allowance before activation. A marketable LIMIT must not be budgeted using
a maker-only assumption.

Kraken's [TradeVolume](https://docs.kraken.com/api-reference/account-data/get-trade-volume)
returns pair-specific account fee information: `fees` holds taker rates and
`fees_maker` holds maker rates for maker/taker pairs. That read requires account
query permission. The collector does not request or export credentials, balances,
trading volume or account responses. No authenticated fee query was performed.

Kraken documents native time-in-force in
[AddOrder](https://docs.kraken.com/api-reference/trading/add-order).
The examined connector does not forward it, so an IOC guarantee cannot be
inferred. Its use of `userref` is not an idempotency guarantee: Kraken documents
that identifier as non-unique.

The same connector ignores extra keyword arguments, including `validate`.
Passing `validate=true` to this executor/connector path is therefore **not a
no-order safeguard**. Source probes use inert transport and deny external I/O;
the installed collector never calls that path.

## Reproduce the source observations

With the repository's frozen development environment:

```bash
uv sync --dev --frozen
uv run pytest -q tests/test_runtime_attestation.py tests/test_pinned_limit_lifecycle.py
uv run python scripts/probe_limit_lifecycle.py --output limit-source-observations.json
```

The probe JSON explicitly identifies deployment blockers and keeps
`installed_backend_verified` and `live_approval_eligible` false. Test success
does not convert a reproduced vulnerability into a safety pass.

## Obtain installed evidence

Use the read-only command in
[spectra-runtime-attestation.md](spectra-runtime-attestation.md) on Bazzite.
It records selected OS, source, container image and installed module facts.
It deliberately keeps live approval false, reports missing evidence, and never
runs a trading module. Local API checkout HEAD and an OCI revision label do not
establish that the running image was built from that checkout.

After that evidence is available, verify an immutable build and the actual
installed config/connector surface in an isolated no-credential environment.
Exercise LIMIT transport, complete and partial fills, executor stop, late-fill
races, ambiguous creation, outages, API restart and journal reconciliation.
Compare the actual pair fee profile to the declared allowance. A paper connector
can test lifecycle coordination but cannot attest Kraken's private REST adapter.
Installed and venue claims require their own evidence.

Neither this document, a source probe nor a collector output grants live trading
authority. The missing runtime and fee checks remain explicit until completed.
