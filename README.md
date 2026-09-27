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
  a snapshot that a paper reads.

A project is a few TOML files: `edr.toml`, `tasks.toml`,
`jobs/<batch>.toml` and a private `site.toml`. The core knows no EDA tool.
Markdown contracts, `AGENTS.md` and the per-directory `CLAUDE.md` of the
paper layout, let an agent set up a project and operate it.

## Install

```sh
uv tool install git+https://github.com/lionnus/edarunner   # the controller, Python 3.11 or newer; PyPI follows the first release
```

A compute host needs ssh, `rsync` and `python3` 3.6 or newer. Nothing is
installed there.

## Five minutes on one machine

```sh
git clone https://github.com/lionnus/edarunner && cd edarunner/examples/local-demo
bash setup.sh                 # a fake flow in a small git repository
edr checkout HEAD             # a pinned worktree of the source; prints its short hash
edr check                     # load the config, probe the hosts, check the hooks
edr plan demo                 # run ids, hosts, every path; writes nothing
edr launch demo               # one driver per run on the `local` host
edr status                    # the board
edr watch --once              # collect, extract metrics, classify
edr metrics --design <src> --csv   # <src> is the hash that `edr checkout` printed
```

The flow is fake and needs no EDA tool or licence. The two runs end
`done` within a minute. `examples/local-demo/README.md` explains the demo.

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

## How it works

<p align="center"><img src="docs/diagrams/where-it-runs.svg" alt="Where each part runs: the head node, the shared filesystem, the compute hosts and the phone" width="900"></p>

`edr.toml` declares the flow as stages that run in order. `edr launch`
copies a one-file driver to the host and starts it. The driver runs the
stages and writes a heartbeat file every minute. `edr watch` on the head
node reads the heartbeats, collects the reports, extracts the metrics
and sends the alerts. It never deletes a file.

## Documents

- `docs/README.md` is the index of the documentation; the same pages are published at <https://lionnus.github.io/edarunner/>.
- `AGENTS.md` tells an agent how to operate the farm through `edr`.

## Contributing

edarunner is alpha. It runs a commercial flow and an open OpenROAD flow.
Read `CONTRIBUTING.md` before you open a pull request.

## Licence

Apache-2.0. See `LICENSE`.
