import asyncio
import json

import pytest

from mcp_servers.hummingbot_api.tools import executor_create
from spectra_core.asset_authority import (
    AssetAuthorityError,
    canonical_kraken_usd_pair,
    load_approved_kraken_assets,
    require_approved_kraken_pair,
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
    assert "before market-data lookup" in result["formatted_output"]
