# Getting started

edarunner runs a flow you already have, a Makefile or a script, on hosts
you reach over ssh, and keeps one ledger of every run. This page goes from
an empty head node to a first batch with a watcher behind it.

## Install

The controller, `edr`, runs on the head node. It needs Python 3.11 or
newer; `rich`, its one dependency, comes with the install.

```sh
uv tool install git+https://github.com/lionnus/edarunner
```

`pipx install git+https://github.com/lionnus/edarunner` does the same. In
a checkout, `uv venv --python 3.11 .venv && uv pip install -e '.[dev]'`
gives you `.venv/bin/edr`.

The head node also needs `ssh` with keys that work without a prompt,
`rsync` and `git`, and a state directory on a filesystem that every host
mounts. A compute host needs nothing installed: a POSIX `sh`, `rsync`,
procps-ng and its own `python3`, 3.6 or newer. The driver is one file that
`edr launch` copies into the state directory. `docs/requirements.md` has
the full list, and `edr check` names what is missing.

`edr` works from any directory below `edr.toml`. Without one it stops
with `no edr.toml in <dir> or above; run edr init`.

## Five minutes on one machine

`examples/local-demo` is a fake flow that runs on the head node alone. It
needs no tool, no licence and no second host.

```sh
git clone https://github.com/lionnus/edarunner && cd edarunner/examples/local-demo
bash setup.sh                 # a small git repository with the fake flow
edr stage HEAD                # a pinned worktree of the source; prints its short hash
edr check                     # load the config, probe the hosts, check the hooks
edr plan demo                 # run ids, hosts, every path; writes nothing
edr launch demo               # one driver per run on the host `local`
edr status                    # the board
edr watch --once              # collect, extract metrics, classify
edr metrics --design <src> --csv   # <src> is the hash that `edr stage` printed
```

The two runs end `done` within a minute. `examples/local-demo/README.md`
says what the flow fakes, where the files land and how to make a run
fail.

## A real project

### 1. Create the project

```sh
mkdir myflow && cd myflow
edr init --site ~/.config/edarunner
```

`edr init` writes `edr.toml`, a template with one stage and one metric,
and `edr-watch.service`, the systemd unit for the watcher. `--site` names
the directory or the file of the private site file; `edr init` does not
write that file. The template then points at
`~/.config/edarunner/site.toml`.

### 2. Write the site file

The site file holds the hosts, the licences and the bot. It stays outside
the project and outside git, because it names your machines.

```toml
schema = 1
scratch = ["/scratch"]

[hosts.hostA]
cores = 64
ram_gb = 256

[hosts.hostB]
cores = 128
ram_gb = 512
```

Add `[licences.<name>]` when a stage has to wait for seats, and
`[telegram]` when you want alerts on your phone. `docs/configuration.md`
lists every key.

### 3. Declare the flow

Edit `edr.toml`: the repository under `[source]`, one `[stages.<name>]`
per command of the flow, and the `[metrics.<name>]` you want in the
ledger. Stages run in file order. `docs/flows.md` shows two real flows,
and `docs/configuration.md` explains stages, steps, task groups and
metrics.

### 4. Check

```sh
edr check
```

`check` loads every file, imports the hooks, probes every host, names
every tool the head node lacks, and plans every batch under `jobs/`. It
prints one `problem:` line per fault and exits 1, or `ok: 2 hosts, 3
stages, 5 metrics, 1 batches` and exits 0.

### 5. Stage the source

```sh
edr stage origin/main
```

This prints `<src> <path>`: the short hash of the commit and a detached
worktree at `<worktrees>/<src>`. Every run of that source works on a copy
of this tree, so a later commit never changes a running flow. A tree
with uncommitted changes goes in with `edr stage --dirty <dir>`; its tag
is `<hash>-dirty-<8 hex>`, and `launch` needs `--allow-dirty` for it.

### 6. Write a batch

```toml
# jobs/sweep1.toml
batch = "sweep1"
source = "3f9a2c1"

[[job]]
label = "base"
config = "base"

[[job]]
label = "base_dw0"
config = "base"
overrides = { DW = 0 }
```

`source` is the tag that `edr stage` printed. A job is one run: a label,
a configuration name the flow understands, optional overrides that
become `KEY=VALUE` tokens in the command, and optional `stages` and
`tasks` lists. The run id is `<date>_<label>_<build_tag>_g<src>`.

### 7. Plan and launch

```sh
edr plan sweep1               # run id, host and root per job; writes nothing
edr launch sweep1 --dry-run   # every path and command, nothing written
edr launch sweep1
```

Read every path of the dry run before the real launch. `launch` copies
the driver into the state directory, syncs the staged tree to each host,
writes one spec per run and starts one driver per run. A job that no host
fits is queued, and the watcher starts it when a host frees up. Confirm
within a minute that `edr status` shows a phase past `setup`.

### 8. Watch

```sh
edr status                    # the board
edr status --live             # asks each host whether the driver exists
edr status base@sweep1        # one run: stages, metrics, log tail
edr status --triage           # every run not running, with one proposed command
```

### 9. Run the watcher

`edr watch` classifies the runs, collects the results, extracts the
metrics, starts queued jobs and sends the alerts. It runs on the head
node, one process per project. As a systemd user service:

```sh
cp edr-watch.service ~/.config/systemd/user/edr-myflow.service
systemctl --user daemon-reload
systemctl --user enable --now edr-myflow
loginctl enable-linger "$USER"     # the service survives a logout and a reboot
```

Without systemd, `tmux new -d -s edr-myflow 'edr watch'` does the same
job. In both cases a cron line tells you when the watcher stopped:

```
*/10 * * * * cd ~/myflow && edr watch --check
```

`--check` exits 1 and sends an alert when the watcher has not written its
own heartbeat for three cycles. `docs/watcher.md` explains the cycle.

### 10. Read the results

```sh
edr metrics --design 3f9a2c1
edr export --design 3f9a2c1 --out exports/3f9a2c1
```

One table holds one design, so `--design` has no default. `docs/results.md`
explains the ledger and the snapshot.

### 11. Clean up

```sh
edr retire --batch sweep1 --dry-run --why "exported"
edr retire --batch sweep1 --why "exported"
```

`retire` removes the run trees on the hosts after a guard on every path,
and refuses a tree whose results are not collected. The ledger keeps the
runs, the metrics and the events. `--collect netlist` copies the larger
files of a `collect_on_request` list to the head node first; the section
"Archive, then clear the hosts" in `docs/running.md` shows the whole
sequence and the rerun from the archive.

### 12. A second project

One project is one directory with an `edr.toml`. A second flow, on
another repository, gets its own directory, and with it its own ledger
and results under `data/`, its own state directory `~/.edr/<project>`,
its own trees under `<scratch>/<user>/edr/<project>/` and its own
watcher unit. The site file is shared. Nothing of one project appears in
the tables of another, and `edr` in a directory sees that project only.
With one Telegram bot for both, set `telegram_poll = false` in one of
them; `docs/telegram.md` says why.
