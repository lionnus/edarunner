# Changelog

## Unreleased

- The backends `condor`, `slurm` and `lsf` hand a run to a batch
  scheduler. `[scheduler]` in `site.toml` takes `backend`, `submit_via`,
  `tree_root`, `max_jobs`, `queue` and `options`; `scheduler.backend`
  keeps its meaning. The driver is the job; the scheduler picks the
  host, and the run tree lies under `tree_root`. New keys
  `tools.<name>.licence` (a scheduler licence or concurrency limit for
  the whole job) and `needs.ram_gb`. The watcher asks the scheduler once
  per cycle for every job, and three new states come from it:
  `pending`, `held` (alerts) and `suspended`. `edr check` asks HTCondor
  or Slurm whether each licence exists. CI runs the local demo on an
  HTCondor pool and on a Slurm cluster in containers;
  `tools/harness/` holds the scripts. LSF has rendering and contract
  tests only.
- `edr notify --board` and `edr notify --digest` send the board or the
  daily digest through every channel, so a cron line can mail either.
  TEXT is now optional; give exactly one of TEXT, `--board` and
  `--digest`.
- An ntfy alert carries the keep, ack and stop commands as three copy
  buttons, next to the command lines. A server that refuses the buttons
  with a 400 gets the push again without them.
- docs/notify.md lists every message kind and what each channel gets.

## 0.3.0 (2026-09-27)

- The database uses the DELETE journal with `synchronous=FULL` when
  `data/` is on NFS, SMB, 9p or FUSE, and WAL elsewhere. WAL does not
  work on a network filesystem. `edr check` prints a warning line with
  the path and the mode, and `Database.journal_mode` holds the mode.
- The tool gate leases its seats. Two drivers that read the same free
  seat from the probe both started before, and a licence error hid the
  race. Now the driver writes one lease file per seat in
  `<state_dir>/leases/<tool>/` by a rename, and counts the free seats
  less the seats other runs leased in the last `lease_s` (new limit,
  default 600 s). The later of two drivers backs off. The driver removes
  its leases when the stage or task ends; the watcher sweeps the leases
  of dead, finished and retired runs and those older than the stage
  budget, with a `lease` event. The heartbeat `gate` and `edr status
  <handle>` read `<tool>: <free> free, <held> held by others, <needed>
  needed`.
- The canonical metric names follow METRICS2.1, the schema of the
  OpenROAD metrics JSON, without the stage prefix: `design__instance__area`,
  `timing__setup__ws`, `timing__setup__tns`, `power__total` and more. The
  examples and the template use them. Rename a `canonical` value in
  `edr.toml` to keep a board or a script that filters on it.
  `compare.html` stacks the `power__*` parts instead of the `power.*`
  phases.
- A metric with `area_hier = <depth>` parses a Synopsys or an OpenROAD
  hierarchical area report into the new `area` table.
  `edr metrics --instance` and `--depth` print its rows, and
  `edr compare A B --area --depth N` prints the area per block of two or
  more runs with the delta to the first.
- `edr compare A B` puts every metric per stage and step side by side,
  with the delta and the percent to the first run. `edr metrics --run X
  --over steps` prints one run along its steps. `edr metrics` takes
  `--run` and `--metric`; `--design` is no longer required with `--run`.
- `edr runtime` prints the time of each stage attempt, step and task
  group of a run, or one row per run of a batch. The driver records when
  it first sees each step (`step_times`, the `step_runs` table), and a
  stage's `step_log` reads step start times from a collected log.
- The watcher keeps each cycle's host probes in `host_samples` for 30
  days. `edr hosts --history` prints them, and `status.html` draws the
  cores and RAM in use per host over the last day.
- The driver samples the CPU and RSS of its process groups at every
  heartbeat and runs `du` at most once per ten minutes. The watcher
  keeps each heartbeat in `run_samples`, and `edr status <handle>` shows
  them.
- `compare.html` has a filter per column of the runs table, the step
  names on the metric-over-steps chart, the area delta of two runs, and
  parallel coordinates over every shown run with one chosen metric.
- The export manifest gives each run a `record`: host, start and end,
  the edarunner version, the driver's sha256, the tool versions and the
  stage times. The spec carries the same `record`.
- `edr export --mlflow DIR` writes the run database into a local MLflow
  tracking store, one MLflow run per run. It needs the new `mlflow`
  extra.
- Every generated reference page starts with its title as the H1.
- The database table `kv` is now `store`, and `Database.get_kv` and
  `set_kv` are `get_store` and `set_store`. An existing `data/edr.db`
  migrates on the first open.
- The database table `params` is now `parameters`, and
  `Database.set_params` is `set_parameters`. An existing `data/edr.db`
  migrates on the first open. In `compare.html` the data block
  `edr-params` is now `edr-parameters`. The export columns do not change.
- Breaking: `edr run` is now `edr continue`, so the word run means a
  run of the flow only. `run` still answers for one release, with a
  deprecation line on stderr. Replace `edr run` with `edr continue` in
  scripts and hooks. Its event kind is now `continue`.
- Breaking: the `edr.toml` key `state` is now `state_dir`. An old key
  stops the load with `'state' is now 'state_dir'`. Rename the key in
  `edr.toml`: `state = "..."` becomes `state_dir = "..."`, with the same
  value.

- Breaking: the metric parser `expr` is gone; the `python` hook does
  the same. A metric with `expr` stops the load with the hook form.
  Migrate: `expr = "power_w * window_ns"` becomes `stage`, `file` and
  `python = "hooks/<name>.py:<name>"`, where `<name>(path)` reads the
  input files and returns the value.
  `examples/local-demo/hooks/energy.py` shows the energy hook.
- Two more notifiers next to Telegram: `[ntfy]` posts one push per
  alert with a priority by kind, and `[mail]` sends one mail per alert
  through SMTP. Both are stdlib only, and `edr notify` reaches every
  channel. `docs/notify.md` shows the setup.
- A job needs no `config`. Without it the build tag hook gets `""`, and
  the run id drops the empty part with its `_`.
- Breaking: the job key `netlist_stage` is gone. A job table `vars`
  holds it and any other value, as `{vars.<name>}` in the stage strings,
  `[env]` and `collect`. An old key or `{netlist_stage}` stops the load.
  Migrate: in `jobs/*.toml`, `netlist_stage = 11` becomes
  `vars = { netlist_stage = 11 }`; in `edr.toml`, `{netlist_stage}`
  becomes `{vars.netlist_stage}`.

## 0.2.0 (2026-09-27)

- The SQLite ledger is now the run database: `ledger.py` is `db.py`,
  `Ledger` is `Database`, and every page says database. The file stays
  `data/edr.db`; no config key changes.
- Breaking: `edr stage` is now `edr checkout`, so the word stage means
  a stage of the flow only. `stage` still answers for one release, with
  a deprecation line on stderr. The module `stagectl.py` is
  `checkout.py`.
- A daily digest at `[limits] digest_at`: the runs that ended, the live
  and queued runs, the hosts with the least free scratch, and the open
  alerts. `/digest` and `edr status --digest` show it on demand.
- A custom command with `detach` that ends within 5 seconds replies
  `ended with rc N: <last output line>` instead of its `reply`.
- The bot reacts to a command message: 👀 when a slow command starts,
  then 👍 when the reply went out or 👎 when the command failed.
- `/start` and `/keyboard` show a reply keyboard with `Status`, `Hosts`,
  `Events`, `Tools` and `Digest`; `/keyboard off` removes it.
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
  `cores 21/32, ram 93/376 GB, scratch 195/1538 GB, gpu 0/1`. `/tools`
  shows the seats used of the total and the hosts of each tool.
- `docs/reference/` is generated from the code by `tools/gen_docs.py`:
  every verb with its flags and exit codes, every config key and
  placeholder, every run state, the bot commands. `docs/cli.md` and
  `docs/configuration.md` are gone. CI refuses a stale page.
- Breaking: the site table `[licences.<name>]` is now `[tools.<name>]`,
  with `seats` and a `probe` argv that prints `free` or `free total`.
  The stage and task key `needs.licence` is now `needs.tools`: a list of
  names, or `{ name = seats }`. An old key stops the load with an error
  that names the new one. The core no longer reads `lmstat` output;
  `examples/site/hooks/flexlm_free.sh` does that as a site hook.
- `[hosts.<name>] tools` lists the tools a host has, with an optional
  version. A stage reads the version as `{tool.<name>.version}`.
  Placement skips a host that lacks a tool of the job, and `plan` names
  the missing tool.
- `edr tools` replaces `edr lic`: free and total seats per tool, and the
  hosts that have it. `lic` and `/lic` still answer for one release. The
  heartbeat field `licence_unknown` is gone; `gate` says why a run waits.
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
