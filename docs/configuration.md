# Configuration

Four TOML files describe a project. `edr.toml` and `tasks.toml` live in
the project directory, one `jobs/<batch>.toml` per batch next to them,
and `site.toml` outside the project with the hosts and the bot.

These rules hold for every file:

- An unknown key is an error. A value of the wrong type is an error that
  names the file and the key path, so `cores = "16"` stops at load.
- A key the tables list without a default is required.
- A path is absolute or relative to the file that names it, and `~`
  expands.
- A string may hold `{placeholders}`; the last section lists them. `${VAR}`
  belongs to the shell and stays as it is.
- A hook is `python:<file>:<function>`, with `<file>` relative to the
  project directory.

`edr check` loads all four, imports every hook, probes the hosts and
plans every batch under `jobs/`, so a wrong file stops there.

## edr.toml

| Key | Meaning | Default |
|---|---|---|
| `schema` | file format version; only `1` | `1` |
| `project` | project name; `{project}` | required |
| `site` | path of `site.toml` | required |
| `state` | the state directory, on a filesystem every host mounts | `"~/.edr/{project}"` |
| `data` | the head-node data directory: `edr.db`, `results/`, `board/` | `"data"` |
| `run_prefix` | the run tree prefix under the host scratch | `"{user}/edr/{project}"` |
| `telegram_poll` | `false`: this project's watcher sends alerts and the board but does not poll for commands. One project per bot token polls | `true` |

`site`, `state`, `data`, `source.repo` and `source.worktrees` render at
load time with `{project}`, `{project_root}`, `{user}` and `{site_dir}`.
Every other string keeps its placeholders until `plan`.

### [source]

| Key | Meaning | Default |
|---|---|---|
| `repo` | the git repository of the flow | required |
| `worktrees` | where `edr stage` adds a worktree per commit | required |
| `ref` | the ref `edr stage` takes without an argument | `"HEAD"` |
| `nested` | nested repositories inside the tree, cloned at the HEAD the repository copy has | `[]` |
| `run_id` | the run id template | `"{date}_{label}_{build_tag}_g{src}"` |
| `build_tag` | hook that returns the build tag from `(config, overrides, worktree)` | `""`, which gives `{config}` plus `_KEYVALUE` per override |

### [env]

The variables every command of every stage needs, on top of the site
`env`. A value takes the run placeholders, and `$VAR` expands on the host
when the driver starts. `PATH = "{root}/.venv/bin:$PATH"` serves a flow
that calls `python` from the venv of its own tree.

### [sync]

| Key | Meaning | Default |
|---|---|---|
| `exclude` | rsync exclude patterns for the copy of the tree | `[]` |
| `after` | a command on the head node after each sync, with the run placeholders | `""` |

### [safety]

| Key | Meaning | Default |
|---|---|---|
| `marker` | a substring every delete target must hold | `"/edr/"` |
| `min_depth` | the smallest path depth of a delete target | `4` |

`docs/safety.md` explains the guard.

### [limits]

| Key | Meaning | Default |
|---|---|---|
| `stagger_s` | pause between two launches of one batch | `120` |
| `stale_s` | heartbeat age that marks a run `stale` | `600` |
| `dead_s` | heartbeat age that marks a run `dead` | `2700` |
| `hung_s` | time without progress that marks a run `hung` | `21600` |
| `grace_s` | wait between an alert and the watcher's stop or kill | `3600` |
| `host_free_min_gb` | free space below which the driver starts nothing new | `100.0` |
| `streak` | equal failure signatures in a row that stop a task group | `3` |
| `heartbeat_s` | period of the heartbeat and of the watcher cycle | `60` |
| `gate_max_s` | longest wait at a licence gate | `14400` |
| `kill_hung` | the watcher kills a hung run after `grace_s` | `false` |
| `kill_orphan` | the watcher kills an orphan tool process after `grace_s` | `false` |

### [placement]

| Key | Meaning | Default |
|---|---|---|
| `max_per_host` | our runs per host | `2` |
| `min_free_cores` | free cores a host needs to take a run | `16` |
| `min_free_ram_gb` | free RAM a host needs | `60` |
| `avoid` | hosts `auto` never picks | `[]` |
| `prefer` | hosts `auto` tries first, in order | `[]` |

A job with `host = "auto"` goes to the first host, preferred ones first
and then the one with the most free cores, that is not avoided, runs
fewer than `max_per_host`, has the free cores, RAM and disk the job's
first stage needs. No such host means the job is queued.

### [telegram]

The table is optional. It takes four keys of the site's `[telegram]`
table and replaces them for this project only. A key it leaves out keeps
the site's value. Without a `[telegram]` table in `site.toml`, the table
here needs `chat_id`. The custom commands stay in `site.toml`.

| Key | Meaning | Default |
|---|---|---|
| `token_file` | the bot token of this project, mode 600 | the site's |
| `chat_id` | the one chat the bot of this project answers | the site's |
| `user_id` | the one user whose messages and buttons the bot obeys | the site's |
| `topic_id` | the forum thread of every message of this project | the site's |

`docs/telegram.md` says when a project needs its own bot.

### [stages.<name>]

A stage is one command of the flow. Stages run in the order of the file.
A job runs every stage, or the subset its `stages` list names, in that
same order.

| Key | Meaning | Default |
|---|---|---|
| `cmd` | the command; in a task group it runs once per task | required |
| `resume` | the command with `{checkpoint}`, for a resume | `""` |
| `cwd` | the working directory, relative to the run tree | `"."` |
| `steps` | the step names the flow passes, indexed by step number | `[]` |
| `progress` | a command that prints the current step number | `""` |
| `needs` | `{ cores, disk_gb, licence }`; `licence` is a name or `{ name = seats }` from the site | `{ cores = 1, disk_gb = 0.0 }` |
| `budget` | `{ hours, disk_gb, kill, per }`; `per` is `"stage"` or `"task"` | `{ kill = false, per = "stage" }` |
| `retry` | `{ match, wait_s, max }`; `match` is a regex over the last 80 log lines | none; `wait_s = 900`, `max = 3` |
| `collect` | paths under the run tree the watcher copies when the stage ends | `[]` |
| `collect_on_request` | named path sets for `edr run --collect <name>` | `{}` |
| `prune` | named path sets for `edr retire --prune <name>` | `{}` |
| `foreach` | `"tasks"` makes the stage a task group | `""` |
| `parallel` | tasks at once in a group | `1` |
| `prepare` | a command once before a group starts | `""` |
| `task_dir` | the directory of a task, relative to the tree; required in a group | `""` |
| `after_each` | a command after each task, with `{task_dir}` | `""` |

#### Steps

A flow that runs several steps inside one tool session stays one stage,
and `edr` tracks the steps. The driver runs `progress` every 5 s in the
stage's `cwd` and takes the first number it prints as the current step.
`steps[step]` is the step name in the heartbeat and on the board, and
the checkpoint the watcher resumes from.

A numbered step belongs to one stage. The `steps` list of a stage is
indexed by the step number and continues the list of the stage before
it, so a flow with two sessions over one numbering lists all names in
the second stage. A list that is not longer than the steps before it
names the stage's own steps and continues from the previous end. A stage
without `steps` owns no numbered step.

#### Task groups

A stage with `foreach = "tasks"` runs `cmd` once per task of the job,
`parallel` at a time, each in its own `task_dir` with its own log,
budget and result. The tasks of a run go through a queue in the state
directory, so a second run with the same queue takes tasks from the same
pool. `docs/running.md` explains the queue and shards.

### [metrics.<name>]

| Key | Meaning | Default |
|---|---|---|
| `stage` | a stage name or a list; the stages whose files hold the number | `[]`; required without `expr` |
| `step` | `"*"` for one row per step, a number, or absent | none |
| `file` | the file under the collected results; `{step}` and `{task_dir}` allowed | required without `expr` |
| `regex` | a regex; group 1 is the value | one of the five |
| `csv` | `{ where = { column = value }, column }`; the first row that matches `where` | one of the five |
| `json` | a dotted path into a JSON file; a number indexes a list | one of the five |
| `python` | a hook that gets the file path and returns a number | one of the five |
| `expr` | an expression over other metrics of the same run, stage, step and task | one of the five |
| `unit` | unit text | `""` |
| `canonical` | a name shared across projects, such as `area.cell` | `""` |

A metric holds exactly one of the five parsers. `expr` allows numbers,
metric names, `+ - * /` and a unary minus, nothing else; it is computed
once every input exists, and with `stage` set only for those stages.

A metric row comes from a stage or a task that ended `done`. A
`step = "*"` metric gives one row per step directory found, under the
stage that owns that step number. A file that does not parse gives a row
with an empty value and the error in `source_file`, never a crash.

## site.toml

| Key | Meaning | Default |
|---|---|---|
| `schema` | only `1` | `1` |
| `scratch` | scratch roots, in order; the largest writable one is the mount | required |
| `env` | environment for every command on every host | `{}` |
| `ssh` | `{ options, timeout_s }` | `{ options = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"], timeout_s = 45 }` |
| `tool_procs` | a regex over process names, for the orphan check and the host table | `""` |
| `nfs_export` | a path the head node reads when ssh to a host fails at collect | `""` |

Every remote command runs through `sh -c`, so the login shell of a host
may be `csh` or `tcsh`.

### [hosts.<name>]

| Key | Meaning | Default |
|---|---|---|
| `cores` | cores | required |
| `ram_gb` | RAM | required |
| `scratch` | scratch roots of this host | the site `scratch` |

The host `local` is the head node itself, reached without ssh. A job that
names a host outside this table is a `check` problem.

### [licences.<name>]

| Key | Meaning | Default |
|---|---|---|
| `feature` | the FlexLM feature name | required |
| `floor` | free seats to leave for others | required |
| `probe` | a command that prints `lmstat` output; `{root}` allowed | required |
| `seats_per_task` | seats one task takes | `1` |

The driver reads the line `Users of <feature>: (Total of N licenses
issued; Total of M licenses in use)` and waits while `N - M - seats` is
below `floor`. A probe that fails counts as unknown and lets the stage
run.

### [telegram]

| Key | Meaning | Default |
|---|---|---|
| `token_file` | the bot token, mode 600 | `"~/.config/edarunner/telegram.token"` |
| `chat_id` | the one chat the bot answers | required |
| `user_id` | the one user whose messages and buttons the bot obeys | none; the chat is the only gate |
| `topic_id` | the forum thread of every message; commands from another thread are ignored | none; the main thread |

### [telegram.commands.<name>]

| Key | Meaning | Default |
|---|---|---|
| `help` | the help line | required |
| `run` | the argv list, never a shell string | required |
| `args` | argument name to regex, in order | `{}` |
| `skip_if` | an argv list; exit 0 skips `run` | none |
| `skip_reply` | the reply when skipped | `"skipped"` |
| `reply` | the reply after `run` instead of its output | `""` |
| `detach` | start `run` in its own session and reply at once | `false` |
| `timeout_s` | kill `run` after this | `60` |
| `cwd` | the working directory of `run` | the project directory |
| `dry_run` | reply with the rendered argv and run nothing | `false` |

`docs/telegram.md` explains the bot.

## tasks.toml

The file is optional. A task is a row of the table, and every key of a
task is a placeholder `{task.<key>}` in the strings of a task group.

| Key | Meaning | Default |
|---|---|---|
| `tasks.<id>.<key>` | any key; `{task.<key>}` in the stage strings | none |
| `tasks.<id>.needs` | `{ cores, disk_gb, licence }`; replaces the stage's | none |
| `tasks.<id>.budget` | `{ hours, disk_gb, kill, per }`; replaces the stage's | none |
| `pattern.resolver` | a hook `id -> table` for ids the file does not list | `""` |

```toml
[tasks.softmax_197]
kernel = "softmax"
args = "ROWS=197 COLS=197"
test = "SOFTMAX_R197_C197"
needs = { disk_gb = 60 }
budget = { hours = 8 }
```

## jobs/<batch>.toml

| Key | Meaning | Default |
|---|---|---|
| `batch` | the batch name | the file stem |
| `source` | a tag from `edr stage`, or a ref that `edr stage` has staged | required |

A tag is a short hash, or `<hash>-dirty-<8 hex>` for a snapshot of a
tree with uncommitted changes.

### [[job]]

| Key | Meaning | Default |
|---|---|---|
| `label` | the run label; unique in the batch | required |
| `config` | the configuration name the flow takes; `{config}` | required |
| `host` | a host name, or `"auto"` | `"auto"` |
| `stages` | the stages to run, a subset of `edr.toml` in file order | every stage |
| `tasks` | the task ids of the task groups | `[]` |
| `overrides` | `KEY = VALUE`; `{overrides}` renders them as `KEY=VALUE` tokens | `{}` |
| `netlist_stage` | a number the flow needs to find its netlist; `{netlist_stage}` | none |
| `reuse` | `{ run_id = "..." }` or `{ label = "...", latest = true }`; start on the tree of that run. With `restore = "<name>"`, start on a fresh tree with the `collect_on_request.<name>` files of that run copied back from `data/results/` | none |

`check` and `plan` verify an override key is an identifier, and that a
stage of the job uses `{overrides}` in `cmd`, `resume` or `prepare`.
They do not know the flow's own variables, so a key the flow ignores
passes. A job with `reuse` runs on the host and the tree of the reused
run, and takes its build tag and `{tree_id}`; a glob in `label` is an
error. With `restore`, the job takes the source tag, the build tag and
`{tree_id}` of the reused run but is placed like a new job, so it runs
after the tree was retired; `docs/running.md` shows the rerun. A task
group in a job without `tasks` is a plan problem.

## Placeholders

| Placeholder | Value | Where |
|---|---|---|
| `{project}`, `{project_root}`, `{site_dir}`, `{user}` | the project name, its directory, the site file's directory, the login name | everywhere |
| `{date}` | the pinned date of the batch, `YYYYMMDD_HHMM` | the run id |
| `{batch}`, `{label}`, `{config}`, `{build_tag}`, `{src}` | the job's identity | the run id, the stage strings |
| `{overrides}` | `KEY=VALUE` tokens separated by spaces | the stage strings |
| `{netlist_stage}` | the job's `netlist_stage`; only when the job sets it | the stage strings |
| `{run_id}`, `{host}`, `{mount}`, `{root}` | the run, its host, the scratch mount, the run tree | the stage strings, `[env]`, `sync.after` |
| `{tree_id}` | the run id of the tree the flow writes in: the reused run's id under `reuse`, else `{run_id}` | the stage strings |
| `{cores}` | `needs.cores` of the stage or task | the stage strings |
| `{checkpoint}` | the step name a resume starts from | `resume` |
| `{task_dir}`, `{task.<key>}` | the task directory and the task's keys | a task group's strings, `collect`, metric files |
| `{step}` | the step number | a metric `file` with `step = "*"` |

A placeholder without a value is an error at `plan`, which names it. A
dict value flattens to dotted keys, so a task table gives `{task.kernel}`.

The date is pinned once per batch in `<state>/<batch>/RUN_DATE`, so
`plan` and `launch` minutes apart name the same run ids. A batch name is
used once; a second launch of the same batch finds its specs and does
nothing.
