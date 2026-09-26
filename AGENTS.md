# Agents

How an agent operates a farm with edarunner. An agent is a Claude session,
a script, or a cron job. The rules also hold for a person in a hurry.

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

Read `code` first: 0 done, 1 refused by a guard or bad input, 2 nothing to
do, 3 some hosts failed. Act on `data`. Quote `output` in a report.

## Start with triage

`edr status --triage` is the entry point. It lists every run that is not
`running`, with the state, the phase, and one proposed command:

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
it before you trust a running count. A heartbeat file keeps its last phase
after the driver dies.

## Dry run first

Run the dry twin before every write: `--dry-run` on `stage`, `plan`,
`launch`, `run`, `keep`, `export`, `stop`, `retire`. Read every path in the
output. A launch shows the run id, the host and the root of every job. A
retire shows every `rm -rf` target. Then run the verb without the flag.

Confirm within one minute that the run made progress. `edr status
<handle>` shows a phase past `setup`.

## Say why

`stop` and `retire` refuse without `--why`. Write the state and the
evidence, not the verb:

```sh
edr stop a@demo --after-task --why "hung: no progress since 14:02, log stops at step 9"
edr retire a@demo --why "superseded by a@demo2, results collected"
```

The text lands in the events table with the actor. `edr events --run
<handle>` shows the history of a run. Read it before you act on a run you
did not start.

## Read a number before you use it

- `edr metrics --design <src>` gives one design. Never put two hashes in
  one table. Say the hash in every caption.
- Compare energy, not power, and measure the whole busy window. A fixed
  window measures a different fraction of each kernel and can invert a
  ranking.
- Every metric row carries `source_file`. Read the source of a number
  before it goes in a table.

## What an agent never does

- Never `tmux kill-server`, `pkill`, `pgrep -f`, or `kill` by hand.
  `edr stop <handle>` signals the recorded pids of one run.
- Never `rm -rf` or `rsync --delete` by hand. `edr retire` after
  `edr watch --once`, so the results are in `data/results/` first.
- Never `--now` as the first move. `--after-task` first, then `stop`, then
  `--now`.
- Never more than one handle per stop. Do not widen the blast radius past
  the run.
- Never trust the board for a running count. `edr status --live` asks the
  hosts.
- Never write under `<state>`, a run tree, or the driver copy by hand.
  `edr keep` and `edr stop --after-task` write the keep and stop files;
  `edr launch` publishes the driver by rename.
- Never relaunch a batch under its old name to get new directories.
  `launch` refuses a job whose spec exists. Use a new batch name.
- Never stop `edr watch` to make the board quiet. Use `edr keep <handle>
  --ack` on the run.
- Never write a site string into the public repository: a host name, a
  licence server, a user name, a chat id. See `CONTRIBUTING.md`.
