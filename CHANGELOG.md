# Changelog

## 0.4.0 (2026-09-27)

This is the first public release. edarunner runs the EDA flow you already
have on the machines you already use, and records every run in one SQLite
database.

### Run

- A project is a few TOML files: `edr.toml` declares the flow as stages,
  `tasks.toml` lists the task groups, `jobs/<batch>.toml` lists the jobs
  of a batch, and a private `site.toml` describes your hosts and tools.
- `edr checkout` pins the source as a detached local clone, or as a
  snapshot of a dirty tree. Its short hash tags every run built from it.
  The copy on the host keeps `.git`, so the flow asks git for its
  version there as it does anywhere else, and every command gets
  `EDR_SRC`, `EDR_RUN_ID` and `EDR_TREE_ID` in its environment.
- `[runtime] setup` runs one command on the host before the first stage,
  such as `uv sync --frozen`, and logs it to `log/setup.log`. A failure
  ends the run as `FAILED:runtime` before any tool seat is taken, and
  `when_changed` runs it again on a continued tree only when a listed
  file changed. `edr status` and `edr brief --run` show it as the phase
  `setup`, with its start, duration and outcome.
- `edr plan` prints the run ids, hosts and paths of a batch without
  writing anything, and `edr launch` starts one driver per job. With
  `--show-spec`, both print the environment, the commands and the collect
  paths each run gets.
- The driver is a single Python file that needs only `python3` 3.6 on the
  host. It runs the stages and task groups, applies time budgets and
  retries, waits for free licence seats, and writes a heartbeat file.
- A job with `host = "auto"` goes to a host with enough free cores, RAM
  and disk and with every tool the job needs. When no host fits, the job
  waits in a queue.
- `edr continue` does more work on the tree of an existing run, a job can
  reuse the tree of an earlier run, and `edr track` records a command you
  run by hand as a run of the project.
- `edr stop`, `edr keep` and `edr retire` stop a run, extend its budget
  and remove its tree. Every command that writes takes `--dry-run`, and
  edarunner deletes nothing on its own.

### Track

- The run database keeps every run with its source hash, host, events,
  metrics and collected files. It works on a local disk and on NFS.
- `edr watch` reads the heartbeats, flags a run that died, hangs or runs
  over budget, collects the reports and extracts the metrics.
- `edr status` draws the board, `edr hosts` shows the load and the free
  resources of each host, `edr tools` shows the free licence seats, and
  `edr events` lists what happened and who did it.
- `edr import` records a run that edarunner did not launch, or only the
  collected results of one.

### Analyze

- A metric comes from a report file through a regex, a CSV row, a JSON
  path, a hierarchical area report or a Python hook.
- `edr metrics` prints the metrics of one design or one run,
  `edr compare` puts two or more runs side by side with the deltas, and
  `edr runtime` prints the time of each stage, step and task.
- `edr export` writes a frozen snapshot of one design with a manifest,
  and `edr export --mlflow` writes the runs into an MLflow tracking store.
- The watcher writes two HTML boards: `status.html` for the farm and
  `compare.html` for the metrics of the runs.

### Notify

- Alerts go to every configured channel: Telegram, ntfy and mail.
- The Telegram bot shows the board, the hosts, the events and the tools,
  sends logs and CSV files, and has buttons to keep, acknowledge or stop
  a run. A site can add its own commands.
- A daily digest lists the runs that ended, the live runs and the open
  alerts.
- `edr notify` sends a message, the board or the digest from a script or
  a hook.

### Schedulers

- Besides plain ssh hosts and the local machine, a run can go to an
  HTCondor, Slurm or LSF scheduler. The driver runs as the job, and the
  watcher reads the job state from the scheduler.
- CI runs the local demo on an HTCondor pool and on a Slurm cluster in
  containers. LSF has rendering and contract tests only.

### Agents

- Every command takes `--json` and prints one object with the exit code,
  the data and the text a person would see.
- `edr stop` and `edr retire` require `--why`, and the reason goes into
  the event log together with who acted.
- `edr brief` prints a Markdown briefing of the project for a person or
  an agent who is new to it, and `edr status --triage` proposes one
  command for each run that needs attention.
- `AGENTS.md` is the operating guide for an agent.

### Changes from the pre-release versions

Versions before 0.4.0 were pre-release. These changes can break a setup
from that time:

- The old command names are gone: `edr stage` is `edr checkout`,
  `edr run` is `edr continue`, `edr lic` is `edr tools`, and the bot's
  `/lic` is `/tools`.
- `edr plan` is a read command and no longer creates `data/edr.db`.
- `edr import --config` is optional and defaults to an empty
  configuration name.
- Custom bot commands and metric `file` strings render with the same
  placeholder engine as the rest of the configuration. A placeholder
  without a value is an error that names it, and the `{{` and `}}`
  escapes no longer apply there.
- A bot command sent as a reply to an alert can use `{handle}` and
  `{run_root}`, the handle and the run tree of that run.
- The `plots` extra is gone; the boards draw their plots in the browser.
- The documentation is organized by what a reader wants to do: Get
  started, How it works, one guide per task, the reference and the
  development pages. The old pages `concepts`, `guarantees`,
  `configure`, `run`, `results`, `notify` and `telegram` redirect to
  their new places.
