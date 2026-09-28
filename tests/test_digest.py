"""The digest of every project on a fixed database, and when it is due."""

from __future__ import annotations

import os
import time
from dataclasses import replace

from helpers_watch import NOW, Env, rid

from edarunner.notify import digest


def fill(e: Env) -> None:
    """Six runs, one of them before the start of the digest, and a keep file that holds."""
    db = e.db
    alert = {"state": "hung", "msgs": {"hung": {"text": "no progress", "ids": ["1"]}}}
    db.set_store("notified", {rid("h"): alert, rid("k"): alert, rid("c"): {"state": "running"}})
    rows = [("a", "done", None, NOW - 600), ("f", "FAILED:3", None, NOW - 2 * 86400),
            ("c", "stage:synth", "running", NOW - 60), ("h", "stage:pnr", "hung", NOW - 60),
            ("k", "stage:pnr", "hung", NOW - 60), ("q", None, "queued", None)]
    for label, phase, state, updated in rows:
        db.upsert_run({"run_id": rid(label), "batch": "demo", "label": label, "phase": phase, "state": state,
                        "stage": "synth" if phase else None, "started": NOW - 7200 if phase else None,
                        "updated": updated})
    (e.project.state_dir / "demo").mkdir(parents=True)
    keep = e.project.state_dir / "demo" / f"{rid('k')}.keep.json"
    keep.write_text('{"hours": 1}')
    os.utime(keep, (NOW, NOW))  # a keep that holds closes the alert of its run


def test_the_digest_of_every_project_on_a_fixed_database(env: Env, tmp_path) -> None:
    fill(env)
    start = NOW - 5 * 3600
    quiet = replace(env.project, project="quiet", data=tmp_path / "quiet")
    broken = replace(env.project, project="broken", data=tmp_path / "broken")
    broken.data.mkdir()
    (broken.data / "edr.db").write_text("not a database")
    assert digest.text({"demo": env.project, "quiet": quiet, "broken": broken}, start, NOW).splitlines() == [
        f"<i>since {time.strftime('%d.%m %H:%M', time.localtime(start))}</i>",
        "",
        "<b>broken</b> <i>no digest: file is not a database</i>",
        "",
        "<b>demo</b>",
        "<i>ended</i>",
        "⚪ <code>a@demo</code> done",
        "<i>live</i>",
        "🔴 <code>h@demo</code> synth, 2h",
        "🔴 <code>k@demo</code> synth, 2h",
        "🟢 <code>c@demo</code> synth, 2h",
        "<i>queued</i>",
        "🔵 <code>q@demo</code>",
        "<i>open alerts</i>",
        "🔴 <code>h@demo</code> hung",
        "",
        "<b>quiet</b> <i>nothing ended, nothing live</i>",
    ]
    assert not quiet.data.exists()  # the digest opens no database that is not there


def test_the_digest_is_due_once_a_day_from_its_hour() -> None:
    at = time.strftime("%H:%M", time.localtime(NOW))
    assert not digest.due("", {}, NOW)
    assert digest.due(at, {}, NOW) and not digest.due(at, {}, NOW - 60)
    sent = {"day": time.strftime("%Y-%m-%d", time.localtime(NOW)), "ts": NOW}
    assert not digest.due(at, sent, NOW + 60) and digest.due(at, sent, NOW + 86400)
    assert digest.since(sent, NOW + 60) == NOW and digest.since({}, NOW) == NOW - digest.DAY_S
