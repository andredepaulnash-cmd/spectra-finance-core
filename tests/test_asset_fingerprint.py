from decimal import Decimal

import pytest

from spectra_core.asset_fingerprint import (
    AssetFingerprint,
    AssetObservation,
    FingerprintHorizon,
    observation_from_kraken_universe,
)
from spectra_core.kraken_market_fabric import (
    KrakenBookMetrics,
    KrakenTicker,
    KrakenTradingRule,
    KrakenUniverseSnapshot,
)


D = Decimal


def observation(
    *,
    timestamp: float,
    price: float,
    volume: float = 1_000_000,
    spread_bps: float | None = 5.0,
    depth_quote: float | None = 50_000,
    depth_imbalance: float | None = 0.1,
    microprice_edge_bps: float | None = 0.5,
) -> AssetObservation:
    return AssetObservation(
        trading_pair="BTC-USD",
        timestamp=timestamp,
        price=price,
        quote_volume_24h=volume,
        spread_bps=spread_bps,
        depth_quote=depth_quote,
        depth_imbalance=depth_imbalance,
        microprice_edge_bps=microprice_edge_bps,
    )


def test_cold_start_creates_every_horizon_without_fake_return():
    fp = AssetFingerprint.cold_start(
        observation(timestamp=1_000, price=100)
    )

    assert fp.trading_pair == "BTC-USD"
    assert fp.observation_count == 1
    assert fp.last_return_bps is None
    assert set(fp.windows) == set(FingerprintHorizon)

    for window in fp.windows.values():
        assert window.observations == 1
        assert window.ema_return_bps is None
        assert window.ema_quote_volume_24h == 1_000_000
        assert window.ema_spread_bps == 5.0


def test_update_tracks_return_volatility_and_directional_persistence():
    fp = AssetFingerprint.cold_start(
        observation(timestamp=1_000, price=100)
    )
    fp = fp.update(observation(timestamp=1_060, price=101))
    fp = fp.update(observation(timestamp=1_120, price=100))

    assert fp.observation_count == 3
    assert fp.last_return_bps == pytest.approx(
        (100 / 101 - 1) * 10_000
    )

    fast = fp.windows[FingerprintHorizon.FAST]
    assert fast.ema_abs_return_bps is not None
    assert fast.ema_abs_return_bps > 0
    assert fast.realized_volatility_bps is not None
    assert fast.realized_volatility_bps >= 0

    # + return followed by - return is evidence of reversal, not persistence.
    assert fast.direction_persistence is not None
    assert fast.direction_persistence < 0
    assert fast.reversal_tendency > 0
    assert fast.momentum_persistence == 0


def test_fast_memory_reacts_more_than_long_memory_to_new_information():
    fp = AssetFingerprint.cold_start(
        observation(timestamp=1_000, price=100, spread_bps=2)
    )
    fp = fp.update(
        observation(
            timestamp=1_060,
            price=100.1,
            spread_bps=2,
        )
    )
    fp = fp.update(
        observation(
            timestamp=1_360,
            price=105,
            spread_bps=40,
        )
    )

    fast = fp.windows[FingerprintHorizon.FAST]
    long = fp.windows[FingerprintHorizon.LONG]

    assert fast.ema_spread_bps is not None
    assert long.ema_spread_bps is not None
    assert fast.ema_spread_bps > long.ema_spread_bps

    assert fast.ema_return_bps is not None
    assert long.ema_return_bps is not None
    assert fast.ema_return_bps > long.ema_return_bps


def test_confidence_requires_time_coverage_not_just_many_fast_samples():
    fp = AssetFingerprint.cold_start(
        observation(timestamp=1_000, price=100)
    )

    # A dense burst of observations should mature FAST memory sooner than LONG.
    price = 100.0
    for i in range(1, 31):
        price *= 1.0001
        fp = fp.update(
            observation(
                timestamp=1_000 + i * 60,
                price=price,
            )
        )

    now = fp.last_seen_at
    fast_conf = fp.confidence(FingerprintHorizon.FAST, now=now)
    long_conf = fp.confidence(FingerprintHorizon.LONG, now=now)

    assert fast_conf > long_conf
    assert fast_conf > 0
    assert long_conf < 0.1
    assert FingerprintHorizon.LONG in fp.cold_start_horizons(now=now)


def test_staleness_is_horizon_specific():
    fp = AssetFingerprint.cold_start(
        observation(timestamp=1_000, price=100)
    )
    fp = fp.update(observation(timestamp=1_060, price=101))

    now = 1_060 + 20 * 60
    assert fp.windows[FingerprintHorizon.FAST].is_stale(now=now)
    assert not fp.windows[FingerprintHorizon.LONG].is_stale(now=now)


def test_round_trip_preserves_behavioral_memory():
    fp = AssetFingerprint.cold_start(
        observation(timestamp=1_000, price=100)
    )
    fp = fp.update(observation(timestamp=1_060, price=101))
    fp = fp.update(observation(timestamp=1_120, price=100.5))

    restored = AssetFingerprint.from_dict(fp.to_dict())

    assert restored == fp
    assert (
        restored.windows[FingerprintHorizon.FAST].realized_volatility_bps
        == fp.windows[FingerprintHorizon.FAST].realized_volatility_bps
    )


def test_restore_rejects_unknown_schema():
    fp = AssetFingerprint.cold_start(
        observation(timestamp=1_000, price=100)
    )
    raw = fp.to_dict()
    raw["schema_version"] = 999

    with pytest.raises(ValueError, match="unsupported"):
        AssetFingerprint.from_dict(raw)


def test_update_rejects_cross_pair_or_non_increasing_time():
    fp = AssetFingerprint.cold_start(
        observation(timestamp=1_000, price=100)
    )

    with pytest.raises(ValueError, match="timestamp must increase"):
        fp.update(observation(timestamp=1_000, price=101))

    with pytest.raises(ValueError, match="does not match"):
        fp.update(
            AssetObservation(
                trading_pair="ETH-USD",
                timestamp=1_060,
                price=50,
                quote_volume_24h=100_000,
            )
        )


def test_observation_validates_microstructure_bounds():
    with pytest.raises(ValueError, match="depth_imbalance"):
        AssetObservation(
            trading_pair="BTC-USD",
            timestamp=1_000,
            price=100,
            quote_volume_24h=1000,
            depth_imbalance=1.1,
        )

    with pytest.raises(ValueError, match="spread_bps"):
        AssetObservation(
            trading_pair="BTC-USD",
            timestamp=1_000,
            price=100,
            quote_volume_24h=1000,
            spread_bps=-1,
        )


def rule(pair: str) -> KrakenTradingRule:
    return KrakenTradingRule(
        trading_pair=pair,
        min_order_size=D("0.001"),
        min_notional_size=D("1"),
        min_order_value=D("1"),
        min_price_increment=D("0.01"),
        min_base_amount_increment=D("0.0001"),
        min_quote_amount_increment=D("0.01"),
        supports_limit_orders=True,
        supports_market_orders=True,
    )


def test_project_kraken_snapshot_into_fingerprint_observation():
    snapshot = KrakenUniverseSnapshot(
        tickers={
            "BTC-USD": KrakenTicker(
                trading_pair="BTC-USD",
                price=D("100"),
                base_volume_24h=D("1000"),
                quote_volume_24h=D("100000"),
                timestamp=1_000,
            )
        },
        trading_rules={"BTC-USD": rule("BTC-USD")},
        eligible_pairs=("BTC-USD",),
        rejected={},
        updated_at=1_002,
        books={
            "BTC-USD": KrakenBookMetrics(
                trading_pair="BTC-USD",
                best_bid=D("99"),
                best_ask=D("101"),
                mid_price=D("100"),
                spread_quote=D("2"),
                spread_bps=D("200"),
                bid_depth_quote=D("10000"),
                ask_depth_quote=D("9000"),
                depth_imbalance=D("0.052631578947368421"),
                microprice=D("100.2"),
                microprice_edge_bps=D("20"),
                timestamp=1_001,
            )
        },
    )

    obs = observation_from_kraken_universe(snapshot, "btc-usd")

    assert obs.trading_pair == "BTC-USD"
    assert obs.timestamp == 1_002
    assert obs.price == 100
    assert obs.quote_volume_24h == 100_000
    assert obs.spread_bps == 200
    assert obs.depth_quote == 19_000
    assert obs.depth_imbalance == pytest.approx(0.052631578947368421)
    assert obs.microprice_edge_bps == 20


def test_kraken_projection_allows_ticker_only_asset():
    snapshot = KrakenUniverseSnapshot(
        tickers={
            "BTC-USD": KrakenTicker(
                trading_pair="BTC-USD",
                price=D("100"),
                base_volume_24h=D("10"),
                quote_volume_24h=D("1000"),
                timestamp=1_000,
            )
        },
        trading_rules={"BTC-USD": rule("BTC-USD")},
        eligible_pairs=("BTC-USD",),
        rejected={},
        updated_at=1_001,
        books={},
    )

    obs = observation_from_kraken_universe(snapshot, "BTC-USD")

    assert obs.spread_bps is None
    assert obs.depth_quote is None
    assert obs.depth_imbalance is None
    assert obs.microprice_edge_bps is None


def test_kraken_projection_rejects_ineligible_pair():
    snapshot = KrakenUniverseSnapshot(
        tickers={},
        trading_rules={},
        eligible_pairs=(),
        rejected={"BROKEN-USD": "missing Kraken trading rule"},
        updated_at=1_000,
        books={},
    )

    with pytest.raises(ValueError, match="not eligible"):
        observation_from_kraken_universe(snapshot, "BROKEN-USD")
