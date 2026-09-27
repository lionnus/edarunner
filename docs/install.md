# Get started

This page installs `edr` and runs a real OpenROAD flow with it on one
machine. After it you have seen a run go from a commit to the numbers in
the database, and you know where to go next.

## Install

`edr` runs on the head node, the machine you work on. It needs Python
3.11 or newer; its only dependency, `rich`, comes with the install.

```sh
uv tool install git+https://github.com/lionnus/edarunner
```

`pipx install git+https://github.com/lionnus/edarunner` does the same. In
a checkout of the repository, `uv venv --python 3.11 .venv && uv pip
install -e '.[dev]'` gives you `.venv/bin/edr`.

The head node also needs Linux with GNU coreutils and procps-ng 3.3.10 or
newer, `rsync`, `git` and `python3` on `PATH`, and ssh keys that work
under `BatchMode=yes` for every host you add later, with no password
prompt and no host key prompt. The first run below uses the head node
only, so it needs no ssh at all.

`edr` works from any directory below an `edr.toml`. Without one it stops
with `no edr.toml in <dir> or above; run edr init`.

## Run the OpenROAD example

`examples/openroad-gcd` takes the GCD design of OpenROAD-flow-scripts
(ORFS) through Yosys and OpenROAD on the head node. You need `edr`, git,
and either an ORFS install or the `openroad/orfs` image that CI uses.
With Docker, start the image in a clone of the repository and install
`edr` inside it:

```sh
git clone https://github.com/lionnus/edarunner && cd edarunner
docker run --rm -it -v "$PWD":/edarunner -w /edarunner openroad/orfs:26Q3-657-gb74a7293e bash
apt-get update -qq && apt-get install -y -qq rsync     # the image has no rsync
curl -LsSf https://astral.sh/uv/install.sh | sh && . ~/.local/bin/env
uv tool install /edarunner
```

With ORFS installed on the machine, skip the container and set `ORFS` to
your checkout. `examples/openroad-gcd/README.md` shows the same run with
Singularity. Then run the flow:

```sh
cd examples/openroad-gcd
bash setup.sh                 # a small git repository with the design config
edr checkout HEAD             # a pinned clone of the source; prints its short hash
edr check                     # load the config, probe the hosts, check the hooks
edr plan gcd                  # run ids, hosts, every path; writes nothing
edr launch gcd                # one driver on the host `local`
edr status                    # the board
edr watch --once              # collect the reports and extract the metrics
edr metrics --design <src>    # <src> is the hash that `edr checkout` printed
```

## What you see

Synthesis, floorplan and placement take about half a minute. `edr
status` draws the board, one line per run, and the run reaches `done`:

```text
#   label  host   state  phase  stage/step  age  fail/done  cost
────────────────────────────────────────────────────────────────
#1  gcd    local  done   done   place        0m      0f/0d   0.0
```

`edr watch --once` runs one cycle of the watcher. It copies the reports
from the run tree into `data/results/` and reads the numbers out of
them. `edr metrics` then prints the area and the setup slack of each
stage:

```text
label  design   stage      step  task  metric                  value  unit
──────────────────────────────────────────────────────────────────────────
gcd    952ceeb  floorplan     -        area_floorplan_um2     698.25  um2
gcd    952ceeb  floorplan     -        wns_floorplan_ns    -0.155306  ns
gcd    952ceeb  place         -        area_place_um2        827.526  um2
gcd    952ceeb  place         -        wns_place_ns        -0.151547  ns
gcd    952ceeb  synth         -        area_synth_um2        626.696  um2
```

The synthesis area comes from the Yosys report, and the other numbers
come from the metrics JSON that OpenROAD writes at each step.
`examples/openroad-gcd/README.md` says which report each number comes
from, and `examples/openroad-gcd/edr.toml` is the whole configuration.
The design column holds the short hash of the commit that `edr checkout`
pinned, so every number stays tied to the source it came from.

## Next

[how-it-works.md](how-it-works.md) explains what happened between
`checkout` and `metrics`, and [guides/project.md](guides/project.md)
turns your own flow into a project.

[edarunner-example](https://github.com/lionnus/edarunner-example) is a
complete setup split the way a lab would split it: a site template with
the hosts, the licences and the bot, and a project that takes the Croc
SoC from RTL to GDS.

`examples/local-demo` runs a stand-in flow of shell scripts that write
example reports, so it needs no EDA tool and no licence. It is the
quickest way to try a failure, a resume or the licence gate:

```sh
cd examples/local-demo && bash setup.sh
edr checkout HEAD && edr launch demo
```

The two runs end `done` within a minute. `examples/local-demo/README.md`
says what each script stands in for and how to make a run fail. At the
end, `edr watch --once` collects the results and
`edr retire --batch demo --why "demo done"` removes the run trees.
