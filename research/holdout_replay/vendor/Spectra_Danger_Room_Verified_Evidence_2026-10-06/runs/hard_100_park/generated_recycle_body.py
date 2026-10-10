def run(self, dataset: KlineDataset) -> FleetResult:
    cfg = self.config
    pairs = dataset.pairs
    if len(pairs) <= cfg.target_positions:
        raise ValueError("universe must contain at least one inactive replacement asset")
    if dataset.frames < 3:
        raise ValueError("at least three synchronized frames required")
    if not dataset.manifest.get("sparse_timeline_grid"):
        raise ValueError("adapter requires an explicit synchronized time axis, not fabricated synchronized candles")
    bases = tuple(base_asset(pair) for pair in pairs)
    if len(set(bases)) != len(bases):
        raise ValueError("duplicate normalized base asset in universe")
    if any(pair.split("-")[1] != dataset.manifest["ledger_quote"] for pair in pairs):
        raise ValueError("one explicit quote ledger is required")

    cash = cfg.initial_cash
    positions: dict[str, Position] = {}
    pending: list[FleetOrder] = []
    fills: list[FleetFill] = []
    samples: list[FleetSample] = []
    rejection_counts: Counter[str] = Counter()
    rejection_examples: list[dict] = []
    fingerprints: dict[str, AssetFingerprint] = {}
    profit_states = {}
    qualifying_wins: dict[str, list[int]] = {pair: [] for pair in pairs}
    earned_dttp_until: dict[str, int] = {}
    dttp_extended_positions: set[int] = set()
    ever_dttp_extended_positions: set[int] = set()
    earned_dttp_assets: set[str] = set()
    exit_signal_reasons: Counter[str] = Counter()
    volume_24h = {pair: ZERO for pair in pairs}
    next_order_id = 0
    next_position_id = 0
    realized = ZERO
    fees = ZERO
    turnover = ZERO
    peak_committed = ZERO
    initialized_index: int | None = None
    successful_dependencies = 0
    replacement_recycle_fills = 0
    planner_exit_signals = 0
    planner_recovery_signals = 0
    hard_target_exit_signals = 0
    earned_dttp_activation_events = 0
    earned_dttp_deactivation_events = 0
    earned_dttp_extension_decisions = 0
    qualifying_hard_wins = 0
    entry_basis_observed: list[D] = []
    decision_digest = hashlib.sha256()
    fee_rate = cfg.fee_bps / BPS
    slip = cfg.slippage_bps / BPS
    exit_costs = VCAPExitCostModel(
        expected_exit_fee_bps=cfg.fee_bps,
        slippage_allowance_bps=cfg.slippage_bps,
    )

    def reject(index: int, order: FleetOrder, reason: str) -> None:
        rejection_counts[reason] += 1
        if len(rejection_examples) < 100:
            rejection_examples.append(
                {
                    "timestamp": dataset.timestamp(index) + dataset.interval_seconds,
                    "order_id": order.order_id,
                    "pair": order.pair,
                    "kind": order.kind,
                    "reason": reason,
                }
            )

    def next_order(
        timestamp: int,
        pair: str,
        kind: str,
        *,
        budget: D = ZERO,
        position_id: int | None = None,
        depends_on: int | None = None,
    ) -> FleetOrder:
        nonlocal next_order_id
        next_order_id += 1
        return FleetOrder(
            next_order_id,
            timestamp,
            pair,
            kind,
            budget,
            position_id,
            depends_on,
        )

    for index in range(dataset.frames):
        bars = dataset.frame(index)
        timestamp = dataset.timestamp(index) + dataset.interval_seconds
        closed_this_frame: set[str] = set()
        successful_orders: set[int] = set()
        replaced_dependencies: set[int] = set()
        released_by_exit: dict[int, D] = {}

        # Orders were submitted after the prior bar and participate through
        # this complete minute.  Sells settle before dependent replacements.
        due = sorted(pending, key=lambda order: {"EXIT":0,"PARTIAL_RECYCLE":1}.get(order.kind,2))
        pending = []
        for order in due:
            bar = bars.get(order.pair)
            if bar is None:
                reject(index, order, "absent_immediate_next_candle")
                continue
            if (
                order.depends_on_order_id is not None
                and order.depends_on_order_id not in successful_orders
            ):
                reject(index, order, "dependency_not_filled")
                continue
            if (
                order.depends_on_order_id is not None
                and order.depends_on_order_id in replaced_dependencies
            ):
                reject(index, order, "replacement_fallback_not_needed")
                continue
            vwap = bar.vwap
            if vwap is None or bar.quote_volume <= ZERO:
                reject(index, order, "zero_recorded_volume")
                continue
            capacity = dataset.capacity(index, order.pair) * cfg.volume_participation
            position = positions.get(order.pair)
            if order.kind == "PARTIAL_RECYCLE":
                filled, reason = dataset.fill_partial(order, position, index, timestamp, cfg, capacity)
                if filled is None:
                    reject(index, order, reason)
                    continue
                fill, updated = filled
                cash += fill.gross_quote - fill.fee_quote
                realized += fill.realized_pnl
                fees += fill.fee_quote
                turnover += fill.gross_quote
                positions[order.pair] = updated
                fills.append(fill)
                successful_orders.add(order.order_id)
                continue
            if order.kind == "EXIT":
                if position is None or position.position_id != order.position_id:
                    reject(index, order, "stale_position_identity")
                    continue
                price = vwap * (ONE - slip)
                gross = position.quantity * price
                if gross > capacity:
                    reject(index, order, "volume_participation_fok")
                    continue
                fee = gross * fee_rate
                proceeds = gross - fee
                pnl = proceeds - position.basis
                if not dataset.floor_passes(position.basis, proceeds):
                    reject(index, order, "net_profit_limit_unfilled")
                    continue
                cash += proceeds
                realized += pnl
                fees += fee
                turnover += gross
                fills.append(
                    FleetFill(
                        timestamp,
                        order.order_id,
                        order.pair,
                        order.kind,
                        position.position_id,
                        position.quantity,
                        price,
                        gross,
                        fee,
                        position.basis,
                        pnl,
                        None,
                    )
                )
                if self.exit_strategy.mode == FleetExitMode.EARNED_DTTP:
                    was_extended = position.position_id in dttp_extended_positions
                    dttp_extended_positions.discard(position.position_id)
                    active = earned_dttp_until.get(order.pair, 0) > timestamp
                    realized_bps = (
                        pnl / position.basis * BPS
                        if position.basis > ZERO
                        else ZERO
                    )
                    # Only completed hard-target wins earn DTTP.  A trade
                    # already extended by DTTP cannot recursively re-arm it.
                    if (
                        not was_extended
                        and not active
                        and realized_bps
                        >= self.exit_strategy.qualifying_win_bps
                    ):
                        qualifying_hard_wins += 1
                        cutoff = (
                            timestamp
                            - self.exit_strategy.win_window_seconds
                        )
                        history = [
                            won_at
                            for won_at in qualifying_wins[order.pair]
                            if won_at >= cutoff
                        ]
                        history.append(timestamp)
                        if len(history) >= self.exit_strategy.wins_required:
                            earned_dttp_until[order.pair] = (
                                timestamp
                                + self.exit_strategy.activation_seconds
                            )
                            history = []
                            earned_dttp_activation_events += 1
                            earned_dttp_assets.add(order.pair)
                        qualifying_wins[order.pair] = history
                dataset.record_full_exit(fills[-1])
                del positions[order.pair]
                profit_states.pop(order.pair, None)
                closed_this_frame.add(order.pair)
                successful_orders.add(order.order_id)
                released_by_exit[order.order_id] = proceeds
                continue

            if order.kind == "ENTRY":
                if position is not None:
                    reject(index, order, "duplicate_active_asset")
                    continue
            elif position is None or position.position_id != order.position_id:
                reject(index, order, "stale_position_identity")
                continue
            gross = order.budget / (ONE + fee_rate)
            if gross < cfg.simulated_min_order_quote:
                reject(index, order, "simulated_order_minimum")
                continue
            if gross > capacity:
                reject(index, order, "volume_participation_fok")
                continue
            price = vwap * (ONE + slip)
            quantity = gross / price
            fee = gross * fee_rate
            total = gross + fee
            if total > cash - dataset.parked_cash():
                reject(index, order, "parked_partial_proceeds" if dataset.park_proceeds else "insufficient_shared_cash")
                continue
            if total > cash:
                reject(index, order, "insufficient_shared_cash")
                continue
            equity_before = cash + sum(
                current.quantity * dataset.marks[pair]
                for pair, current in positions.items()
            )
            reserve_breached = cash - total < equity_before * cfg.reserve_fraction
            recycled_replacement = (
                order.depends_on_order_id is not None
                and total
                <= released_by_exit.get(order.depends_on_order_id, ZERO)
            )
            if reserve_breached and not recycled_replacement:
                reject(index, order, "shared_reserve")
                continue
            if order.kind == "ENTRY":
                if total < cfg.minimum_position_basis:
                    reject(index, order, "undersized_position_identity")
                    continue
                if len(positions) >= cfg.target_positions:
                    reject(index, order, "fleet_target_already_full")
                    continue
                next_position_id += 1
                position = Position(
                    next_position_id,
                    order.pair,
                    quantity,
                    total,
                    timestamp,
                )
                positions[order.pair] = position
                entry_basis_observed.append(total)
            else:
                pair_cap = equity_before * cfg.pair_cap_fraction
                if position.basis + total > pair_cap:
                    reject(index, order, "pair_basis_cap")
                    continue
                position = replace(
                    position,
                    quantity=position.quantity + quantity,
                    basis=position.basis + total,
                    recovery_count=position.recovery_count + 1,
                )
                positions[order.pair] = position
                profit_states.pop(order.pair, None)
            cash -= total
            fees += fee
            turnover += gross
            fills.append(
                FleetFill(
                    timestamp,
                    order.order_id,
                    order.pair,
                    order.kind,
                    position.position_id,
                    quantity,
                    price,
                    gross,
                    fee,
                    ZERO,
                    ZERO,
                    order.depends_on_order_id,
                )
            )
            dataset.record_buy(fills[-1], position, cash + total)
            successful_orders.add(order.order_id)
            if order.depends_on_order_id is not None:
                replaced_dependencies.add(order.depends_on_order_id)
                successful_dependencies += 1
                if reserve_breached:
                    replacement_recycle_fills += 1

        # Update only with the just-completed bar.  Rolling volume subtracts
        # exactly the minute that left the causal 24-hour window.
        dataset.roll_volume(index, volume_24h, bars)
        for pair, bar in bars.items():
            observation = AssetObservation(
                pair,
                timestamp,
                float(bar.close),
                float(volume_24h[pair]),
            )
            prior = fingerprints.get(pair)
            fingerprints[pair] = (
                AssetFingerprint.cold_start(observation)
                if prior is None
                else prior.update(observation)
            )

        committed = sum((position.basis for position in positions.values()), ZERO)
        marked = sum(
            (position.quantity * dataset.marks[pair] for pair, position in positions.items()),
            ZERO,
        )
        equity = cash + marked
        if cash < ZERO or abs(cash + committed - cfg.initial_cash - realized) > D("1e-18"):
            raise ArithmeticError("shared fleet accounting invariant failed")
        if len(positions) > cfg.target_positions:
            raise ArithmeticError("fleet exceeded requested simultaneous positions")
        active_bases = {base_asset(pair) for pair in positions}
        if len(active_bases) != len(positions):
            raise ArithmeticError("duplicate simultaneous base-asset exposure")
        if any(position.basis < cfg.minimum_position_basis for position in positions.values()):
            raise ArithmeticError("active position fell below identity minimum")
        peak_committed = max(peak_committed, committed)
        samples.append(
            FleetSample(
                timestamp,
                cash,
                committed,
                marked,
                equity,
                len(positions),
                len(active_bases),
            )
        )
        if initialized_index is None and len(positions) == cfg.target_positions:
            initialized_index = index

        spendable = min(max(ZERO, cash - equity * cfg.reserve_fraction), max(ZERO, cash - dataset.parked_cash()))
        pending_pairs: set[str] = set()
        exit_orders: list[FleetOrder] = []
        recovery_orders: list[FleetOrder] = []
        partial_orders: list[FleetOrder] = []
        for pair, position in sorted(positions.items()):
            bar = bars.get(pair)
            if bar is None:
                continue
            state = VCAPPositionState(
                pair,
                position.quantity,
                position.basis,
                bar.close,
            )
            previous_profit_state = profit_states.get(pair)
            exit_plan = plan_vcap_profit_exit(
                state,
                costs=exit_costs,
                now=timestamp,
                fingerprint=fingerprints[pair],
                previous_state=previous_profit_state,
                policy=self.exit_policy,
            )
            profit_states[pair] = exit_plan.state
            effective_action = exit_plan.action
            effective_reason = exit_plan.reasons[-1]
            if (
                self.exit_strategy.mode == FleetExitMode.HARD_TARGET
                and exit_plan.action == VCAPProfitExitAction.EXTEND
            ):
                effective_action = VCAPProfitExitAction.EXIT
                effective_reason = "hard_net_target_reached"
                hard_target_exit_signals += 1
            elif self.exit_strategy.mode == FleetExitMode.EARNED_DTTP:
                active_until = earned_dttp_until.get(pair, 0)
                earned = active_until > timestamp
                if exit_plan.action == VCAPProfitExitAction.EXTEND:
                    first_floor = (
                        previous_profit_state is None
                        or previous_profit_state.profit_floor_reached_at is None
                    )
                    supported = (
                        exit_plan.continuation_support
                        >= self.exit_strategy.extension_continuation_threshold
                        if first_floor
                        else exit_plan.continuation_support
                        >= self.exit_strategy.continuation_exit_threshold
                    )
                    if earned and supported:
                        dttp_extended_positions.add(position.position_id)
                        ever_dttp_extended_positions.add(position.position_id)
                        earned_dttp_extension_decisions += 1
                        effective_reason = "earned_dttp_extension_supported"
                    else:
                        effective_action = VCAPProfitExitAction.EXIT
                        hard_target_exit_signals += 1
                        effective_reason = (
                            "earned_dttp_continuation_failed"
                            if earned
                            else "hard_net_target_reached"
                        )
                        if earned:
                            earned_dttp_until[pair] = timestamp
                            qualifying_wins[pair] = []
                            earned_dttp_deactivation_events += 1
                elif earned and exit_plan.action == VCAPProfitExitAction.EXIT:
                    # Giveback, stagnation, or a floor breach means the
                    # temporary momentum privilege has ended.
                    earned_dttp_until[pair] = timestamp
                    qualifying_wins[pair] = []
                    earned_dttp_deactivation_events += 1
                    effective_reason = "earned_dttp_protection_exit"
            if dataset.hard_target:
                effective_action = (VCAPProfitExitAction.EXIT
                    if exit_plan.estimated_net_profit_bps >= dataset.whole_target_bps
                    else VCAPProfitExitAction.WAIT)
                effective_reason = ("strict_hard_net_target_reached"
                    if effective_action == VCAPProfitExitAction.EXIT else "strict_hard_net_target_not_reached")
            decision_digest.update(
                (
                    f"{timestamp}|{pair}|{self.exit_strategy.mode}|"
                    f"{exit_plan.action}|{effective_action}|{effective_reason}|"
                    f"{exit_plan.estimated_net_profit_bps}|"
                    f"{exit_plan.continuation_support}\n"
                ).encode()
            )
            if effective_action == VCAPProfitExitAction.EXIT:
                planner_exit_signals += 1
                exit_signal_reasons[effective_reason] += 1
                order = next_order(
                    timestamp,
                    pair,
                    "EXIT",
                    position_id=position.position_id,
                )
                dataset.note_full(order, position, bar.close,
                    exit_plan.estimated_net_profit_bps, effective_action)
                exit_orders.append(order)
                pending_pairs.add(pair)
                continue
            partial = dataset.recycle_manifest(position, bar.close, timestamp, cfg, effective_action)
            if partial is not None:
                order = next_order(timestamp, pair, "PARTIAL_RECYCLE", position_id=position.position_id)
                dataset.note_partial(order, partial, bar.close, exit_plan.action)
                partial_orders.append(order)
                pending_pairs.add(pair)
                continue
            average = position.basis / position.quantity
            if not dataset.recovery_enabled or bar.close >= average * (ONE - cfg.recovery_trigger) or spendable <= ZERO:
                continue
            proposal = CapitalProposal(
                f"fleet:{timestamp}:{position.position_id}",
                pair,
                IntentKind.RECOVERY,
                cfg.recovery_seed_quote,
            )
            recovery = plan_vcap_recovery(
                proposal,
                fingerprints[pair],
                state,
                now=timestamp,
                fingerprint_policy=self.fingerprint_policy,
                vcap_policy=self.recovery_policy,
            )
            if recovery.planned_amount_quote <= ZERO:
                continue
            pair_available = max(ZERO, equity * cfg.pair_cap_fraction - position.basis)
            authorization = min(
                recovery.planned_amount_quote,
                pair_available,
                spendable / (ONE + fee_rate),
            )
            if authorization < cfg.simulated_min_order_quote:
                continue
            rule = KrakenTradingRule(
                pair,
                cfg.simulated_base_step,
                cfg.simulated_min_order_quote,
                ZERO,
                ZERO,
                cfg.simulated_base_step,
                ZERO,
                True,
                True,
            )
            entry_price = bar.close * (ONE + slip)
            tranche = plan_vcap_tranches(
                recovery,
                authorized_quote=authorization,
                current_price=entry_price,
                trading_rule=rule,
                policy=self.tranche_policy,
            )
            if not tranche.tranches:
                continue
            budget = tranche.tranches[0].quote_amount * (ONE + fee_rate)
            recovery_orders.append(
                next_order(
                    timestamp,
                    pair,
                    "RECOVERY",
                    budget=budget,
                    position_id=position.position_id,
                )
            )
            planner_recovery_signals += 1
            pending_pairs.add(pair)
            spendable -= budget

        # Rank only from observations available at this close.  The first
        # bar uses its realized range; later bars use the FAST fingerprint's
        # causal absolute-return memory.  Volume is only a tie-breaker.
        def movement_rank(pair: str) -> tuple[float, float, str]:
            window = fingerprints[pair].windows[FingerprintHorizon.FAST]
            movement = window.ema_abs_return_bps
            if movement is None:
                bar = bars[pair]
                movement = float((bar.high - bar.low) / bar.open * BPS)
            return (-movement, -float(volume_24h[pair]), pair)

        entry_gross = cfg.entry_budget / (ONE + fee_rate)
        candidates = [
            pair
            for pair in bars
            if pair not in positions
            and pair not in pending_pairs
            and pair not in closed_this_frame
            # The current completed minute is a causal eligibility check,
            # not a guarantee about the following minute's volume.
            and bars[pair].quote_volume * cfg.volume_participation
            >= entry_gross
        ]
        candidates.sort(key=movement_rank)
        entry_orders: list[FleetOrder] = []
        # First repair any already-visible shortfall using current cash.
        shortfall = cfg.target_positions - len(positions)
        while shortfall > 0 and candidates and spendable >= cfg.entry_budget:
            pair = candidates.pop(0)
            entry_orders.append(
                next_order(timestamp, pair, "ENTRY", budget=cfg.entry_budget)
            )
            pending_pairs.add(pair)
            spendable -= cfg.entry_budget
            shortfall -= 1
        if shortfall > 0:
            reason = (
                "insufficient_shared_cash_for_fleet"
                if spendable < cfg.entry_budget
                else "insufficient_liquidity_eligible_assets"
            )
            rejection_counts[reason] += shortfall
        # Pre-authorize a distinct inactive replacement for every pending
        # exit.  It may fill only if that exact exit fills first next bar.
        for exit_order in exit_orders:
            assigned = 0
            while candidates and assigned < cfg.replacement_candidates_per_exit:
                pair = candidates.pop(0)
                entry_orders.append(
                    next_order(
                        timestamp,
                        pair,
                        "ENTRY",
                        budget=cfg.entry_budget,
                        depends_on=exit_order.order_id,
                    )
                )
                pending_pairs.add(pair)
                assigned += 1
            if assigned == 0:
                rejection_counts["no_distinct_replacement_candidate"] += 1

        pending = exit_orders + partial_orders + recovery_orders + entry_orders

    final_marks = dataset.marks
    committed = sum((position.basis for position in positions.values()), ZERO)
    marked = sum(
        (position.quantity * final_marks[pair] for pair, position in positions.items()),
        ZERO,
    )
    equity = cash + marked
    unrealized = marked - committed
    drawdown = ZERO
    peak = samples[0].equity
    for sample in samples:
        peak = max(peak, sample.equity)
        if peak > ZERO:
            drawdown = max(drawdown, (peak - sample.equity) / peak)
    if initialized_index is None:
        post_init = samples
    else:
        post_init = samples[initialized_index:]
    below_target = sum(
        sample.active_positions < cfg.target_positions for sample in post_init
    )
    active_minutes = sum(sample.active_positions for sample in samples)
    position_minutes_capacity = cfg.target_positions * len(samples)
    entry_fills = [fill for fill in fills if fill.kind == "ENTRY"]
    exit_fills = [fill for fill in fills if fill.kind == "EXIT"]
    recovery_fills = [fill for fill in fills if fill.kind == "RECOVERY"]
    metrics = {
        "starting_cash": cfg.initial_cash,
        "ending_cash": cash,
        "ending_equity": equity,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_pnl": equity - cfg.initial_cash,
        "fees": fees,
        "turnover_quote": turnover,
        "max_drawdown_fraction": drawdown,
        "open_basis": committed,
        "peak_committed_basis": peak_committed,
        "ending_positions": len(positions),
        "target_positions": cfg.target_positions,
        "fleet_initialized": initialized_index is not None,
        "first_target_timestamp": (
            None if initialized_index is None else samples[initialized_index].timestamp
        ),
        "minimum_positions_after_initialization": min(
            sample.active_positions for sample in post_init
        ),
        "minutes_below_target_after_initialization": below_target * dataset.interval_seconds // 60,
        "fleet_position_minute_fill_ratio": (
            D(active_minutes) / D(position_minutes_capacity)
            if position_minutes_capacity
            else ZERO
        ),
        "entry_fills": len(entry_fills),
        "rotations": max(0, len(entry_fills) - cfg.target_positions),
        "completed_positions": len(exit_fills),
        "recovery_fills": len(recovery_fills),
        "linked_replacements_filled": successful_dependencies,
        "linked_replacements_funded_from_released_capital": replacement_recycle_fills,
        "unique_position_ids": len({fill.position_id for fill in entry_fills}),
        "unique_assets_ever_entered": len({fill.pair for fill in entry_fills}),
        "minimum_entry_basis": min(entry_basis_observed) if entry_basis_observed else None,
        "maximum_simultaneous_positions": max(
            sample.active_positions for sample in samples
        ),
        "duplicate_asset_violations": 0,
        "expired_orders": len(pending),
        "planner_exit_signals": planner_exit_signals,
        "planner_recovery_signals": planner_recovery_signals,
        "exit_mode": self.exit_strategy.mode,
        "target_net_profit_bps": self.exit_policy.target_net_profit_bps,
        "hard_target_exit_signals": hard_target_exit_signals,
        "exit_signal_reasons": dict(sorted(exit_signal_reasons.items())),
        "qualifying_hard_wins": qualifying_hard_wins,
        "earned_dttp_activation_events": earned_dttp_activation_events,
        "earned_dttp_assets_activated": len(earned_dttp_assets),
        "earned_dttp_deactivation_events": earned_dttp_deactivation_events,
        "earned_dttp_extension_decisions": earned_dttp_extension_decisions,
        "earned_dttp_extended_positions": len(ever_dttp_extended_positions),
        "execution_model": dataset.execution_model,
        "evidence_class": "recorded OHLCV; estimated execution; not top-of-book",
    }
    fingerprint = hashlib.sha256(
        (
            _source_hash(Path(__file__))
            + _source_hash(Path(__file__).with_name("binance_kline_import.py"))
            + dataset.manifest["source_fingerprint"]
            + json.dumps(asdict(cfg), default=str, sort_keys=True)
            + json.dumps(asdict(self.exit_strategy), default=str, sort_keys=True)
            + json.dumps(asdict(self.exit_policy), default=str, sort_keys=True)
            + decision_digest.hexdigest()
        ).encode()
    ).hexdigest()
    return FleetResult(
        cfg,
        self.exit_strategy,
        self.exit_policy,
        dataset.manifest,
        cash,
        tuple(sorted(positions.values(), key=lambda position: position.position_id)),
        tuple(fills),
        tuple(samples),
        dict(sorted(rejection_counts.items())),
        tuple(rejection_examples),
        metrics,
        fingerprint,
    )
