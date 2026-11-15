"""Both model pickers say what this machine cannot run, before the pick.

The dashboard's rows are covered in ``test_session_options_readiness``; this is
the Telegram half, which deliberately marks rather than refuses: choosing there
only sets a preference for the *next* session, so someone about to start their
local server is entitled to pick it first.
"""

import pytest

from condor.llm.readiness import INVALID, MISSING, READY, Readiness
from handlers.agents.menu import _settings_keyboard


@pytest.fixture
def states():
    return {
        "claude-acp": Readiness(READY, "installed and logged in"),
        "ollama": Readiness(MISSING, "not reachable — start it with `ollama serve`"),
        "openrouter": Readiness(INVALID, "OpenRouter rejected OPENROUTER_API_KEY (HTTP 401)"),
    }


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def test_a_provider_that_is_not_here_is_flagged_but_still_offered(states):
    labels = _labels(_settings_keyboard("claude-acp:sonnet", True, states))

    assert "⚠️ Ollama — Default Model" in labels, "the row warns"
    assert "• Claude (ACP) — Sonnet" in labels, "the pick is still bulleted"
    assert not any(
        lbl.startswith("⚠️") and "Claude" in lbl for lbl in labels
    ), "a runnable provider is never flagged"


def test_an_unprobed_menu_flags_nothing(states):
    """No probe is "we could not ask", never "nothing works"."""
    assert not any(lbl.startswith("⚠️") for lbl in _labels(_settings_keyboard("codex")))



def test_rejected_openrouter_key_is_warned_in_telegram_picker(states):
    labels = _labels(
        _settings_keyboard("claude-acp:sonnet", True, states)
    )
    assert "⚠️ OpenRouter — Pick Model" in labels
