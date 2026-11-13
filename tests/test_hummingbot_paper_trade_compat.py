from __future__ import annotations

import pytest

from scripts.patch_hummingbot_paper_trade_compat import patch_source


def source_fixture() -> str:
    return """    async def ensure_data_connector_started(self, connector_name, trading_pair):
        try:
            # Add trading pair before starting network
            if trading_pair not in connector._trading_pairs:
                connector._trading_pairs.append(trading_pair)
            await connector.start_network()
        except Exception:
            self._purge_trading_pair_registration(connector, trading_pair)

    def _purge_trading_pair_registration(self, connector, trading_pair):
        targets = [getattr(connector, "_trading_pairs", None)]
"""


def test_paper_trade_pair_registration_uses_public_pair_mapping():
    patched = patch_source(source_fixture())

    assert "SPECTRA_PAPER_TRADE_DATA_CONNECTOR_PAIR_MAP_COMPAT" in patched
    assert 'getattr(connector, "trading_pair", None)' in patched
    assert "TradingPair(trading_pair, base_asset, quote_asset)" in patched
    assert "trading_pairs.append(trading_pair)" in patched


def test_paper_trade_pair_cleanup_uses_public_pair_mapping():
    patched = patch_source(source_fixture())

    assert "SPECTRA_PAPER_TRADE_DATA_CONNECTOR_PAIR_MAP_CLEANUP_COMPAT" in patched
    assert "paper_pair_map.pop(trading_pair, None)" in patched


def test_paper_trade_compatibility_patcher_is_idempotent():
    once = patch_source(source_fixture())
    twice = patch_source(once)

    assert twice == once


def test_paper_trade_compatibility_patcher_fails_closed_on_source_drift():
    with pytest.raises(RuntimeError, match="registration block"):
        patch_source('    if trading_pair in connector._trading_pairs:\n')
