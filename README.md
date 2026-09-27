<p align="center"><img src="docs/logo.svg" alt="edarunner" width="300"></p>

<p align="center">Run the EDA flow you already have on the ssh hosts you already reach, and keep one database of every run.</p>

<p align="center">
<a href="https://github.com/lionnus/edarunner/actions/workflows/ci.yml"><img src="https://github.com/lionnus/edarunner/actions/workflows/ci.yml/badge.svg" alt="ci"></a>
<img src="https://github.com/lionnus/edarunner/blob/ci-status/coverage.svg?raw=true" alt="coverage">
<img src="https://img.shields.io/badge/python-3.11%2B-008000" alt="Python 3.11+">
<a href="https://lionnus.github.io/edarunner/"><img src="https://img.shields.io/badge/docs-lionnus.github.io-008000" alt="docs"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-008000" alt="Apache-2.0"></a>
</p>

edarunner runs, tracks and analyzes an EDA flow on shared ssh hosts.

- Run: it starts the flow you already have as stages on the hosts. It
  queues jobs, applies budgets, and a watcher finds a dead or stuck run.
- Track: one SQLite database holds every run with its source hashes, host,
  events, metrics and artifacts. The board and a Telegram bot show it.
- Analyze: it gives the metrics of one design, compares runs, and exports
  a snapshot that a report, a notebook or a dashboard reads.

A project is a few TOML files: `edr.toml`, `tasks.toml`,
`jobs/<batch>.toml` and a private `site.toml`. The core knows no EDA tool.
`AGENTS.md` lets an agent set up a project and operate it.

## Install

```sh
uv tool install git+https://github.com/lionnus/edarunner   # the controller, Python 3.11 or newer; PyPI follows the first release
```

A compute host needs ssh, `rsync` and `python3` 3.6 or newer. Nothing is
installed there.

## Five minutes on one machine

The first run takes the GCD design of OpenROAD-flow-scripts (ORFS) through
Yosys and OpenROAD on your own machine. You need edarunner, git, and either
an ORFS install or the `openroad/orfs` image that CI uses. With Docker, start
the image in a clone of this repository and install edarunner inside it:

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
edr checkout HEAD             # a pinned worktree of the source; prints its short hash
edr check                     # load the config, probe the hosts, check the hooks
edr plan gcd                  # run ids, hosts, every path; writes nothing
edr launch gcd                # one driver on the `local` host
edr status                    # the board
edr watch --once              # collect the reports and extract the metrics
edr metrics --design <src>    # <src> is the hash that `edr checkout` printed
```

Synthesis, floorplan and placement take about half a minute. Once the
board says `done`, `edr watch --once` collects the reports and
`edr metrics` prints the area and the setup slack of each stage:

```text
#   label  host   state  phase  stage/step  age  fail/done  cost
────────────────────────────────────────────────────────────────
#1  gcd    local  done   done   place        0m      0f/0d   0.0

label  design   stage      step  task  metric                  value  unit
──────────────────────────────────────────────────────────────────────────
gcd    952ceeb  floorplan     -        area_floorplan_um2     698.25  um2
gcd    952ceeb  floorplan     -        wns_floorplan_ns    -0.155306  ns
gcd    952ceeb  place         -        area_place_um2        827.526  um2
gcd    952ceeb  place         -        wns_place_ns        -0.151547  ns
gcd    952ceeb  synth         -        area_synth_um2        626.696  um2
```

The synthesis area comes from the Yosys report, and the other numbers
come from the metrics JSON that OpenROAD writes at each step. For a
complete setup with a site template and a project that takes the Croc SoC
from RTL to GDS, see
[edarunner-example](https://github.com/lionnus/edarunner-example).

## See the farm

`edr status` draws the board, one line per run.

```text
#   label  host   state    phase        stage/step  age  fail/done  cost
────────────────────────────────────────────────────────────────────────
#1  wide   hostB  stale    group:power  power/4     12m      1f/3d   9.6
#2  base   hostA  running  stage:pnr    pnr/4        1m      0f/0d   6.4
#3  small  hostA  done     done         export      35m      0f/4d   0.0
```

`edr hosts` shows the load and the free resources of each host. The
fullest host comes first.

```text
ok  host      cores            load      ram GB  mount       scratch GB               gpu   gpu GB  tools  runs
───────────────────────────────────────────────────────────────────────────────────────────────────────────────
🟠  hostA  🟠 52/64  ██████░░  51.5  🟢 120/256  /scratch   🟢 800/2000  █████░░░  🟡 1/4  290/320    4/2     2
🟠  hostB   🟢 3/32  █░░░░░░░   3.1   🟢 98/128  /scratch2  🟠 150/1000  ███████░       -        -    1/0     1
```

## From your phone

`edr watch` sends an alert when a run dies, hangs, fails or runs over its
budget. On Telegram the alert carries the next command and three buttons
to keep, acknowledge or stop the run. ntfy and mail get the same alerts,
with the commands written out. The bot also answers `/status`, `/hosts`,
`/events`, `/tools` and `/digest`. A site can add its own commands, such
as one that opens a Claude Code session in the project directory. Scripts
and hooks send their own messages with `edr notify`. See
[docs/telegram.md](docs/telegram.md) for the bot and
[docs/notify.md](docs/notify.md) for ntfy and mail.

## Operate it with an agent

Every command takes `--json` and prints one object with the exit code,
the data and the text a person would see. Every command that writes takes
`--dry-run`. `edr stop` and `edr retire` refuse to act without `--why`, and
the reason lands in the events table with the actor. `edr status --triage`
lists each run that needs attention with one proposed command.
[AGENTS.md](AGENTS.md) is the operating guide for an agent. The example
repository keeps a Claude Code setup next to the flow: a contract per
directory, a session-start hook and a skill.

## How it works

<p align="center"><img src="docs/diagrams/where-it-runs.svg" alt="Where each part runs: the head node, the shared filesystem, the compute hosts and the phone" width="900"></p>

`edr.toml` declares the flow as stages that run in order. `edr launch`
copies a one-file driver to the host and starts it. The driver runs the
stages and writes a heartbeat file every minute. `edr watch` on the head
node reads the heartbeats, collects the reports, extracts the metrics
and sends the alerts. It never deletes a tree, and removes no file but a
stale seat lease.

## Documents

`docs/README.md` is the index: one path for a user, from the install to
the results, and one for a contributor. The same pages are published at
<https://lionnus.github.io/edarunner/>. `AGENTS.md` tells an agent how to
operate the farm through `edr`.

## Contributing

edarunner is alpha. It runs a commercial flow and an open OpenROAD flow.
Read `CONTRIBUTING.md` before you open a pull request.

## Licence

Apache-2.0. See `LICENSE`.
