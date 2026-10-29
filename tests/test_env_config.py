"""Env-var-driven app config — locks the accepted truthy / falsy values for
``C24_FLAT_STRUCTURE`` and ``C24_SEED_HISTORY`` so an accidental rename or a
parser tweak doesn't silently change behaviour."""

from __future__ import annotations

import pytest

from app import _flat_enabled, _seed_history_enabled


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "on", "  True  "])
def test_truthy_values_enable_flat(monkeypatch, value):
    monkeypatch.setenv("C24_FLAT_STRUCTURE", value)
    assert _flat_enabled() is True


@pytest.mark.parametrize("value", ["", "false", "False", "0", "no", "off", "anything"])
def test_falsy_values_keep_subfolders(monkeypatch, value):
    monkeypatch.setenv("C24_FLAT_STRUCTURE", value)
    assert _flat_enabled() is False


def test_unset_defaults_to_subfolders(monkeypatch):
    monkeypatch.delenv("C24_FLAT_STRUCTURE", raising=False)
    assert _flat_enabled() is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
def test_truthy_values_enable_seed_history(monkeypatch, value):
    monkeypatch.setenv("C24_SEED_HISTORY", value)
    assert _seed_history_enabled() is True


@pytest.mark.parametrize("value", ["", "false", "0", "anything"])
def test_falsy_values_keep_downloading(monkeypatch, value):
    monkeypatch.setenv("C24_SEED_HISTORY", value)
    assert _seed_history_enabled() is False


def test_unset_seed_history_downloads(monkeypatch):
    monkeypatch.delenv("C24_SEED_HISTORY", raising=False)
    assert _seed_history_enabled() is False
