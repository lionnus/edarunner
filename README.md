# edarunner

Run a flow you already have on hosts you already reach over ssh. Keep one
ledger of every run: source versions, host, stages, tasks, metrics,
artifacts, events. Watch the runs without an agent, apply declared
limits, and get told on your phone. Export a frozen snapshot for a paper.

Nothing in the core knows an EDA tool. Your project config names the
commands, the report files and the numbers in them.

## Install

```sh
uv tool install edarunner        # the controller, Python 3.11 or newer
```

A compute host needs only ssh, `rsync` and its own `python3` (3.6 or
newer). Nothing is installed there; the driver is one file copied at
launch.

## Five minutes on one machine

```sh
git clone https://github.com/lionnus/edarunner && cd edarunner/examples/local-demo
bash setup.sh                 # a fake flow in a small git repository
edr stage HEAD                # a pinned worktree of the source
edr check                     # config, hosts, hooks, guards
edr plan demo                 # run ids, hosts, every path; writes nothing
edr launch demo               # one driver per run on the `local` host
edr status                    # the board
edr watch --once              # collect, extract metrics, classify
edr metrics --design HEAD --csv
```

The demo flow sleeps for seconds and writes fake reports and a fake
`power.csv`, so every command runs without an EDA tool or a licence.

## What a project declares

| File | Holds |
|---|---|
| `edr.toml` | the stages (one command each, or a task group), their needs, budgets and retry rules, the metrics and where to read them |
| `tasks.toml` | the task table: a kernel, a test pattern, an argument string |
| `jobs/<batch>.toml` | one batch: the source commit and one job per run |
| `site.toml` | the hosts, the scratch layout, the licence probes, the Telegram bot; private, outside the project |

A stage is a command the driver runs in its own process group. Inside
it, the flow's own steps are tracked, not run: a `progress` probe says
where the flow is, and metrics are extracted per step. A task group runs
its tasks in parallel on the host, each with its own directory, budget
and result, and shards claim tasks from one queue.

## What you get back

- `edr status`, on 48 columns if you ask, so it reads in an ssh app on a
  phone; `edr status <run>` for one run.
- `edr events`: every action with who did it and why.
- `edr metrics --design <hash> --csv`: every number, never two hashes in
  one table unless you say so.
- `edr export`: a snapshot with a manifest, `runs.csv`, `metrics.csv`
  and the small report files, for a paper that reads snapshots only.
- The Telegram bot: alerts with buttons, a pinned board, `/keep`, `/ack`,
  `/stop`, and custom commands you declare.

## Safety

Every delete and every `rsync --delete` runs through a guard that
refuses an empty path, a root, a home directory and a path without the
project marker. Every verb that writes has `--dry-run`, and a dry run
writes nothing. `stop` and `retire` need `--why`. No guard deletes a
result; a killed run keeps its tree until you retire it. See
`docs/safety.md`.

## Documents

- `docs/design.md`: the contract between the modules, the file formats,
  the driver protocol, the CLI.
- `docs/config.md`: every key of the four TOML files.
- `docs/telegram.md`: the bot, from BotFather to custom commands.
- `AGENTS.md`: how an agent operates the farm through `edr`.

## Status

Alpha. Maintained as time allows by one PhD student. Issues and adapters
welcome. Apache-2.0.
