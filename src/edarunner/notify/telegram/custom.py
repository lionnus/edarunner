"""The custom commands of `[telegram.commands.*]`: gate the arguments, render the argv, run it without a shell."""

from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

from edarunner.config import ConfigError, render
from edarunner.model import BotCommand

DETACH_WATCH_S = 5.0  # a detached command that ends this soon failed to start, as a rule


def run_custom(c: BotCommand, args: list[str], env: dict[str, str], log_dir: Path,
               event: Callable[[str, str], None]) -> tuple[str, bool]:
    """Run one entry with the placeholders `env`: (the reply, False for a refusal, a timeout or a failed exit)."""

    names = list(c.args)
    if names and len(args) >= len(names):
        values = args[: len(names) - 1] + [" ".join(args[len(names) - 1 :])]  # the last argument takes the rest
    else:
        values = args
    if len(values) != len(names):
        return f"usage: /{c.name} " + " ".join(f"<{n}>" for n in names), False
    env = dict(env)
    for n, v in zip(names, values):
        if not re.fullmatch(c.args[n], v):
            event("refused", f"/{c.name} {n}={v!r} does not match {c.args[n]}")
            return f"refused: {n} must match {c.args[n]}", False
        env[n] = v
    try:
        argv = [render(t, env) for t in c.run]
        cwd = render(c.cwd or "{root}", env)
        skip = [render(t, env) for t in c.skip_if or []]
        skip_reply = render(c.skip_reply or "skipped", env)
        done = render(c.reply, env)
    except ConfigError as e:
        return f"/{c.name}: {e}", False
    event("command", f"/{c.name} " + " ".join(f"{n}={v}" for n, v in zip(names, values)))
    if c.dry_run:
        return f"would run in {cwd}:\n{shlex.join(argv)}", True
    try:
        if skip and subprocess.run(skip, capture_output=True, cwd=cwd, timeout=c.timeout_s).returncode == 0:
            return skip_reply, True
        if c.detach:
            return _detach(argv, cwd, log_dir / f"telegram-{c.name}.log", done or "started")
        r = subprocess.run(argv, capture_output=True, text=True, errors="replace", cwd=cwd, timeout=c.timeout_s)
    except subprocess.TimeoutExpired:
        return f"/{c.name}: timed out after {c.timeout_s} s", False
    except OSError as e:
        return f"/{c.name}: {e}", False
    if r.returncode == 0 and done:
        return done, True
    out = (r.stdout + r.stderr).strip()
    return out or f"(no output, exit {r.returncode})", r.returncode == 0


def _detach(argv: list[str], cwd: str, logf: Path, started: str) -> tuple[str, bool]:
    """Start `argv` in its own session with its output in `logf`, and watch it for DETACH_WATCH_S.

    A process that ends in that window gives `ended with rc N: <its last output line>`.
    """
    logf.parent.mkdir(parents=True, exist_ok=True)
    with open(logf, "ab") as f:
        start = f.tell()
        p = subprocess.Popen(argv, cwd=cwd, start_new_session=True, stdin=subprocess.DEVNULL, stdout=f,
                             stderr=subprocess.STDOUT)
    try:
        rc = p.wait(timeout=DETACH_WATCH_S)
    except subprocess.TimeoutExpired:
        return f"{started} (pid {p.pid}, log {logf})", True
    with open(logf, "rb") as f:
        f.seek(start)
        lines = [ln for ln in f.read().decode(errors="replace").splitlines() if ln.strip()]
    return f"ended with rc {rc}: {lines[-1] if lines else '(no output)'}", rc == 0
