# Architecture

This page helps you find the module that owns a behaviour, and it follows
one run through the code from `plan` to `export`.

## Three programs

- `edr` is the controller. It runs on the head node under Python 3.11 or
  newer, uses the standard library plus `rich`, and lives in
  `src/edarunner/`.
- `edr_driver.py` is the driver, a single file for Python 3.6 or newer
  that uses only the standard library. Launch copies it to the state
  directory. It runs one run and never imports the package. The source is
  in `src/edarunner/driver/`, and [driver.md](driver.md) describes the
  protocol.
- `edr watch` is the controller running as a long-lived process on the
  head node, with the Telegram bot as a thread inside it.

No module knows an EDA tool. The project config names the commands, the
report files and the numbers in them.

## Modules

| Module | Owns |
|---|---|
| `model.py` | the dataclasses every module shares: `Project`, `Site`, `Stage`, `Metric`, `Task`, `Job`, `Batch`; nothing here reads a file |
| `config.py` | loading and validation of the four TOML files, the type check against the model, placeholders, hooks |
| `guards.py` | `assert_safe_target`, `assert_run_id` and `Refuse` |
| `home.py` | the user root `~/.edr` or `EDR_HOME`: the registry of the projects and the process locks |
| `runid.py` | the git calls, the source tag, the run id template |
| `checkout.py` | `edr checkout`: local clones, nested repositories, the dirty snapshot |
| `hosts.py` | the ssh wrapper with timeouts, the host probe and the census call, placement with the disk floor, the head-node check |
| `sync.py` | the rsync of the tree behind the guard, the driver copy by rename, the sync hook |
| `launch.py` | spec rendering, `plan`, `launch`, `stop` |
| `backend.py` | the `Backend` protocol: `submit`, `alive`, `stop`, `free`, `file_host`; `SshBackend` and `LocalBackend`; `[scheduler] backend` picks one of these or a scheduler backend |
| `schedulers.py` | the HTCondor, Slurm and LSF backends: the submit file, the state query, the stop |
| `driver/edr_driver.py` | one run on one host: stages, task groups, gates, budgets, retries, the heartbeat, the stop and keep files |
| `watch.py` | the cycle of one project: classify, act, collect, resume, launch queued, boards, `watch.json`; the watcher that holds `serve.lock` then calls `census.work` |
| `census.py` | the live runs of every registered project, the reservations, the census of the hosts, and the work of the user: orphans, the full-host stop, the lease sweep, the clock check; the host view of `edr hosts` |
| `collect.py` | the rsync of the collect paths into `data/results` |
| `metrics.py` | the five parsers with the hierarchical area report, extraction |
| `analysis.py` | the views over the database: area deltas, metrics per step, runtimes, host and run samples |
| `mlflow_export.py` | `edr export --mlflow`: the project database as a local MLflow tracking store; imports `mlflow` only when called |
| `db.py` | the project database: the SQLite schema, upserts, queries, `board.json` |
| `export.py` | the snapshot |
| `board.py` | the text boards, the rich tables and the plain text of one, `status.html`, `compare.html` |
| `notify/__init__.py` | the notifier interface, `make_notifiers`, and the plain text of an alert |
| `notify/ntfy.py` | `NtfyNotifier`: one JSON post per alert to an ntfy server |
| `notify/mail.py` | `MailNotifier`: one mail per alert through `smtplib` |
| `notify/digest.py` | `Digest`, the daily summary that the watcher sends and `/digest` shows |
| `notify/telegram/api.py` | `BotApi`, the HTTPS client: one method per Bot API call, the retry and the 429 wait |
| `notify/telegram/format.py` | pure functions that turn database rows into Telegram HTML |
| `notify/telegram/commands.py` | the built-in command table and one handler per command |
| `notify/telegram/custom.py` | the custom argv commands of `[telegram.commands.*]` |
| `notify/telegram/buttons.py` | the inline buttons of an alert, the action of a press, the confirmation of a stop |
| `notify/telegram/bot.py` | `TelegramBot`: the poll thread, the router, the allowlist, the alerts and the pinned board |
| `brief.py` | `edr brief`: the Markdown briefing of the project or of one run |
| `cli.py` | the commands, the exit codes, `--json`, the project lookup |
| `tools/gen_docs.py` | the pages under `docs/reference/`, from the parser, the model, `STATES`, the marks and the bot table |

A channel never imports `cli` or `watch`; it gets its commands through the
`Actions` object the CLI hands in.

## Data flow

![One run, from a commit to an analysis](../diagrams/run-lifecycle.svg)

1. `config.load_project` reads `edr.toml`, the site file and
   `tasks.toml`; `load_batch` reads one `jobs/<batch>.toml`.
2. `launch.plan` pins the date, computes the build tag and the run id,
   places each job on a host, and renders every string of the job into
   a spec, the JSON file the driver reads. A problem is recorded in the
   plan instead of raising an exception.
3. `launch.launch` publishes the driver, syncs the checked-out tree with
   `rsync --delete` behind the guard, writes the spec by rename, records
   the run and an event, and starts the driver through the backend,
   which returns the handle that `runs.handle` keeps. A job without a host is
   a `queued` row.
4. The driver writes the heartbeat by rename, the logs into the tree,
   and the task queue in the state directory.
5. `watch.cycle` ingests the heartbeats into `runs`, `stage_runs`,
   `step_runs` and `run_samples`, classifies, acts, collects into
   `data/results`, extracts metrics into `metrics` and `area`, writes
   `parameters`, resumes, launches queued rows, keeps the host probes in
   `host_samples`, and writes the boards.
6. `export.export` selects the newest run per label of one source tag
   from the database and copies its results with a manifest.

The read commands (`status`, `events`, `hosts`, `tools`, `metrics`, `compare`, `runtime`, `check`)
ingest the heartbeats too, so the board follows the driver and not the
last watcher cycle; that step is idempotent.

## The analysis tables

Four tables hold what the analysis views read. Each has one writer.

| Table | Writer | Key |
|---|---|---|
| `area` | `Database.add_metric`, from the `instances` of an `area_hier` metric row | run, stage, step, metric name, instance; a unique index maps a missing step to -1 |
| `step_runs` | `watch.ingest`, from the heartbeat's `step_times` | run, stage, step |
| `run_samples` | `watch.ingest`, one row per heartbeat `updated` | run, time |
| `host_samples` | `watch._boards`, one row per host that answered, 30 days kept | host, time |

`analysis.py` reads them and never writes. The source of every number
stays with it: `area` joins the metric row for its `source_file`, and a
runtime row names `stage_runs`, `step_runs` or the file and line of a
`step_log`.
