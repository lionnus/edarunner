<p align="center"><img src="docs/logo.svg" alt="edarunner" width="300"></p>

<p align="center">Run the EDA flow you already have on the ssh hosts you already reach, and keep one ledger of every run.</p>

<p align="center">
<a href="https://github.com/lionnus/edarunner/actions/workflows/ci.yml"><img src="https://github.com/lionnus/edarunner/actions/workflows/ci.yml/badge.svg" alt="ci"></a>
<img src="https://github.com/lionnus/edarunner/blob/ci-status/coverage.svg?raw=true" alt="coverage">
<img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
<a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="Apache-2.0"></a>
</p>

## Why

A place-and-route flow on a few shared ssh hosts fails in ways a Makefile
cannot see: a run dies overnight, a batch fills a disk, a kill by session
name takes a colleague's work with it, and the numbers for a paper end up
in a spreadsheet with no record of the commit that made them. edarunner
starts the flow you already have and records every run in one SQLite
file: source versions, host, stages, tasks, metrics, artifacts and events.
A watcher applies the limits you declare without throwing a result away,
and a Telegram bot puts the alerts on your phone. Nothing in the core
knows an EDA tool; the project config names the commands, the report
files and the numbers in them.

## Install

```sh
uv tool install git+https://github.com/lionnus/edarunner   # the controller, Python 3.11 or newer; PyPI follows the first release
```

A compute host needs only ssh, `rsync` and its own `python3` (3.6 or
newer). Nothing is installed there; the driver is one file that
`edr launch` copies over.

The watcher runs on the head node. `edr init` writes `edr-watch.service`
next to `edr.toml`, and with `loginctl enable-linger` the service survives
a logout and a reboot without root:

```sh
cp edr-watch.service ~/.config/systemd/user/edr-<project>.service
systemctl --user daemon-reload
systemctl --user enable --now edr-<project>
```

Without a user service, `edr watch` in a tmux session does the same job,
and `edr watch --check` from cron tells you when it stopped.

## Five minutes on one machine

```sh
git clone https://github.com/lionnus/edarunner && cd edarunner/examples/local-demo
bash setup.sh                 # a fake flow in a small git repository
edr stage HEAD                # a pinned worktree of the source; prints its short hash
edr check                     # load the config, probe the hosts, check the hooks
edr plan demo                 # run ids, hosts, every path; writes nothing
edr launch demo               # one driver per run on the `local` host
edr status                    # the board
edr watch --once              # collect, extract metrics, classify
edr metrics --design <src> --csv   # <src> is the hash that `edr stage` printed
```

The flow sleeps for seconds and writes fake reports and a fake
`power.csv`, so every command runs without an EDA tool or a licence. The
two runs end `done` within a minute, and `edr watch --once` after that
collects everything. `examples/local-demo/README.md` says what the flow
fakes, where the files land and how to make a run fail.

## See the farm

`edr status` draws the board: one line per run, live runs first and dead
ones on top. The state has a colour on a terminal and none in a pipe.

```text
#   label  host   state    phase        stage/step  age  fail/done  cost
────────────────────────────────────────────────────────────────────────
#1  wide   hostB  stale    group:power  power/4     12m      1f/3d   9.6
#2  base   hostA  running  stage:pnr    pnr/4        1m      0f/0d   6.4
#3  small  hostA  done     done         export      35m      0f/4d   0.0
```

`edr status --narrow` fits the board in 48 columns for an ssh app on a
phone. The Telegram bot pins a 40-column board, one line per run, in
the chat and rewrites it every watcher cycle.

`edr hosts` shows each machine: cores used of total, RAM and scratch free
of total, GPUs idle of total with their memory free of total, tool
processes ours and others, and our runs. A host without `nvidia-smi`
shows `-` for the GPUs.

```text
host   cores            load   ram GB  mount      scratch GB            gpu  gpu GB  tools  runs
────────────────────────────────────────────────────────────────────────────────────────────────
hostA  52/64  ██████░░  51.5  120/256  /scratch     800/2000  █████░░░  1/4  290/320    4/2     2
hostB   3/32  █░░░░░░░   3.1   98/128  /scratch2    150/1000  ███████░    -       -    1/0     1
```

## How it works

<p align="center"><img src="docs/diagrams/where-it-runs.svg" alt="Where each part runs: the head node, the shared filesystem, the compute hosts and the phone" width="900"></p>

A project declares its flow in `edr.toml` as stages that run in order. A
stage is one command, which the driver runs in its own process group. The
flow's own steps inside it are tracked, not run: a `progress` probe
reports the current step, and metrics are read per step from the report
files. A stage with `foreach = "tasks"` is a task group. Its command runs
once per task, the tasks run in parallel on the host, each with its own
directory, budget and result, and shards claim tasks from one queue.

`edr launch` renders one spec per job, copies the one-file driver to the
host and starts it. The driver runs the stages, waits for licence seats,
applies budgets and retries, and writes a heartbeat file every minute.

`edr watch` on the head node reads the heartbeats, classifies every run
(running, stale, dead, hung, over budget, host full, superseded),
collects the report files, extracts the metrics, launches queued jobs
when a host fits, and notifies. It never deletes anything.

The ledger is one SQLite file, `data/edr.db`, with the batches, runs,
stages, params, metrics, artifacts and events. Every verb that changes
something writes an event with the actor and the reason.
`docs/configuration.md` lists every key of the four config files: `edr.toml`, `tasks.toml`,
`jobs/<batch>.toml`, and the private `site.toml` with the hosts and the
bot.

## Tools and processes

edarunner is glue around tools that a Linux host already has. Nothing runs
as root, and nothing is installed on a compute host.

| Tool | Used for |
|---|---|
| `ssh` with `BatchMode=yes` | every remote command, under `sh -c`, so a tcsh login shell is fine |
| `rsync` | the source tree to the host, with `--delete` behind the guard; the results back to `data/results/` |
| `setsid` and `nohup` | the driver starts in its own session, out of reach of a closed terminal or a lost ssh |
| `ps` from procps-ng, `/proc` | whether a driver is alive, the cpu time behind the hung check, load, memory and the working directory of a process |
| `git worktree` | one pinned checkout per source tag under `wt/`; an uncommitted tree becomes a snapshot commit |
| `sqlite3`, through Python | the ledger |
| `systemd --user` or tmux, and cron | the watcher, and the check that it is still there |
| `lmutil` | the seats of a FlexLM licence server; optional |

`edr launch` copies the driver, one Python file, into the state directory
on the shared filesystem, writes the spec of the run next to it, and
starts the driver over ssh with `setsid nohup python3 edr_driver.py`.
From there the run needs no connection to the head node. The driver
starts every stage command in a new process group, logs it to
`log/<stage>.log` in the run tree, writes its heartbeat by a rename every
minute and reads the stop and keep files next to the spec. A stop signals
the driver pid and the process groups the heartbeat names, never a
session name or a process pattern.

`edr watch` is one long-running process per project on the head node,
with the Telegram bot as a thread inside it. It runs as a systemd user
service with lingering, or in a tmux session, and a cron line with
`edr watch --check` tells you when it stopped. Every other verb runs and
exits. `docs/requirements.md` lists what each machine needs, and
`docs/architecture.md` follows one run from `plan` to `export`.

## What you get

- `edr status` draws the board, or one run with `edr status <run>`.
  `--narrow` fits 48 columns for an ssh app on a phone, `--live` asks the
  hosts whether the drivers exist, and `--triage` proposes one command for
  each run that is not running.
- `edr events` lists every action, with who did it and why.
- `edr metrics --design <hash> --csv` gives every number of one design.
  One table holds one design, so the flag has no default.
- `edr export` writes a snapshot with a manifest, `runs.csv`,
  `metrics.csv` and the small report files, for a paper that reads
  snapshots only.
- The Telegram bot sends alerts with buttons, keeps a pinned board, and
  answers `/keep`, `/ack`, `/stop` and the custom commands you declare.

## Safety

Every `rm -rf` and every `rsync --delete` passes a guard that refuses an
empty path, a root, a home directory and a path without the project
marker. Every verb that writes takes `--dry-run`, and a dry run writes
nothing. `stop` and `retire` need `--why`, and the reason lands in the
events table. A stop signals the pids the driver recorded, never a
session name or a `pgrep` pattern. Nothing deletes on its own: a killed
run keeps its tree until you retire it, and `docs/safety.md` names the
incident behind each rule.

## Documents

`docs/README.md` is the index. The pages:

- `docs/getting-started.md`, install, the demo, and a first project.
- `docs/configuration.md`, every key of the four TOML files.
- `docs/cli.md`, every verb with its flags and exit codes.
- `docs/running.md`, the driver, the run tree, phases, budgets, resume,
  shards, import and retire.
- `docs/watcher.md`, the cycle, the run states, the boards, the service.
- `docs/results.md`, the ledger, `metrics`, `export` and the snapshot.
- `docs/telegram.md`, the bot, from BotFather to custom commands.
- `docs/flows.md`, two real flows declared.
- `docs/safety.md`, the rules, the incident behind each one, the guards.
- `docs/architecture.md`, for contributors: the modules and the rules.
- `AGENTS.md`, how an agent operates the farm through `edr`.
- `examples/local-demo/README.md`, the demo project.

## Status and contributing

Alpha. One PhD student maintains it as time allows. It has run a
commercial place-and-route and power flow and an open Yosys and OpenROAD
flow; `docs/flows.md` shows both configs. Issues and adapters for other
flows are welcome. `CONTRIBUTING.md` has the rules; the short version is
the standard library plus `rich` in the controller, the driver on the
Python 3.6 subset with the standard library only, no site strings in the
repository, and tests under `tmp_path`.

## Licence

Apache-2.0. See `LICENSE`.
