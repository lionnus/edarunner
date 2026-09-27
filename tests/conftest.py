"""Fixtures for every test file."""

import pytest

from edarunner.notify.telegram import api


@pytest.fixture(autouse=True)
def no_telegram(monkeypatch):
    """No test reaches api.telegram.org; a test that needs HTTP patches urlopen itself."""

    def refuse(req, timeout):
        raise AssertionError(f"a test called {req.full_url}")

    monkeypatch.setattr(api, "urlopen", refuse)
