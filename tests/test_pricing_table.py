"""Pricing table: the rates themselves, and the ordering rule that makes lookup correct.

Rates observed 2026-09-03 01:57 KST from the official pricing docs (REQ-482/RES-482).
"""

from __future__ import annotations

import pytest

from ccmeter import report

# input, output, cache_read, cache_create (5-minute TTL)
CLAUDE_5_RATES = {
    "claude-fable-5-1": (10.00, 50.00, 0.25, 12.50),
    "claude-fable-5": (10.00, 50.00, 1.00, 12.50),
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25),
}


@pytest.mark.parametrize(("key", "rates"), CLAUDE_5_RATES.items())
def test_published_rates(key: str, rates: tuple[float, float, float, float]):
    entry = report.PRICING[key]
    got = (entry["input"], entry["output"], entry["cache_read"], entry["cache_create"])
    assert got == rates


def test_pricing_keys_are_not_shadowed():
    """A shorter key above a longer one wins every lookup the longer one wanted.

    Stated as a rule over the whole table rather than one assertion about the
    fable pair, so a future key cannot reintroduce the bug quietly.
    """
    keys = list(report.PRICING)
    for i, short in enumerate(keys):
        for long in keys[i + 1 :]:
            assert not long.startswith(short), f"{short!r} shadows {long!r}; put the longer key first"


def test_fable_5_1_is_not_priced_as_fable_5():
    """The concrete case: the two differ only in cache_read, so shadowing is silent."""
    assert report.pricing_for("claude-fable-5-1")["cache_read"] == 0.25
    assert report.pricing_for("claude-fable-5")["cache_read"] == 1.00


def test_dated_model_ids_resolve():
    """Real ids carry date suffixes; the table keys have to match as prefixes."""
    assert report.pricing_for("claude-haiku-4-5-20251001") is report.PRICING["claude-haiku-4-5"]


def test_our_models_no_longer_fall_back(capsys: pytest.CaptureFixture[str]):
    """The whole point: these used to be billed at Opus 4.6 rates and warn."""
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-fable-5-1"):
        assert report.pricing_for(model) is not report.FALLBACK_PRICING
    assert capsys.readouterr().err == ""


def test_sonnet_5_is_cheaper_than_opus_5():
    """Control: if both resolved to the same fallback this would fail, which is the
    exact failure the table was added to remove."""
    tokens = {"input": 1_000_000, "output": 1_000_000, "cache_read": 0, "cache_create": 0}
    assert report.cost_usd(tokens, "claude-sonnet-5") < report.cost_usd(tokens, "claude-opus-5")


def test_unknown_model_still_falls_back(capsys: pytest.CaptureFixture[str]):
    """Control: adding rates must not disable the warning for genuinely unknown models."""
    report._UNPRICED_WARNED.discard("claude-nonexistent-9")  # pyright: ignore[reportPrivateUsage]
    assert report.pricing_for("claude-nonexistent-9") is report.FALLBACK_PRICING
    assert "no published rates" in capsys.readouterr().err


def test_fallback_is_unchanged():
    assert report.FALLBACK_PRICING is report.PRICING["claude-opus-4-6"]
