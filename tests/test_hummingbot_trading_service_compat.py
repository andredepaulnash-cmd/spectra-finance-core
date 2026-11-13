from __future__ import annotations

import pytest

from scripts.patch_hummingbot_trading_service_compat import MARKER, patch_source


def source_fixture() -> str:
    return '''        if trading_pair not in connector._trading_pairs:
            connector._trading_pairs.append(trading_pair)
            logger.debug(f"Registered {trading_pair} with connector {type(connector).__name__}")
'''


def test_paper_trade_registration_uses_public_pair_mapping():
    patched = patch_source(source_fixture())
    assert MARKER in patched
    assert 'getattr(connector, "trading_pair", None)' in patched
    assert "TradingPair(trading_pair, base_asset, quote_asset)" in patched
    assert "trading_pairs.append(trading_pair)" in patched


def test_other_connectors_keep_list_registration():
    patched = patch_source(source_fixture())
    assert "trading_pairs = getattr(connector, \"_trading_pairs\", None)" in patched
    assert "does not expose a supported " in patched
    assert "trading-pair registration interface" in patched


def test_patch_is_idempotent():
    once = patch_source(source_fixture())
    assert patch_source(once) == once


def test_patch_fails_closed_on_unrecognized_source():
    with pytest.raises(RuntimeError, match="registration block"):
        patch_source("def unrelated():\n    return None\n")
