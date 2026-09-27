"""Fixtures for every test file."""

import pytest

try:
    from edarunner.notify.telegram import api
except ImportError:  # the driver job runs the driver tests on Python 3.6 without the package
    api = None


@pytest.fixture(autouse=True)
def no_telegram(monkeypatch):
    """No test reaches api.telegram.org; a test that needs HTTP patches urlopen itself."""
    if api is None:
        return

    def refuse(req, timeout):
        raise AssertionError(f"a test called {req.full_url}")

    monkeypatch.setattr(api, "urlopen", refuse)
