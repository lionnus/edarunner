"""cli.py: main, Ctx and check on a copy of examples/local-demo; the verbs are in test_cli.py."""

from __future__ import annotations

import json

from edarunner import cli
from test_cli import demo, edr  # noqa: F401 - the fixture and the runner of test_cli


def test_main_reports_an_unhandled_exception(capsys, caplog, monkeypatch) -> None:
    def boom(c, a):
        raise KeyError("runs: unknown columns ['x']")

    monkeypatch.setattr(cli, "cmd_hosts", boom)
    code, out, err = edr(capsys, "--json", "hosts")
    assert code == 1 and json.loads(out)["code"] == 1
    assert "edr: \"runs: unknown columns ['x']\"" in err
    assert caplog.records[-1].exc_info is not None
