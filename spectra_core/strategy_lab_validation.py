from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import statistics
from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import StrEnum
from itertools import combinations
from math import sqrt
from pathlib import Path
from statistics import NormalDist
from typing import Mapping, Sequence


ZERO = Decimal("0")
ONE = Decimal("1")


class ResearchStage(StrEnum):
    HISTORICAL = "HISTORICAL"
    ADVERSARIAL = "ADVERSARIAL"
    OUT_OF_SAMPLE = "OUT_OF_SAMPLE"
    SHADOW = "SHADOW"
    CANARY = "CANARY"


class PromotionDecision(StrEnum):
    BLOCKED = "BLOCKED"
    CANARY_ELIGIBLE = "CANARY_ELIGIBLE"


@dataclass(frozen=True)
class WalkForwardFold:
    train_start: int
    train_end: int
    test_start: int
    test_end: int

    @property
    def train_range(self) -> range:
        return range(self.train_start, self.train_end)

    @property
    def test_range(self) -> range:
        return range(self.test_start, self.test_end)


def purged_walk_forward_splits(
    observation_count: int,
    *,
    train_size: int,
    test_size: int,
    purge_size: int = 0,
    embargo_size: int = 0,
) -> tuple[WalkForwardFold, ...]:
    """Create chronological train/test folds with an explicit leakage gap."""

    if observation_count <= 0:
        raise ValueError("observation_count must be positive")
    if train_size <= 0 or test_size <= 0:
        raise ValueError("train_size and test_size must be positive")
    if purge_size < 0 or embargo_size < 0:
        raise ValueError("purge_size and embargo_size must be non-negative")

    folds: list[WalkForwardFold] = []
    train_start = 0
    while True:
        train_end = train_start + train_size
        test_start = train_end + purge_size
        test_end = test_start + test_size
        if test_end > observation_count:
            break
        folds.append(
            WalkForwardFold(
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
            )
        )
        train_start = test_end + embargo_size
    return tuple(folds)


@dataclass(frozen=True)
class DeflatedSharpeResult:
    observed_sharpe: float
    expected_maximum_sharpe: float
    probability: float
    trial_count: int
    observations: int


def _moments(values: Sequence[float]) -> tuple[float, float, float, float]:
    if len(values) < 3:
        raise ValueError("at least three returns are required")
    mean = statistics.fmean(values)
    variance = statistics.fmean((value - mean) ** 2 for value in values)
    if variance <= 0:
        return mean, 0.0, 0.0, 3.0
    sigma = sqrt(variance)
    skew = statistics.fmean(
        ((value - mean) / sigma) ** 3 for value in values
    )
    kurtosis = statistics.fmean(
        ((value - mean) / sigma) ** 4 for value in values
    )
    return mean, sigma, skew, kurtosis


def deflated_sharpe_ratio(
    selected_returns: Sequence[float],
    *,
    trial_sharpes: Sequence[float],
) -> DeflatedSharpeResult:
    """Bailey/Lopez de Prado DSR at the return observation frequency.

    The trial ledger matters: every searched configuration contributes to the
    expected-maximum benchmark instead of disappearing after a winner is found.
    """

    if not trial_sharpes:
        raise ValueError("trial_sharpes cannot be empty")
    mean, sigma, skew, kurtosis = _moments(selected_returns)
    observed = 0.0 if sigma == 0 else mean / sigma
    trial_count = len(trial_sharpes)

    if trial_count == 1:
        expected_max = 0.0
    else:
        trial_variance = statistics.pvariance(trial_sharpes)
        trial_sigma = sqrt(max(0.0, trial_variance))
        normal = NormalDist()
        gamma = 0.5772156649015329
        expected_max = trial_sigma * (
            (1.0 - gamma)
            * normal.inv_cdf(1.0 - 1.0 / trial_count)
            + gamma
            * normal.inv_cdf(
                1.0 - 1.0 / (trial_count * 2.718281828459045)
            )
        )

    denominator_sq = (
        1.0
        - skew * observed
        + ((kurtosis - 1.0) / 4.0) * observed * observed
    )
    if denominator_sq <= 0:
        probability = 0.0
    else:
        statistic = (
            (observed - expected_max)
            * sqrt(len(selected_returns) - 1)
            / sqrt(denominator_sq)
        )
        probability = NormalDist().cdf(statistic)

    return DeflatedSharpeResult(
        observed_sharpe=observed,
        expected_maximum_sharpe=expected_max,
        probability=max(0.0, min(1.0, probability)),
        trial_count=trial_count,
        observations=len(selected_returns),
    )


def probability_of_backtest_overfitting(
    fold_scores: Mapping[str, Sequence[float]],
) -> float:
    """Estimate PBO using combinatorially symmetric fold partitions.

    Each strategy must have one score per chronological fold. Half the folds
    select the in-sample winner; the complementary half measure whether that
    winner falls below the median out of sample.
    """

    if len(fold_scores) < 2:
        raise ValueError("PBO requires at least two strategy variants")
    lengths = {len(scores) for scores in fold_scores.values()}
    if len(lengths) != 1:
        raise ValueError("all strategies must have the same fold count")
    fold_count = lengths.pop()
    if fold_count < 4 or fold_count % 2:
        raise ValueError("PBO requires an even fold count of at least four")

    names = sorted(fold_scores)
    half = fold_count // 2
    overfit = 0
    partitions = 0
    # Symmetric complements are duplicates for PBO, so keep only partitions
    # containing fold zero.
    for train_indices in combinations(range(fold_count), half):
        if 0 not in train_indices:
            continue
        train = set(train_indices)
        test = [index for index in range(fold_count) if index not in train]
        train_means = {
            name: statistics.fmean(
                fold_scores[name][index] for index in train_indices
            )
            for name in names
        }
        winner = max(names, key=lambda name: (train_means[name], name))
        oos_scores = {
            name: statistics.fmean(
                fold_scores[name][index] for index in test
            )
            for name in names
        }
        median = statistics.median(oos_scores.values())
        if oos_scores[winner] < median:
            overfit += 1
        partitions += 1
    return 0.0 if partitions == 0 else overfit / partitions


@dataclass(frozen=True)
class MonteCarloStress:
    path_count: int
    median_terminal_equity: Decimal
    worst_terminal_equity: Decimal
    median_max_drawdown: Decimal
    worst_max_drawdown: Decimal


def block_bootstrap_stress(
    returns: Sequence[Decimal],
    *,
    starting_equity: Decimal,
    path_count: int = 500,
    block_size: int = 5,
    seed: int = 0,
) -> MonteCarloStress:
    """Deterministic block bootstrap preserving short return clusters."""

    if not returns:
        raise ValueError("returns cannot be empty")
    if path_count <= 0 or block_size <= 0:
        raise ValueError("path_count and block_size must be positive")
    start = Decimal(starting_equity)
    if not start.is_finite() or start <= ZERO:
        raise ValueError("starting_equity must be positive and finite")
    clean = tuple(Decimal(value) for value in returns)
    if any(not value.is_finite() or value <= Decimal("-1") for value in clean):
        raise ValueError("returns must be finite and greater than -1")

    rng = random.Random(seed)
    terminals: list[Decimal] = []
    drawdowns: list[Decimal] = []
    length = len(clean)
    for _ in range(path_count):
        sampled: list[Decimal] = []
        while len(sampled) < length:
            begin = rng.randrange(length)
            for offset in range(block_size):
                sampled.append(clean[(begin + offset) % length])
                if len(sampled) == length:
                    break
        equity = start
        peak = start
        max_drawdown = ZERO
        for value in sampled:
            equity *= ONE + value
            peak = max(peak, equity)
            max_drawdown = max(
                max_drawdown,
                ZERO if peak == ZERO else (peak - equity) / peak,
            )
        terminals.append(equity)
        drawdowns.append(max_drawdown)

    terminals.sort()
    drawdowns.sort()
    middle = path_count // 2
    return MonteCarloStress(
        path_count=path_count,
        median_terminal_equity=terminals[middle],
        worst_terminal_equity=terminals[0],
        median_max_drawdown=drawdowns[middle],
        worst_max_drawdown=drawdowns[-1],
    )


def parameter_neighborhood_stability(
    center_score: Decimal,
    neighbor_scores: Sequence[Decimal],
    *,
    tolerance_fraction: Decimal = Decimal("0.25"),
) -> Decimal:
    """Fraction of nearby parameterizations that remain near the center."""

    if not neighbor_scores:
        raise ValueError("neighbor_scores cannot be empty")
    center = Decimal(center_score)
    tolerance = Decimal(tolerance_fraction)
    if not center.is_finite() or not tolerance.is_finite():
        raise ValueError("inputs must be finite")
    if tolerance < ZERO or tolerance > ONE:
        raise ValueError("tolerance_fraction must be in [0, 1]")
    scale = max(abs(center), Decimal("0.00000001"))
    floor = center - scale * tolerance
    acceptable = sum(
        1 for score in neighbor_scores if Decimal(score) >= floor
    )
    return Decimal(acceptable) / Decimal(len(neighbor_scores))


@dataclass(frozen=True)
class PromotionEvidence:
    adversarial_passed: bool
    out_of_sample_passed: bool
    shadow_passed: bool
    deflated_sharpe_probability: Decimal
    pbo: Decimal
    parameter_stability: Decimal
    max_drawdown: Decimal


@dataclass(frozen=True)
class PromotionPolicy:
    min_deflated_sharpe_probability: Decimal = Decimal("0.95")
    max_pbo: Decimal = Decimal("0.20")
    min_parameter_stability: Decimal = Decimal("0.70")
    max_drawdown: Decimal = Decimal("0.30")


@dataclass(frozen=True)
class PromotionResult:
    decision: PromotionDecision
    reasons: tuple[str, ...]


def evaluate_canary_promotion(
    evidence: PromotionEvidence,
    *,
    policy: PromotionPolicy | None = None,
) -> PromotionResult:
    """Research can only nominate a canary; it can never authorize live capital."""

    p = policy or PromotionPolicy()
    reasons: list[str] = []
    if not evidence.adversarial_passed:
        reasons.append("adversarial_suite_not_passed")
    if not evidence.out_of_sample_passed:
        reasons.append("out_of_sample_not_passed")
    if not evidence.shadow_passed:
        reasons.append("shadow_mode_not_passed")
    if (
        evidence.deflated_sharpe_probability
        < p.min_deflated_sharpe_probability
    ):
        reasons.append("deflated_sharpe_below_threshold")
    if evidence.pbo > p.max_pbo:
        reasons.append("backtest_overfitting_probability_too_high")
    if evidence.parameter_stability < p.min_parameter_stability:
        reasons.append("parameter_neighborhood_is_brittle")
    if evidence.max_drawdown > p.max_drawdown:
        reasons.append("drawdown_exceeds_research_limit")
    return PromotionResult(
        decision=(
            PromotionDecision.BLOCKED
            if reasons
            else PromotionDecision.CANARY_ELIGIBLE
        ),
        reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class ResearchTrial:
    trial_id: str
    strategy_name: str
    stage: ResearchStage
    dataset_fingerprint: str
    parameters: Mapping[str, object]
    metrics: Mapping[str, object]
    created_at: float

    def __post_init__(self) -> None:
        if not self.trial_id.strip() or not self.strategy_name.strip():
            raise ValueError("trial_id and strategy_name are required")
        if not self.dataset_fingerprint.strip():
            raise ValueError("dataset_fingerprint is required")
        if self.created_at < 0:
            raise ValueError("created_at must be non-negative")


def dataset_fingerprint(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class StrategyLabStore:
    """Durable trial ledger: failed/losing searches cannot be forgotten."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_lab_trials (
                    trial_id TEXT PRIMARY KEY,
                    strategy_name TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    dataset_fingerprint TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_strategy_lab_trials_strategy_stage
                ON strategy_lab_trials(strategy_name, stage)
                """
            )

    @staticmethod
    def _json(value: Mapping[str, object]) -> str:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    def record(self, trial: ResearchTrial) -> None:
        parameters = self._json(trial.parameters)
        metrics = self._json(trial.metrics)
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT *
                FROM strategy_lab_trials
                WHERE trial_id = ?
                """,
                (trial.trial_id,),
            ).fetchone()
            if existing is not None:
                expected = (
                    trial.strategy_name,
                    trial.stage.value,
                    trial.dataset_fingerprint,
                    parameters,
                    metrics,
                    trial.created_at,
                )
                actual = (
                    existing["strategy_name"],
                    existing["stage"],
                    existing["dataset_fingerprint"],
                    existing["parameters_json"],
                    existing["metrics_json"],
                    existing["created_at"],
                )
                if actual != expected:
                    raise RuntimeError(
                        "strategy trial replay conflicts with durable ledger"
                    )
                return
            conn.execute(
                """
                INSERT INTO strategy_lab_trials (
                    trial_id,
                    strategy_name,
                    stage,
                    dataset_fingerprint,
                    parameters_json,
                    metrics_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trial.trial_id,
                    trial.strategy_name,
                    trial.stage.value,
                    trial.dataset_fingerprint,
                    parameters,
                    metrics,
                    trial.created_at,
                ),
            )

    def list_trials(
        self,
        *,
        strategy_name: str | None = None,
    ) -> tuple[ResearchTrial, ...]:
        with self._connect() as conn:
            if strategy_name is None:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM strategy_lab_trials
                    ORDER BY created_at, trial_id
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM strategy_lab_trials
                    WHERE strategy_name = ?
                    ORDER BY created_at, trial_id
                    """,
                    (strategy_name,),
                ).fetchall()
        return tuple(
            ResearchTrial(
                trial_id=str(row["trial_id"]),
                strategy_name=str(row["strategy_name"]),
                stage=ResearchStage(str(row["stage"])),
                dataset_fingerprint=str(row["dataset_fingerprint"]),
                parameters=json.loads(str(row["parameters_json"])),
                metrics=json.loads(str(row["metrics_json"])),
                created_at=float(row["created_at"]),
            )
            for row in rows
        )

    def trial_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM strategy_lab_trials"
            ).fetchone()
        return int(row["count"])
