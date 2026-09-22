from decimal import Decimal

import pytest

from spectra_core.capital_governor import CapitalGovernor
from spectra_core.models import CapitalPolicy
from spectra_core.position_hold_accounting import PositionHoldAccounting


D = Decimal


def governor(tmp_path):
    return CapitalGovernor(
        tmp_path / "capital.sqlite3",
        CapitalPolicy(
            reserve_floor_quote=D("0"),
            max_total_committed_quote=D("100"),
            max_pair_quote=D("100"),
            max_position_quote=D("100"),
            max_recovery_quote=D("100"),
            max_leverage=D("1"),
            reservation_ttl_seconds=120,
            position_hold_snapshot_ttl_seconds=30,
        ),
    )


def hold(*, pair="BTC-USD", quote="15"):
    return PositionHoldAccounting(
        account_name="master_account",
        connector_name="kraken",
        trading_pair=pair,
        controller_id="spectra",
        status="ACTIVE",
        buy_amount_base=D("0.001"),
        buy_amount_quote=D(quote),
        sell_amount_base=D("0"),
        sell_amount_quote=D("0"),
        realized_pnl_quote=D("0"),
        cum_fees_quote=D("0.02"),
        executor_ids=("exec-recovery-1",),
        last_updated="2026-09-22T13:00:00+00:00",
    )


def scope():
    return {
        "account_name": "master_account",
        "connector_name": "kraken",
        "trading_pair": "BTC-USD",
        "controller_id": "spectra",
    }


def test_round_trip_preserves_exact_decimal_accounting(tmp_path):
    g = governor(tmp_path)

    snapshot_id = g.record_position_hold_snapshot(
        **scope(),
        holds=(hold(quote="15.125000000000000001"),),
        observed_at=1000.0,
    )

    rows = g.get_fresh_position_hold_snapshot(
        **scope(),
        now=1029.0,
    )

    assert snapshot_id > 0
    assert rows is not None
    assert len(rows) == 1
    assert rows[0].buy_amount_quote == D("15.125000000000000001")
    assert rows[0].executor_ids == ("exec-recovery-1",)


def test_snapshot_at_exact_ttl_boundary_is_fresh(tmp_path):
    g = governor(tmp_path)

    g.record_position_hold_snapshot(
        **scope(),
        holds=(hold(),),
        observed_at=1000.0,
    )

    assert g.get_fresh_position_hold_snapshot(
        **scope(),
        now=1030.0,
    ) is not None


def test_stale_snapshot_is_not_usable(tmp_path):
    g = governor(tmp_path)

    g.record_position_hold_snapshot(
        **scope(),
        holds=(hold(),),
        observed_at=1000.0,
    )

    assert g.get_fresh_position_hold_snapshot(
        **scope(),
        now=1030.001,
    ) is None


def test_future_dated_snapshot_is_not_usable(tmp_path):
    g = governor(tmp_path)

    g.record_position_hold_snapshot(
        **scope(),
        holds=(hold(),),
        observed_at=1001.0,
    )

    assert g.get_fresh_position_hold_snapshot(
        **scope(),
        now=1000.0,
    ) is None


def test_fresh_empty_snapshot_supersedes_old_nonempty(tmp_path):
    g = governor(tmp_path)

    g.record_position_hold_snapshot(
        **scope(),
        holds=(hold(),),
        observed_at=1000.0,
    )

    g.record_position_hold_snapshot(
        **scope(),
        holds=(),
        observed_at=1010.0,
    )

    rows = g.get_fresh_position_hold_snapshot(
        **scope(),
        now=1011.0,
    )

    assert rows == ()


def test_writer_rejects_cross_scope_hold(tmp_path):
    g = governor(tmp_path)

    with pytest.raises(
        ValueError,
        match="trading_pair .* does not match requested",
    ):
        g.record_position_hold_snapshot(
            **scope(),
            holds=(hold(pair="ETH-USD"),),
            observed_at=1000.0,
        )


def test_writer_rejects_multiple_active_rows(tmp_path):
    g = governor(tmp_path)

    with pytest.raises(
        ValueError,
        match="cannot contain multiple ACTIVE rows",
    ):
        g.record_position_hold_snapshot(
            **scope(),
            holds=(hold(), hold(quote="20")),
            observed_at=1000.0,
        )
