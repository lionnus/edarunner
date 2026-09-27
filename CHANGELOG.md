# Changelog

## Unreleased

- The bot reacts to a command message: 👀 when a slow command starts,
  then 👍 when the reply went out or 👎 when the command failed.
- `/start` and `/keyboard` show a reply keyboard with `Status`, `Hosts`,
  `Events` and `Lic`; `/keyboard off` removes it.
- An alert has a third button, `stop`, which stops the run after its
  task. It asks `Stop <handle>?` first and acts only on `Yes, stop`
  within 10 minutes.
- The bot sends files: `/log <handle> [n]` the log tail of a run,
  `/board` the two HTML boards, and `/csv <design>` the metrics, each
  up to 20 MB. `/pin` now pins a new board message, which `/board` did
  before.
- No bot text uses a middle dot as a separator: the first line is
  `<project>: <title>`, and a run line, a host line and the counts
  separate their parts with commas.
- `edr notify TEXT [--silent] [--dry-run]` sends one message through
  every notifier, for example from a Claude Code hook.
- A command sent as a reply to an alert acts on the run of the alert:
  `/keep 24`, `/ack`, `/stop` and `/status` need no handle, and a custom
  command gets `{handle}`, `{run_id}`, `{run_root}` and `{host}`.
- `[telegram] topic_id` puts every message of a project into one topic
  of a forum group; the bot ignores a command from another topic.
- `/hosts` shows every resource as used of total, in one order:
  `cores 21/32, ram 93/376 GB, scratch 195/1538 GB, gpu 0/1`. `/lic`
  shows the seats used of the pool.
- `edr hosts` marks each resource from 🟢 to 🔴 by the thresholds of a
  new `[marks]` table in `site.toml`, which `edr.toml` may override. A
  new first column `ok` holds the worst mark, and the rows go by it.
  `--json` gives the marks of each host in `marks`.
- The `/hosts` reply of the bot puts the mark of each resource in front
  of it, and lists the hosts by their worst mark.

## 0.1.1 (2026-09-27)

- Every Telegram reply fits 40 columns and names the project in its first
  line: the board, `/status <run>`, `/events`, `/hosts`, `/lic`, `/help`
  and the alerts, which now carry the proposed command.
- A button press or a `/keep`, `/ack` or `/stop` records one event, with
  the actor `telegram`.
- `edr.toml` may carry a `[telegram]` table with `token_file`, `chat_id`
  and `user_id` that override the site's values, so a project can have
  its own bot and chat.
- `edr stage` points a nested clone at the upstream of the repository
  copy, not at the copy.

## 0.1.0 (2026-09-27)

The first release.

- `edr` draws its tables with `rich`, the one dependency of the
  controller: colour on a terminal, plain text in a pipe, under
  `NO_COLOR` and in the boards of the bot; `--json` is unchanged.
- `edr hosts` shows cores, RAM, scratch and GPUs as used or free of
  total, with a bar; the probe reports the totals and reads `nvidia-smi`
  when the host has it.
- `edr`, the controller, with seventeen verbs; `docs/cli.md` lists them.
- `edr_driver.py`, the one-file driver for Python 3.6 or newer: stages,
  task groups with a shared queue, licence gates, budgets, retries, the
  heartbeat, and the stop and keep files.
- `edr watch`: the classifier, collection, metric extraction, one resume
  of a dead run, the boards, and the Telegram bot.
- The SQLite ledger, `edr export` with a manifest, and
  `examples/local-demo` and `examples/openroad-gcd`.
- A project `[env]` table rendered per run, and the `{tree_id}`
  placeholder that a chain of reuse keeps.
- `edr import --results DIR` links the collected files of a run whose
  tree is gone and extracts its metrics; `--host` and `--root` are
  optional then, and `import` takes `--why`.
- `edr` works from any directory below `edr.toml`.
- A read verb (`status`, `events`, `hosts`, `lic`, `metrics`, `check`)
  never creates `data/edr.db`.
- `edr check` names every tool the head node lacks, and probes each host
  once for every batch.
- Config values are checked against their type at load; the error names
  the key path.
- Stages run in file order; the `after` key is gone, and a job's `stages`
  picks a subset.
- `netlist_stage` is an optional job field with no default.
- `edr launch` exits 0 when a run started or was queued, 2 when every job
  was already launched, 1 otherwise.
- `edr stop` marks a queued run `stopped`, refuses a run with no driver
  pid, waits at most 60 s, and exits 3 with `still alive; use --now`.
- `edr retire` refuses a live run with no heartbeat yet, a root a live
  run uses, and a root shared with an uncollected run.
- The safety guard refuses `/`, the home directory, a one-component path,
  a path without the marker, and a path shallower than `min_depth`.
- `edr export` skips `log/` and `*.log` unless `--with-logs`; `runs.csv`
  and the manifest carry `build_tag` and `src`.
- `metrics --design` and `export --design` match the source tag exactly.
- Metrics come only from stages and tasks that ended `done`.
- The board never downloads: `compare.html` uses
  `data/board/plotly.min.js` when present, else the CDN URL, and its
  tables work without Plotly. `edr watch --serve` is gone; serve
  `data/board` with `python -m http.server`.
- `edr watch --once` exits 1 when the cycle failed.
- `[telegram] user_id` limits the bot to one user of the chat.
- Remote commands run through `sh -c`, so a csh login shell works.
- A compute host needs a POSIX `sh`, `ssh`, `rsync`, procps-ng and its
  own `python3` 3.6 or newer; nothing is installed there.
- `edr retire --collect NAME,...` copies the named `collect_on_request`
  lists to the head node before the tree goes, and removes nothing when
  a copy failed. `retire --batch` also drops the staged tree that no
  other batch uses.
- `reuse = { ..., restore = "NAME" }` in a job starts on a fresh tree
  with the archived list of the reused run copied back, so a stage runs
  again after the tree was retired.
- `edr run` joins the batch of the run it continues; the driver is
  published once per version as `<state>/bin/edr_driver-<hash>.py`; the
  watcher keeps its side state in the ledger's `kv` table.
- The export manifest names the project.
- `telegram_poll = false` in `edr.toml` makes a project's watcher send
  alerts and the board without polling the bot, so several projects can
  share one token.
