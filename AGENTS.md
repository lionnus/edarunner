# Agents

This file explains how an agent operates a farm with edarunner. An agent
here is a Claude session, a script or a cron job, but the rules hold just
as well for a person in a hurry.

## Only through edr

Operate the farm through `edr` and nothing else. Do not call `ssh`,
`rsync`, `rm`, `kill`, `tmux` or the driver yourself. Every `edr` verb
runs the guards, writes an event with the actor and the reason, and has a
dry twin. A hand command has none of that.

## Every call with --json

Pass `--json` on every call. The result is one object:

```json
{"code": 0, "data": {}, "output": "the text a person would see"}
```

Read `code` first: 0 means done, 1 means a guard refused or the input was
bad, 2 means there was nothing to do, and 3 means some hosts failed. On
`launch`, 2 means every job was already launched; on `stop`, 3 means the
driver is still alive and `--now` is the next step. Act on `data`, and
quote `output` when you report. `docs/cli.md` lists every verb.

## Start with triage

`edr status --triage` is the entry point. It lists every run that is not
`running`, together with its state, its phase and one proposed command:

```
dead        a@demo                       stage:pnr
    edr run a@demo --stage pnr --from cts
```

| State | Proposed command | Check before you run it |
|---|---|---|
| `queued` | `edr launch <batch> --only <label>` | `edr hosts` shows a host that fits |
| `stale` | `edr status <handle> --live` | nothing; it only asks the host |
| `dead` | `edr run <handle> --stage <S> --from <step>` | keep `--from`; without it the stage starts from its first step and can destroy the checkpoints it needs |
| `hung`, `looping`, `over_budget` | `edr stop <handle> --why <state>` | the log tail in `edr status <handle>` |
| `host_full` | `edr stop <handle> --now --why host-full` | `edr hosts`; one stop frees the host |
| `superseded` | `edr stop <handle> --after-task --why superseded` | the newer batch is the one you want |
| `done` | `edr export --design <src> --out exports/<src>` | `edr metrics --design <src>` looks complete |
| other finished | `edr retire <handle> --why <state>` | `edr watch --once` collected the results |

`edr status --live` asks each host whether the driver process exists. Use
it before you trust a running count, because a heartbeat file keeps its
last phase after the driver dies.

## Dry run first

Run the dry twin before every write: `--dry-run` on `stage`, `plan`,
`launch`, `run`, `keep`, `export`, `stop` and `retire`. Read every path in
the output. A launch shows the run id, the host and the root of every job,
and a retire shows every `rm -rf` target. Then run the verb without the
flag.

Confirm within one minute that the run made progress: `edr status
<handle>` should show a phase past `setup`.

## Say why

`stop` and `retire` refuse to run without `--why`. Write the state and the
evidence, not the verb:

```sh
edr stop a@demo --after-task --why "hung: no progress since 14:02, log stops at step 9"
edr retire a@demo --why "superseded by a@demo2, results collected"
```

The text lands in the events table with the actor. `edr events --run
<handle>` shows the history of a run; read it before you act on a run you
did not start.

`edr notify "<text>"` sends one line to the chat of the project; send it
when a long task ends or needs a person.

## Read a number before you use it

- `edr metrics --design <src>` gives one design. Never put two hashes in
  one table, and say the hash in every caption.
- Compare energy, not power, and measure the whole busy window. A fixed
  window measures a different fraction of each kernel and can invert a
  ranking.
- Every metric row carries `source_file`. Read the source of a number
  before it goes in a table.

## What an agent never does

- Never run `tmux kill-server`, `pkill`, `pgrep -f` or `kill` by hand.
  `edr stop <handle>` signals the recorded pids of one run.
- Never run `rm -rf` or `rsync --delete` by hand. Run `edr retire` after
  `edr watch --once`, so the results are in `data/results/` first.
- Never use `--now` as the first move. Try `--after-task` first, then a
  plain `stop`, then `--now`.
- Never pass more than one handle to a stop. Do not widen the blast radius
  past the run.
- Never trust the board for a running count. `edr status --live` asks the
  hosts.
- Never write under `<state>`, a run tree or the driver copy by hand.
  `edr keep` and `edr stop --after-task` write the keep and stop files, and
  `edr launch` publishes the driver by rename.
- Never relaunch a batch under its old name to get new directories.
  `launch` refuses a job whose spec exists. Use a new batch name.
- Never stop `edr watch` to make the board quiet. Use `edr keep <handle>
  --ack` on the run instead.
- Never write a site string into the public repository: a host name, a
  licence server, a user name, a chat id. See `CONTRIBUTING.md`.
