"""Fixtures for every test file."""

import pytest

try:
    from edarunner.notify.telegram import api
except ImportError:  # the driver job runs pytest with --no-project, so the package is not installed
    api = None


@pytest.fixture(autouse=True)
def no_telegram(monkeypatch):
    """No test reaches api.telegram.org; a test that needs HTTP patches urlopen itself."""
    if api is None:
        return

    def refuse(req, timeout):
        raise AssertionError(f"a test called {req.full_url}")

    monkeypatch.setattr(api, "urlopen", refuse)
