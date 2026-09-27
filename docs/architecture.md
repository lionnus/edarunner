# Architecture

This page is for a contributor. It names the modules, follows the data
from `plan` to `export`, and lists the rules every change keeps.

## Three programs

- `edr`, the controller: Python 3.11 or newer, the standard library plus
  `rich`, on the head node. `src/edarunner/`.
- `edr_driver.py`, the driver: one file, Python 3.6 or newer, standard
  library only, copied to the state directory at launch. It runs one run
  and never imports the package. `src/edarunner/driver/`.
- `edr watch`, the controller as a long-running process on the head
  node, with the Telegram bot as a thread inside it.

Nothing in the core knows an EDA tool. The project config names the
commands, the report files and the numbers in them.

## Modules

| Module | Owns |
|---|---|
| `model.py` | the dataclasses every module shares: `Project`, `Site`, `Stage`, `Metric`, `Task`, `Job`, `Batch`; nothing here reads a file |
| `config.py` | loading and validation of the four TOML files, the type check against the model, placeholders, hooks |
| `guards.py` | `assert_safe_target`, `assert_run_id` and `Refuse` |
| `runid.py` | the git calls, the source tag, the run id template |
| `stagectl.py` | `edr stage`: worktrees, nested repositories, the dirty snapshot |
| `hosts.py` | the ssh wrapper with timeouts, the host probe, placement, the head-node check |
| `sync.py` | the rsync of the tree behind the guard, the driver copy by rename, the sync hook |
| `launch.py` | spec rendering, `plan`, `launch`, `stop` |
| `driver/edr_driver.py` | one run on one host: stages, task groups, gates, budgets, retries, the heartbeat, the stop and keep files |
| `watch.py` | the cycle: classify, act, collect, resume, launch queued, boards, `watch.json` |
| `collect.py` | the rsync of the collect paths into `data/results` |
| `metrics.py` | the four parsers, `expr`, extraction, the FlexLM line |
| `ledger.py` | the SQLite schema, upserts, queries, `board.json` |
| `export.py` | the snapshot |
| `board.py` | the text boards, the rich tables and the plain text of one, `status.html`, `compare.html` |
| `notify/__init__.py`, `notify/telegram.py` | the notifier interface and the bot |
| `cli.py` | the verbs, the exit codes, `--json`, the project lookup |

A channel never imports `cli` or `watch`; it gets its verbs through the
`Actions` object the CLI hands in.

## Data flow

1. `config.load_project` reads `edr.toml`, the site file and
   `tasks.toml`; `load_batch` reads one `jobs/<batch>.toml`.
2. `launch.plan` pins the date, computes the build tag and the run id,
   places each job on a host, and renders every string of the job into
   a spec. A problem lands in the plan, not in an exception.
3. `launch.launch` publishes the driver, syncs the staged tree with
   `rsync --delete` behind the guard, writes the spec by rename, records
   the run and an event, and starts the driver. A job without a host is
   a `queued` row.
4. The driver writes the heartbeat by rename, the logs into the tree,
   and the task queue in the state directory.
5. `watch.cycle` ingests the heartbeats into `runs` and `stage_runs`,
   classifies, acts, collects into `data/results`, extracts metrics into
   `metrics`, writes `params`, resumes, launches queued rows, writes the
   boards.
6. `export.export` selects the newest run per label of one source tag
   from the ledger and copies its results with a manifest.

The read verbs (`status`, `events`, `hosts`, `lic`, `metrics`, `check`)
ingest the heartbeats too, so the board follows the driver and not the
last watcher cycle; that step is idempotent.

## The driver protocol

The driver reads one spec and nothing else. It writes one heartbeat
file, by a temporary file and a rename, every `heartbeat_s` seconds and
at every phase change. It reads the stop file and the keep file next to
the spec every 0.5 s and at every heartbeat. A task group claims tasks
by renaming files in the queue directory. The exit code names the
terminal phase. `docs/running.md` has the fields, the phases and the
codes; the spec shape lives in `launch._spec`, and a change to it
changes the driver and the tests in the same commit.

## Rules every change keeps

- Every `rm -rf` and every `rsync --delete` calls `assert_safe_target`
  first, and every path built from a run id calls `assert_run_id`. The
  guard refuses `/`, the home directory, a one-component path, a path
  without the marker and a path shallower than `min_depth`.
- A dry run writes nothing: no date pin, no spec, no file on a host, no
  ledger row, no event, no database. `tests/test_e2e_local.py::
  test_dry_run_flow_writes_nothing` proves it for the whole flow.
- A read verb creates nothing.
- The watcher never deletes. A budget stops or kills; it never removes
  a file.
- A stop signals the pids the driver recorded, never a session name or
  a process pattern.
- A swallowed failure is worse than a crash: a loop that continues
  counts what it skipped and reports the count at the end.
- Standard library plus `rich` in the controller; the driver stays
  standard library only.
- The driver stays on the Python 3.6 subset and never imports the
  package; a feature that subset cannot express belongs in the
  controller.
- No site string in the repository: no host name, licence server, user
  name, chat id or unpublished design name. The demo uses `local`,
  `hostA`, `user`, `demo` and `k_small`.
- Tests write under `tmp_path`, use the host `local` only, and start a
  driver only through `tests/helpers_driver.py`.
