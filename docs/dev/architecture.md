# Architecture

After this page you can find the module that owns a behaviour, and follow
one run through the code from `plan` to `export`.

## Three programs

- `edr`, the controller: Python 3.11 or newer, the standard library plus
  `rich`, on the head node. `src/edarunner/`.
- `edr_driver.py`, the driver: one file, Python 3.6 or newer, standard
  library only, copied to the state directory at launch. It runs one run
  and never imports the package. `src/edarunner/driver/`; [driver.md](driver.md)
  has the protocol.
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
| `checkout.py` | `edr checkout`: worktrees, nested repositories, the dirty snapshot |
| `hosts.py` | the ssh wrapper with timeouts, the host probe, placement, the head-node check |
| `sync.py` | the rsync of the tree behind the guard, the driver copy by rename, the sync hook |
| `launch.py` | spec rendering, `plan`, `launch`, `stop` |
| `backend.py` | the `Backend` protocol: `submit`, `alive`, `stop`, `free`, `file_host`; `SshBackend` and `LocalBackend`, picked by `[scheduler] backend` |
| `driver/edr_driver.py` | one run on one host: stages, task groups, gates, budgets, retries, the heartbeat, the stop and keep files |
| `watch.py` | the cycle: classify, act, collect, resume, launch queued, boards, `watch.json` |
| `collect.py` | the rsync of the collect paths into `data/results` |
| `metrics.py` | the four parsers, extraction |
| `db.py` | the run database: the SQLite schema, upserts, queries, `board.json` |
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
   a spec, the JSON file the driver reads. A problem lands in the plan,
   not in an exception.
3. `launch.launch` publishes the driver, syncs the checked-out tree with
   `rsync --delete` behind the guard, writes the spec by rename, records
   the run and an event, and starts the driver through the backend,
   which returns the handle that `runs.handle` keeps. A job without a host is
   a `queued` row.
4. The driver writes the heartbeat by rename, the logs into the tree,
   and the task queue in the state directory.
5. `watch.cycle` ingests the heartbeats into `runs` and `stage_runs`,
   classifies, acts, collects into `data/results`, extracts metrics into
   `metrics`, writes `parameters`, resumes, launches queued rows, writes the
   boards.
6. `export.export` selects the newest run per label of one source tag
   from the database and copies its results with a manifest.

The read commands (`status`, `events`, `hosts`, `tools`, `metrics`, `check`)
ingest the heartbeats too, so the board follows the driver and not the
last watcher cycle; that step is idempotent.
