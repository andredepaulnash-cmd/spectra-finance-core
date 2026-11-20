import asyncio
import json

import pytest

from mcp_servers.hummingbot_api.tools import executor_create
from spectra_core.asset_authority import (
    AssetAuthorityError,
    canonical_kraken_usd_pair,
    load_approved_kraken_assets,
    require_approved_kraken_pair,
    write_discovered_kraken_assets,
)


def write_policy(path, pairs, *, source="Architect Governor allowlist", connector="kraken_paper_trade"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": source,
                "connector": connector,
                "approved_pairs": pairs,
            }
        ),
        encoding="utf-8",
    )


def test_missing_governor_asset_list_is_not_allow_all(monkeypatch, tmp_path):
    state = tmp_path / "paper-state"
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false")
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    monkeypatch.setenv("SPECTRA_PAPER_STATE_DIR", str(state))
    policy = load_approved_kraken_assets()
    assert not policy.configured
    assert policy.pairs == frozenset()
    with pytest.raises(AssetAuthorityError, match="list is not configured"):
        require_approved_kraken_pair("BTC-USD")


def test_explicit_source_attributed_pair_is_authorized(monkeypatch, tmp_path):
    state = tmp_path / "paper-state"
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false")
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    monkeypatch.setenv("SPECTRA_PAPER_STATE_DIR", str(state))
    write_policy(state / "approved_assets.json", ["XBT-USD", "ETH-USD"])

    policy = require_approved_kraken_pair("BTC-USD")

    assert policy.configured
    assert policy.source == "Architect Governor allowlist"
    assert policy.pairs == frozenset({"BTC-USD", "ETH-USD"})


def test_unapproved_pair_and_wrong_connector_fail_closed(monkeypatch, tmp_path):
    state = tmp_path / "paper-state"
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false")
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    monkeypatch.setenv("SPECTRA_PAPER_STATE_DIR", str(state))
    write_policy(state / "approved_assets.json", ["BTC-USD"])

    with pytest.raises(AssetAuthorityError, match="not in the Governor-approved"):
        require_approved_kraken_pair("SOL-USD")
    with pytest.raises(AssetAuthorityError, match="only permits kraken_paper_trade"):
        require_approved_kraken_pair("BTC-USD", connector_name="kraken")


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"schema_version": 2, "source": "x", "connector": "kraken_paper_trade", "approved_pairs": ["BTC-USD"]}, "schema_version"),
        ({"schema_version": 1, "source": "", "connector": "kraken_paper_trade", "approved_pairs": ["BTC-USD"]}, "source"),
        ({"schema_version": 1, "source": "x", "connector": "kraken", "approved_pairs": ["BTC-USD"]}, "connector"),
        ({"schema_version": 1, "source": "x", "connector": "kraken_paper_trade", "approved_pairs": ["BTC-USDT"]}, "USD spot pair"),
        ({"schema_version": 1, "source": "x", "connector": "kraken_paper_trade", "approved_pairs": ["BTC-USD", "XBT-USD"]}, "duplicate"),
    ],
)
def test_malformed_or_ambiguous_allowlist_fails_closed(monkeypatch, tmp_path, payload, message):
    state = tmp_path / "paper-state"
    state.mkdir()
    (state / "approved_assets.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false")
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    monkeypatch.setenv("SPECTRA_PAPER_STATE_DIR", str(state))
    with pytest.raises(AssetAuthorityError, match=message):
        load_approved_kraken_assets()


def test_pair_normalization_is_strict():
    assert canonical_kraken_usd_pair("xbt-usd") == "BTC-USD"
    with pytest.raises(AssetAuthorityError):
        canonical_kraken_usd_pair("BTC-USDT")


def test_unapproved_paper_pair_is_refused_before_portfolio_read(monkeypatch, tmp_path):
    from spectra_core.kraken_authorization import reserve_kraken_capital

    state = tmp_path / "paper-state"
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false")
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    monkeypatch.setenv("SPECTRA_PAPER_STATE_DIR", str(state))

    class NoReadClient:
        @property
        def portfolio(self):
            raise AssertionError("portfolio must not be read before asset approval")

    with pytest.raises(AssetAuthorityError, match="list is not configured"):
        asyncio.run(
            reserve_kraken_capital(
                NoReadClient(),
                account_name="master_account",
                trading_pair="SOL-USD",
                intent="ENTRY",
                amount_quote="25",
            )
        )


def test_final_executor_boundary_refuses_unapproved_pair_before_market_data(monkeypatch, tmp_path):
    state = tmp_path / "paper-state"
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false")
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    monkeypatch.setenv("SPECTRA_PAPER_STATE_DIR", str(state))
    monkeypatch.setattr(executor_create.settings, "paper_only", True)
    monkeypatch.setattr(executor_create.settings, "agent_slug", "spectra")

    result = asyncio.run(
        executor_create.create_executor(
            object(),
            "grid_executor",
            {
                "connector_name": "kraken_paper_trade",
                "trading_pair": "SOL-USD",
                "side": 1,
                "total_amount_quote": 25,
            },
            account_name="master_account",
            controller_id="spectra",
            spectra_proposal_id="unapproved-entry",
            spectra_intent="ENTRY",
        )
    )

    assert "not configured" in result["error"]
    assert "no market data or capital was requested" in result["formatted_output"]


def test_symlinked_governor_allowlist_is_rejected(monkeypatch, tmp_path):
    state = tmp_path / "paper-state"
    state.mkdir()
    real = tmp_path / "real-approved-assets.json"
    write_policy(real, ["BTC-USD"])
    link = state / "approved_assets.json"
    link.symlink_to(real)
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false")
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    monkeypatch.setenv("SPECTRA_PAPER_STATE_DIR", str(state))

    with pytest.raises(AssetAuthorityError, match="must not be a symlink"):
        load_approved_kraken_assets()


def _scan(as_of=1000.0):
    return {
        "paper_only": True,
        "orders_created": 0,
        "source": "Kraken spot market data via Hummingbot API",
        "as_of": as_of,
        "lists": {
            "mean_reversion_candidates": [
                {
                    "trading_pair": "XBT-USD",
                    "quote_asset": "USD",
                    "eligible": True,
                    "capital_authorized": False,
                    "current_price": 100.0,
                    "volume_24h_usd": 500000.0,
                    "selection_score": 92.0,
                },
                {
                    "trading_pair": "ETH-USD",
                    "quote_asset": "USD",
                    "eligible": True,
                    "capital_authorized": False,
                    "current_price": 2000.0,
                    "volume_24h_usd": 400000.0,
                    "selection_score": 87.0,
                },
                {
                    "trading_pair": "SOL-USD",
                    "quote_asset": "USD",
                    "eligible": False,
                    "capital_authorized": False,
                    "current_price": 100.0,
                    "volume_24h_usd": 300000.0,
                    "selection_score": 80.0,
                },
                {
                    "trading_pair": "BTC-USDT",
                    "quote_asset": "USDT",
                    "eligible": True,
                    "capital_authorized": False,
                    "current_price": 100.0,
                    "volume_24h_usd": 300000.0,
                    "selection_score": 79.0,
                },
            ]
        },
    }


def test_fresh_discovery_shortlist_authorizes_only_eligible_usd_pairs(monkeypatch, tmp_path):
    state = tmp_path / "paper-state"
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false")
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    monkeypatch.setenv("SPECTRA_PAPER_STATE_DIR", str(state))
    monkeypatch.delenv("SPECTRA_APPROVED_ASSETS_FILE", raising=False)
    monkeypatch.setenv("SPECTRA_AUTO_ASSET_SELECTION", "true")

    written = write_discovered_kraken_assets(_scan(), now=1000.0)
    loaded = load_approved_kraken_assets(now=1000.0)

    assert written.automatic is True
    assert loaded.configured is True
    assert loaded.automatic is True
    assert loaded.pairs == frozenset({"BTC-USD", "ETH-USD"})
    monkeypatch.setattr("spectra_core.asset_authority.time.time", lambda: 1000.0)
    assert require_approved_kraken_pair("BTC-USD").automatic is True
    with pytest.raises(AssetAuthorityError, match="not in the Governor-approved"):
        require_approved_kraken_pair("SOL-USD")


def test_manual_allowlist_always_overrides_automatic_discovery(monkeypatch, tmp_path):
    state = tmp_path / "paper-state"
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false")
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    monkeypatch.setenv("SPECTRA_PAPER_STATE_DIR", str(state))
    monkeypatch.delenv("SPECTRA_APPROVED_ASSETS_FILE", raising=False)
    write_discovered_kraken_assets(_scan(), now=1000.0)
    write_policy(state / "approved_assets.json", ["BTC-USD"], source="Operator allowlist")

    policy = load_approved_kraken_assets(now=1000.0)

    assert policy.automatic is False
    assert policy.source == "Operator allowlist"
    assert policy.pairs == frozenset({"BTC-USD"})


def test_stale_automatic_discovery_list_fails_closed(monkeypatch, tmp_path):
    state = tmp_path / "paper-state"
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false")
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    monkeypatch.setenv("SPECTRA_PAPER_STATE_DIR", str(state))
    monkeypatch.delenv("SPECTRA_APPROVED_ASSETS_FILE", raising=False)
    write_discovered_kraken_assets(_scan(), now=1000.0)

    policy = load_approved_kraken_assets(now=1301.0)

    assert policy.configured is False
    assert policy.reason == "automatic_asset_list_stale"
    assert policy.pairs == frozenset()


def test_auto_asset_selection_can_be_disabled(monkeypatch, tmp_path):
    state = tmp_path / "paper-state"
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false")
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    monkeypatch.setenv("SPECTRA_PAPER_STATE_DIR", str(state))
    monkeypatch.delenv("SPECTRA_APPROVED_ASSETS_FILE", raising=False)
    write_discovered_kraken_assets(_scan(), now=1000.0)
    monkeypatch.setenv("SPECTRA_AUTO_ASSET_SELECTION", "false")

    policy = load_approved_kraken_assets(now=1000.0)

    assert policy.configured is False
    assert policy.reason == "automatic_asset_selection_disabled"


def test_automatic_discovery_rejects_stale_or_mutating_scan(monkeypatch, tmp_path):
    state = tmp_path / "paper-state"
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false")
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    monkeypatch.setenv("SPECTRA_PAPER_STATE_DIR", str(state))

    with pytest.raises(AssetAuthorityError, match="stale"):
        write_discovered_kraken_assets(_scan(as_of=600.0), now=1000.0)

    unsafe = _scan()
    unsafe["orders_created"] = 1
    with pytest.raises(AssetAuthorityError, match="research-only"):
        write_discovered_kraken_assets(unsafe, now=1000.0)


def test_top_loser_reversal_candidates_join_paper_allowlist_only_after_all_entry_guards(monkeypatch, tmp_path):
    state = tmp_path / "paper-state"
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false")
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    monkeypatch.setenv("SPECTRA_PAPER_STATE_DIR", str(state))
    monkeypatch.delenv("SPECTRA_APPROVED_ASSETS_FILE", raising=False)
    monkeypatch.setenv("SPECTRA_AUTO_ASSET_SELECTION", "true")
    scan = _scan()
    scan["criteria"] = {"minimum_24h_volume_quote": 10_000_000}
    scan["lists"]["top_loser_reversal_candidates"] = [
        {
            "trading_pair": "XRP-USD", "quote_asset": "USD",
            "eligible": True, "capital_authorized": False,
            "current_price": 0.60, "volume_24h_usd": 15_000_000,
            "selection_score": 70.0, "reversal_pathway_eligible": True,
            "entry_liquidity_eligible": True, "change_24h_pct": -4.0,
            "reversal_signal_count": 2,
        },
        {
            "trading_pair": "THIN-USD", "quote_asset": "USD",
            "eligible": True, "capital_authorized": False,
            "current_price": 1.0, "volume_24h_usd": 9_000_000,
            "selection_score": 70.0, "reversal_pathway_eligible": True,
            "entry_liquidity_eligible": False, "change_24h_pct": -4.0,
            "reversal_signal_count": 3,
        },
    ]
    policy = write_discovered_kraken_assets(scan, now=1000.0)
    assert "XRP-USD" in policy.pairs
    assert "THIN-USD" not in policy.pairs
