"""tools/gen_docs.py: every page is written, and every command, config table and state appears."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from edarunner import watch
from edarunner.notify.telegram import format as fmt

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("gen_docs", ROOT / "tools" / "gen_docs.py")
gen = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(gen)  # type: ignore[union-attr]


def test_pages_cover_every_verb_table_and_state(tmp_path: Path) -> None:
    gen.write(tmp_path)
    pages = {p.name: p.read_text() for p in tmp_path.glob("*.md")}
    assert set(pages) == set(gen.pages()) and all(pages.values())
    for command in gen.commands():
        assert f"\n## {command}\n" in pages["cli.md"]
    for heading, _, _ in gen.CONFIG:
        assert f"\n{heading}\n" in pages["configuration.md"]
    for state in watch.STATES:
        assert f"`{state}`" in pages["states.md"]
    assert set(watch.STATES) <= set(fmt.MARK)
    assert " · " not in "".join(pages.values())


def test_check_reports_a_stale_page(tmp_path: Path, capsys) -> None:
    gen.write(tmp_path)
    (tmp_path / "cli.md").write_text("old\n")
    assert gen.check(tmp_path) == 1
    out = capsys.readouterr()
    assert "cli.md (committed)" in out.out and "stale: cli.md" in out.err
    gen.write(tmp_path)
    assert gen.check(tmp_path) == 0
