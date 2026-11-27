# Alpha 2.0 Position-Cap Decision

**Decision: 74 is the hard maximum; 50 is the initial operating target.** `MAX_OPEN_POSITIONS=74`, `INITIAL_TARGET_POSITIONS=50`, `MAX_NEW_POSITIONS_PER_CYCLE=10`.

This resolves the earlier 50-to-83 range and supersedes the prior 50/83 selectable-target proposal for Alpha 2.0. The Architect's explicit direction is 74 maximum concurrent live positions (paper positions during this release). The hard policy constant is `MAX_OPEN_POSITIONS=74`; the supervisor and application default target is `INITIAL_TARGET_POSITIONS=50` (configured target `SPECTRA_MAX_POSITIONS` default 50 range 1-74); staged acceptance is 10, 25, 50, 74; per-cycle admission is 10. Normal operation is not a spending mandate to reach 74.

The cap counts both `RESERVED` and `OPEN` positions. A pending entry consumes a slot immediately; it cannot create a temporary 75th slot while Hummingbot is still processing an order. A position slot is released only after all initial, DCA, and VCAP lots have been fully sold and reconciled, then the position is closed.

Seventy-four is a ceiling, not a command to fill every slot. Condor may recommend fewer eligible assets; the supervisor admits only assets that pass market filters and CapitalGovernor checks. The default supervisor admits at most ten new positions per cycle so the system can be observed as it scales.

Acceptance stages are 10, 25, 50, and 74 concurrent positions. The 74-position test must demonstrate that a 75th reservation is rejected, including when some of the existing 74 are still reserved or awaiting fills.
