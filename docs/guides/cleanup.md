# Clean up the hosts

This page shows how to free the scratch disks without losing a result.
You retire runs and batches, prune the large parts of a tree, keep the
files you may need later on the head node, and start a rerun from that
archive. edr removes none of your files on its own, so every step here
is a command you run.

## Collect first

A retire refuses a tree whose results are not collected yet, so let the
watcher finish its work before you clean up:

```sh
edr watch --once              # the reports and logs of every finished run
```

`data/results/<run_id>/` then holds the logs and the `collect` paths of
every stage, and the database holds the metrics. That is what an
analysis, a review or `edr export` reads; the run tree on the host is
no longer needed for them.

## Retire a run

```sh
edr retire base@sweep1 --why "superseded by sweep2" --dry-run
edr retire base@sweep1 --why "superseded by sweep2"
```

`retire` removes the run tree on the host with `rm -rf`, after the path
passes the guard that [how-it-works.md](../how-it-works.md#launch)
describes. The dry run prints every target. `retire` refuses a run whose
driver is alive, a tree that another live run uses, a tree shared with a
run whose results are not collected, and a tree whose own results are
not collected unless you pass `--uncollected`.
[reference/cli.md](../reference/cli.md) lists every refusal and flag.

`--why` is required, and the text goes into the event log. The run keeps
its rows in the database and its files under `data/results/`, with the
state `retired`.

## Prune part of a tree

`edr retire <handle> --prune <name> --why <text>` removes only the paths
that `prune.<name>` names in the stages, and keeps the rest of the tree:

```toml
[stages.pnr]
prune = { lib = ["flow/runs/{tree_id}/out/library"] }
```

This frees the space of a large library or build directory while the
reports and the checkpoints stay, so `edr continue` can still build on
the tree.

## Free a full host

`--host` prunes the finished runs of the project on one host:

```sh
edr retire --host hostA --prune lib --why "hostA full" --dry-run
edr retire --host hostA --prune lib --why "hostA full"
```

It takes every run on `hostA` that has ended, has a tree and is not
retired, and removes the paths of each `--prune` name; several names go
apart by commas. The live runs keep their trees. The Free space button
of a `host_full` alert runs this command with every prune name of the
project, after a second tap.

## Retire a batch

```sh
edr retire --batch sweep1 --why "sweep done" --dry-run
edr retire --batch sweep1 --why "sweep done"
```

`--batch` retires every run of the batch and writes the `RETIRED` file,
so the watcher and the board leave the batch alone. It then removes the
checked-out source of the batch under `source.worktrees`, unless a batch
that is not retired uses the same source. When that tree does not pass
the guard, for example because `source.worktrees` lies outside the
marker path, `retire` keeps it, prints a `worktree kept` line and still
removes the run trees; you can remove the checked-out tree by hand.

## Keep the large files

Some files are too large to collect from every run, such as a netlist,
a parasitics file or a waveform, yet you may need them for a later
analysis or a rerun. Name them once per stage under
`collect_on_request`:

```toml
[stages.export]
collect_on_request = { netlist = ["out/15/"], power_inputs = ["out/15/", "sdc/", "spef/"] }
```

Then fetch them before the tree goes:

```sh
edr retire --batch sweep2 --collect netlist,power_inputs --why "project done" --dry-run
edr retire --batch sweep2 --collect netlist,power_inputs --why "project done"
```

The dry run prints the file count of every list and every `rm -rf`
target. The real run copies first and deletes nothing when a copy
failed, so a tree is gone only once its files are on the head node. The
`artifacts` table records every copied file with its class, `always` or
the name of the list. `edr continue <handle> --collect <name>` copies a
list without a retire.

## Rerun from the archive

A job can start from the archive of an earlier run through `restore` on
`reuse`:

```toml
[[job]]
label = "base"
config = "base"
stages = ["power"]
tasks = ["softmax_197"]
reuse = { label = "base", latest = true, restore = "power_inputs" }
```

`edr launch` checks out the source tag of the archived run, syncs a
fresh tree to a host that fits, copies the `power_inputs` files of the
archived run into it at their old paths, and starts the stage. The new
run keeps the `tree_id` and the build tag of the archived one, so every
path in the flow resolves as before.

A result whose tree is gone and which edarunner never collected can
still join the database through `edr import --results`;
[run.md](run.md#import-a-run) shows it.
