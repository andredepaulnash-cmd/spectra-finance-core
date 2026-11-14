from pathlib import Path

import yaml

from condor.agents.config import AgentConfig


def test_spectra_kraken_market_intelligence_strategy_is_read_only():
    path = (
        Path(__file__).resolve().parents[1]
        / "agents/spectra/strategies/kraken_spot_market_intelligence/strategy.md"
    )
    _, frontmatter, _ = path.read_text(encoding="utf-8").split("---", 2)
    metadata = yaml.safe_load(frontmatter)
    config = AgentConfig.from_dict(metadata["default_config"])

    assert config.market_connector_name == "kraken_paper_trade"
    assert config.market_trading_pair == "BTC-USD"
    assert config.execution_mode == "dry_run"
    assert config.total_amount_quote == 0
    assert config.risk_limits.max_position_size_quote == 0
    assert config.risk_limits.max_open_executors == 0


def test_market_intelligence_cache_ttl_is_a_validated_agent_setting():
    assert AgentConfig().market_data_cache_ttl_seconds == 10.0
    configured = AgentConfig.from_dict({"market_data_cache_ttl_seconds": 4})
    assert configured.market_data_cache_ttl_seconds == 4.0
