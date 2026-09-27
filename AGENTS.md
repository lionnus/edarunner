# Agents

This file explains how an agent operates a farm with edarunner. An agent
here is a Claude session, a script or a cron job, but the rules hold just
as well for a person in a hurry.

## Start with edr brief

Run `edr brief` first. It prints what the project is, its flow, the
hosts and tools, the runs per batch, every run that needs a decision
with the proposed command, and the last ten events. Before you act on a
run you did not start, read its story with `edr brief --run <handle>`:
its phases, events, log tail, metrics and the proposed command.
`docs/run.md` shows the Claude Code hook that runs it at the start of
every session.

## Only through edr

Operate the farm through `edr` and nothing else. Do not call `ssh`,
`rsync`, `rm`, `kill`, `tmux` or the driver yourself. Every `edr` command
runs the guards, writes an event with the actor and the reason, and takes
`--dry-run`. A hand command has none of that.

## Every call with --json

Pass `--json` on every call. The result is one object:

```json
{"code": 0, "data": {}, "output": "the text a person would see"}
```

Read `code` first: 0 means done, 1 means a guard refused or the input was
bad, 2 means there was nothing to do, and 3 means some hosts failed. On
`launch`, 2 means every job was already launched; on `stop`, 3 means the
driver is still alive and `--now` is the next step. Act on `data`, and
quote `output` when you report. `docs/reference/cli.md` lists every
command.

## Triage

`edr status --triage` gives the decisions of the briefing on their own.
It lists every run that is not `running`, together with its state, its
phase and one proposed command:

```
dead        a@demo                       stage:pnr
    edr continue a@demo --stage pnr --from cts
```

`a@demo` is a handle. A handle names one run: `label@batch`, a run id
prefix, or `#n` from the last board that `edr status` printed.

`docs/reference/states.md` gives the proposed command per state. Check
this before you run it:

| State | Check before you run it |
|---|---|
| `queued` | `edr hosts` shows a host that fits |
| `stale` | nothing; the command only asks the host |
| `dead` | keep `--from`; without it the stage starts from its first step and can destroy the checkpoints it needs |
| `hung`, `looping`, `over_budget` | the log tail in `edr status <handle>` |
| `host_full` | `edr hosts`; one stop frees the host |
| `superseded` | the newer batch is the one you want |
| `done` | `edr metrics --design <src>` looks complete |
| other finished | `edr watch --once` collected the results |

`edr status --live` asks each host whether the driver process exists. Use
it before you trust a running count, because a heartbeat file keeps its
last phase after the driver dies.

## Dry run first

Do a dry run before every write: `--dry-run` on `checkout`, `plan`,
`launch`, `continue`, `keep`, `export`, `stop` and `retire`. Read every path in
the output. A launch shows the run id, the host and the root of every job,
and a retire shows every `rm -rf` target. Then run the command without the
flag.

Confirm within one minute that the run made progress: `edr status
<handle>` should show a phase past `setup`.

## Say why

`stop` and `retire` refuse to run without `--why`. Write the state and the
evidence, not the command:

```sh
edr stop a@demo --after-task --why "hung: no progress since 14:02, log stops at step 9"
edr retire a@demo --why "superseded by a@demo2, results collected"
```

The text goes into the event log together with who acted. `edr events --run
<handle>` shows the history of a run; read it before you act on a run you
did not start.

`edr notify "<text>"` sends one line through every notifier of the
project; send it when a long task ends or needs a person.

## Read a number before you use it

- `edr metrics --design <src>` gives the numbers of one source tag. Keep
  numbers from different tags out of one table, and name the tag in every
  caption.
- Every metric row carries `source_file`. Check which file a number came
  from before you put it in a table.

## What an agent never does

- Never run `tmux kill-server`, `pkill`, `pgrep -f` or `kill` by hand.
  `edr stop <handle>` signals the recorded pids of one run.
- Never run `rm -rf` or `rsync --delete` by hand. Run `edr retire` after
  `edr watch --once`, so the results are in `data/results/` first.
- Never use `--now` as the first move. Try `--after-task` first, then a
  plain `stop`, then `--now`.
- Never pass more than one handle to a stop, so that one mistake costs at
  most one run.
- Never trust the board for a running count. `edr status --live` asks the
  hosts.
- Never write under `<state_dir>`, a run tree or the driver copy by hand.
  `edr keep` and `edr stop --after-task` write the keep and stop files, and
  `edr launch` publishes the driver by rename.
- Never relaunch a batch under its old name to get new directories.
  `launch` refuses a job whose spec, the run's JSON file in the state
  directory, exists. Use a new batch name.
- Never stop `edr watch` to make the board quiet. Use `edr keep <handle>
  --ack` on the run instead.
- Never write a site string into the public repository: a host name, a
  licence server, a user name, a chat id. See
  [docs/dev/conventions.md](docs/dev/conventions.md).
