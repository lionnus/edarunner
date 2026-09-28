# Find out why a run failed

When a run ends `FAILED`, goes `dead` or waits at a gate, this page
shows where to look, in the order that usually finds the cause fastest.
It also lists the causes that come up most often.

## Read the history of the run

```sh
edr brief --run base@sweep1
```

`edr brief --run` prints the history of one run in Markdown: its stages
with their start, end and exit, the times of its steps, its events with
the reason of every action, the last 20 lines of the current log, its
metrics, and the one command that `edr status --triage` proposes for it.
It asks the host for the log tail, so it works while the run is live.
`edr status <handle>` shows the same run as a table, with the failure
signature of each task and the reason of a wait at a gate, and
`edr events --run <handle>` lists the events alone.

The phase names the stage that failed and the exit code says how;
[run.md](run.md#phases) lists every phase and code.

## Read the logs

The logs are in the run tree on the host:

| File | Holds |
|---|---|
| `log/setup.log` | the output of `[runtime] setup` |
| `log/<stage>.log` | the output of a stage command; every attempt is appended |
| `log/<stage>.prepare.log` | the `prepare` command of a task group |
| `log/<stage>.<task>.log` | one task, then its `after_each` |

Each command starts its log with a line `# edr: <cmd>`, the command as
the driver ran it after every placeholder was filled in. When a stage
ends, the watcher copies `log/` to `data/results/<run_id>/log/` on the
head node, so the log is still there after the tree is retired. On the
phone, `/log <handle>` sends the tail of the current log as a file.

The driver's own output goes to `<state_dir>/<batch>/<run_id>.driver.log`
in the state directory. Look there when the run has no heartbeat at all
or ended with exit 2, because then the driver stopped before it could
write one.

## See what the driver was told

```sh
edr plan sweep1 --show-spec
edr launch sweep1 --dry-run --show-spec
```

`--show-spec` prints each run as the driver gets it: the environment, the
working directory and the command of every stage with every placeholder
filled in, and the collect paths. Neither command runs anything. To
reproduce a failure by hand, log in to the host, `cd` into the run tree,
set the variables from `env`, and run the command from the first line of
the stage log.

## Common causes

### It works on the host `local` but fails on a site host

The stage command runs in a non-login bash without a terminal. It does
not read `~/.bashrc` or `~/.bash_profile`, so a `PATH`, a module or an
alias from your login is missing, and there is no `TERM` variable. A
tool that calls `tput` or a script that runs `clear` fails with a
message about the terminal. On the host `local` the driver inherits the
environment of your shell instead, which hides the difference. Put what
the flow needs into `[env]`, such as `TERM = "xterm"` or the tool's
`PATH`; [project.md](project.md#the-environment) explains how the site
and project tables combine.

### The run ends `FAILED:runtime`

`[runtime] setup` failed before the first stage, and `log/setup.log`
says why. The usual cause is a tool that the head node has and the host
lacks, such as `uv` or `git` missing from the host's `PATH`; `edr check`
does not look for them on the hosts. Add the tool's directory to the
site `[env]`.

### The run never writes a heartbeat

The driver did not start, or stopped before its first heartbeat. Its
log in the state directory names the reason. Typical causes are a host
whose `python3` is older than 3.6, a state directory that the host does
not mount at the same path as the head node, and a run tree that is not
a directory. [site.md](site.md#what-a-host-needs) lists what a host
needs.

### The run waits at a gate

The phase is `gate:<stage>`, and `edr status <handle>` shows a line such
as `pnr: 1 free, 1 held by others, 1 needed`. The probe of the tool
reports too few free seats, or other runs have leased them. `edr tools`
runs every probe and shows the free seats of each tool. After `gate_max_s` the stage fails
with exit 4. A probe that fails or prints no number does not block the
stage; the driver logs it and runs the stage.

### The run ends with exit 3 or `host_full`

Exit 3 means the scratch disk had less free space than `needs.disk_gb`
of the first stage at the start. `host_full` means the free space fell
below `host_free_min_gb` of the site or of the host while runs were
going; after `grace_s` the watcher stops the newest run on that host,
and a keep does not hold that stop off. `edr retire --host <host>
--prune <name>` removes the prune targets of your finished runs there,
except the runs whose trees have stages left.
`edr hosts` shows the scratch of each host and how much your trees
hold.

### The run went over budget

`OVER_BUDGET:<stage>` means the stage took longer than `budget.hours`
or its tree grew past `budget.disk_gb`. When the stage only needs more
time, `edr keep <handle> --hours <n>` extends the budget of the running
stage before the limit hits. A stage without `kill = true` runs to its
end, but the stages after it do not run; `edr continue <handle>` runs
them on the same tree once the run has ended. Raise the budget in
`edr.toml` for the next batch.

### A task group ends `INCOMPLETE`

Some tasks failed or were skipped. `edr status <handle>` shows the
failure signature of each task, the last log line with its digits
removed, and `log/<stage>.<task>.log` holds the whole output. After a
fix, `edr continue <handle> --stage <S> --tasks <id>...` runs only those
tasks on the same tree. When `limits.streak` tasks in a row fail with
the same signature, the group stops claiming tasks and the run shows
`looping`, which usually points to a fault shared by every task.

### A run is `hung`

The heartbeat is fresh, but nothing has changed for `limits.hung_s`:
neither the step, the log, the tree size nor the CPU time. Read the log tail
first. The watcher kills a hung run only when `kill_hung` is set, and
`edr keep <handle> --hours <n>` holds that off for n hours.

### A metric is missing

The watcher extracts a metric only from a stage that ended `done`, and
only from a file that `collect` copied to the head node. Check that the
file is under `data/results/<run_id>/` at the path that the metric's
`file` names. After you fix a metric definition, `edr extract` reads the
collected files again; [results.md](results.md#extract-again) shows it.

## Resume a run that died

A stage with a `resume` command can continue from a step. `{checkpoint}`
in that command takes the step name, so a flow with
`FIRST_STAGE={checkpoint}` skips the steps before it. The watcher
resumes a `dead` run this way once, from the last step of its heartbeat.
To resume a run by hand:

```sh
edr continue a@sweep1 --stage pnr --from cts
```

Without `--from` the stage starts from its first step, which in many
flows deletes the checkpoints it would need. `edr status --triage`
proposes the command with `--from` filled in from the heartbeat.
