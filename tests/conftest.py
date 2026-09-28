"""Fixtures for every test file."""

import shutil
from pathlib import Path

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


@pytest.fixture(autouse=True)
def user_root(tmp_path: Path, monkeypatch) -> Path:
    """Every test has its own user root, so no test reads or writes the registry, the leases or the locks of ~/.edr."""
    monkeypatch.setenv("EDR_HOME", str(tmp_path / ".edr"))
    monkeypatch.delenv("EDR_PROJECT", raising=False)
    return tmp_path / ".edr"


@pytest.fixture
def demo(tmp_path: Path, monkeypatch) -> Path:
    """The demo copied under tmp_path/edr (the safety marker), its scratch and HOME under tmp_path, cwd in the copy."""
    root = tmp_path / "edr" / "demo"
    shutil.copytree(Path(__file__).resolve().parents[1] / "examples" / "local-demo", root,
                    ignore=shutil.ignore_patterns("repo", "wt", "data"))
    (tmp_path / "scratch").mkdir()
    site = root / "site.toml"
    site.write_text(site.read_text().replace('"/tmp/edr-demo"', f'"{tmp_path / "scratch"}"'))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("EDR_BATCH", raising=False)
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """A watcher test environment; the import stays here, so the driver job needs no package."""
    from helpers_watch import Env

    monkeypatch.setenv("HOME", str(tmp_path))
    e = Env(tmp_path)
    yield e
    e.db.close()
