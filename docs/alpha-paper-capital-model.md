# Spectra Alpha Paper Capital Model

This document describes the paper-mode capital contract. It is a budget ceiling, not a promise that every dollar will be deployed.

## Initial allocation

The default simulated account starts with **$25,000 USD**.

| Allocation | Ceiling | Purpose |
|---|---:|---|
| Initial entries | $6,250 (25%) | Open the initial diversified portfolio |
| VCAP recovery | $18,750 (75%) | Selective, independently tracked recovery shards |
| Total committed capital | $25,000 | Hard combined ceiling |

The capital policy also keeps a $100 quote-currency liquidity floor. Consequently, the engine may leave some of the nominal $25,000 unused rather than consume the final reserve.

The default fleet target is 83 positions. The initial-entry amount is calculated as:

`initial_entry_quote = floor_to_cent(6250 / target_active_positions)`

That is **$75.30 per position at 83 positions** or **$125.00 at 50 positions**. The existing `SPECTRA_TARGET_ACTIVE_POSITIONS` setting selects the target; the entry budget itself remains a single policy value.

## Enforcement

`CapitalGovernor` enforces three independent ceilings:

- `max_entry_quote = 6250` for all non-recovery intents (`ENTRY`, `PRESS`, and `ROTATION`)
- `max_recovery_quote = 18750` for `RECOVERY`
- `max_total_committed_quote = 25000` for combined committed exposure

The entry ceiling is based on outstanding reservations plus reconciled base-entry exposure. Reconciled entry exposure is derived from the canonical economic-position ledger. If attribution is missing or inconsistent, the Governor conservatively charges the original authorized amount. A zero-basis position does not release entry budget until the existing verified-terminal-flat process retires its entry claim.

The recovery pool is deliberately **not** modeled as parked cash. The separate cash-authority PARK bucket is unavailable to recovery reservations by design; using it here would make the 75% recovery allocation unusable. The 75% pool is instead governed by the dedicated recovery ceiling and the combined total ceiling.

## Position sizing and VCAP

Initial entries are evenly sized against the configured target count. VCAP recovery remains independently sharded at the existing **$20 per recovery shard** default. Initial-entry sizing does not change recovery shard size.

The supervisor opens no more than its configured number of new positions per cycle and still requires a fresh market-discovery candidate, the paper asset allowlist, valid Kraken USD trading rules, and a successful Governor reservation.

## Venue and execution boundaries

- The current Kraken paper market universe is USD-quoted only. No USDT quote conversion or USDT-quoted Kraken trading is introduced.
- Paper execution uses Hummingbot's explicit `*_paper_trade` connector path and supported API executors.
- Hummingbot V2 controllers are not currently supported by the native paper-trade connector path. Keep those controller capabilities available in the full live architecture, but do not pretend they are paper-compatible.
- Paper-only mode continues to reject order-capable live connectors. Live execution remains a separately authorized mode and is not enabled by this paper capital model.
- Existing paper balances, order history, and credentials must not be reset by setup scripts. New-account defaults may be seeded; existing balances are preserved.
- `scripts/migrate_alpha_paper_balance.py` upgrades an existing account to $25,000 only after the local API proves paper-only mode, no trades/orders/active orders/positions exist, and all non-USD balances are zero. It is dry-run by default, creates a timestamped backup, and requires `--apply` to write.

## Configuration sources

- `spectra_core/models.py`: canonical default capital constants and policy schema
- `spectra_core/runtime.py`: paper-mode fallback for a missing entry ceiling
- `spectra_core/capital_governor.py`: reservation and reconciled-exposure enforcement
- `spectra_core/portfolio_supervisor.py`: even initial-entry sizing
- `spectra_core/vcap_entry.py`: initial-entry admission and reservation
- `scripts/configure_alpha_paper_state.py`: additive paper policy bootstrap
- `scripts/configure_alpha_paper_connector.py`: new-account balance defaults without overwriting existing balances
