"""The daily digest on a fixed ledger, and its place in the watcher cycle."""

from __future__ import annotations

import json
import time

from edarunner import watch
from edarunner.notify.digest import Digest
from edarunner.notify.telegram import format as fmt
from test_watch import NOW, Env, env, rid  # noqa: F401  (the fixture of test_watch)


def fill(e: Env) -> None:
    """Five runs, one of them before the last digest, a keep file with ack, and four host probes."""
    led = e.ledger
    led.set_kv("digest", {"day": "2027-01-14", "ts": NOW - 5 * 3600})
    alert = {"state": "hung", "msgs": {"hung": {"text": "no progress", "ids": ["1"]}}}
    led.set_kv("notified", {rid("h"): alert, rid("k"): alert, rid("c"): {"state": "running"}})
    rows = [("a", "done", None, NOW - 600), ("f", "FAILED:3", None, NOW - 2 * 86400),
            ("c", "stage:synth", "running", NOW - 60), ("h", "stage:pnr", "hung", NOW - 60),
            ("k", "stage:pnr", "hung", NOW - 60), ("q", None, "queued", None)]
    for label, phase, state, updated in rows:
        led.upsert_run({"run_id": rid(label), "batch": "demo", "label": label, "phase": phase, "state": state,
                        "stage": "synth" if phase else None, "started": NOW - 7200 if phase else None,
                        "updated": updated})
    (e.project.state / "demo").mkdir(parents=True)
    (e.project.state / "demo" / f"{rid('k')}.keep.json").write_text('{"hours": 0, "ack": true}')
    hosts = {"hostA": {"host": "hostA", "free_gb": 100.0, "total_gb": 1000.0},
             "hostB": {"host": "hostB", "free_gb": 900.0, "total_gb": 1000.0},
             "hostC": {"host": "hostC", "error": "timeout"},
             "hostD": {"host": "hostD", "free_gb": 500.0, "total_gb": 1000.0},
             "local": {"host": "local", "free_gb": 50.0, "total_gb": 100.0}}
    (e.project.data / "board").mkdir(parents=True)
    (e.project.data / "board" / "board.json").write_text(json.dumps({"hosts": hosts}))


def test_digest_text_on_a_fixed_ledger(env: Env) -> None:
    fill(env)
    since = time.strftime("%d.%m %H:%M", time.localtime(NOW - 5 * 3600))
    assert Digest(env.project, env.ledger).text(NOW) == "\n".join([
        f"<b>Ended since {since}</b>",
        "⚪ <code>a@demo</code> done",
        "",
        "<b>Live</b>",
        "🔴 <code>h@demo</code> synth, 2h",
        "🔴 <code>k@demo</code> synth, 2h",
        "🟢 <code>c@demo</code> synth, 2h",
        "",
        "<b>Queued</b>",
        "🔵 <code>q@demo</code>",
        "",
        "<b>Least free scratch</b>",
        "<b>local</b> scratch 50/100 GB",
        "<b>hostA</b> scratch 900/1000 GB",
        "<b>hostD</b> scratch 500/1000 GB",
        "",
        "<b>Open alerts</b>",
        "🔴 <code>h@demo</code> hung",
    ])


def test_an_empty_digest_says_none(env: Env) -> None:
    text = Digest(env.project, env.ledger).text(NOW)
    assert text.count("<i>none</i>") == 5 and fmt.plain(text).startswith("Ended since ")


def test_the_digest_is_due_once_a_day_from_its_hour(env: Env) -> None:
    d = Digest(env.project, env.ledger)
    at = time.strftime("%H:%M", time.localtime(NOW))
    assert not d.due(NOW)
    env.project.limits.digest_at = at
    assert d.due(NOW) and not d.due(NOW - 60)
    d.mark_sent(NOW)
    assert not d.due(NOW + 60) and d.due(NOW + 86400)
    assert env.ledger.get_kv("digest")["ts"] == NOW


def test_the_cycle_sends_the_digest_once(env: Env) -> None:
    posts: list[tuple[str, str]] = []
    env.notifier.post = lambda title, html, silent=False: posts.append((title, html)) or True
    env.project.limits.digest_at = time.strftime("%H:%M", time.localtime(NOW))
    watch.cycle(env.project, env.ssh, env.ledger, [env.notifier], now=NOW)
    watch.cycle(env.project, env.ssh, env.ledger, [env.notifier], now=NOW + 60)
    assert [t for t, _ in posts] == ["digest"] and posts[0][1].startswith("<b>Ended since ")
