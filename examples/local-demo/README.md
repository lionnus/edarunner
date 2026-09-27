# The local demo

This project runs on the head node alone, with a fake flow. Every `edr`
command works here without an EDA tool, a licence or a second host. The
tests build on it, and it is the shortest way to see the tool at work.

## What the flow fakes

| Script | Stands in for | Does |
|---|---|---|
| `flow/flow.sh <stage> <run_id> <config> [FIRST_STAGE=x] [LAST_STAGE=y] [NETLIST_STAGE=n] [KEY=VALUE ...]` | synthesis, place and route, export | one step per second; writes `reports/<n>/area.rpt` and `reports/<n>/qor.rpt`; `export` writes `out/<n>/netlist.v`, 11 by default |
| `flow/kernel.sh <kernel> <test> [KEY=VALUE ...]` | a gate-level power simulation | sleeps `DEMO_SLEEP` seconds (default 2); writes a 1 MiB `wave.vcd`, `power/reports/power.csv` and `power/phases.json` |
| `flow/seats.sh` | the seat probe of the tool `demo` | prints `free total`; 10 seats, `DEMO_SEATS_USED` in use (default 2) |

The steps are `setup`, `analyze`, `elaborate` and `synth` (stage `synth`),
then `cts` and `route` (stage `pnr`), then `export`. `FIRST_STAGE` makes a
resume, and `LAST_STAGE` an early end. The numbers are made up: an area of
`1000 + 10.5 * step` um2, a slack of `-0.0<step>` ns, a power of 0.250 W
and a window of 3400 ns, so the energy is 850 nJ.

Two switches make failures:

- The config `fail_licence` makes step 2 fail once with `license checkout
  failed` in the log; the retry then passes.
- The kernel `bad`, task `k_bad`, fails with `boom`.

## The files

| File | Holds |
|---|---|
| `edr.toml` | the stages `synth`, `pnr`, `export` and the task group `power`; five metrics; short limits (heartbeat 5 s, stale 30 s, dead 90 s) |
| `site.toml` | one host, `local`, with the tool `demo` in version `1.0`; the scratch `/tmp/edr-demo`; the tool `demo` with 10 seats and its probe |
| `hooks/energy.py` | the metric hook of `energy_nj`: the power times the window of one task |
| `tasks.toml` | `k_small`, `k_big` (budget 2 h), `k_bad` |
| `jobs/demo.toml` | job `a`: every stage, tasks `k_small` and `k_big`, and `vars = { netlist_stage = 11 }` for `export`; job `b_nodw`: `synth` and `pnr` with `DW=0` |
| `setup.sh` | makes `repo/`, a git repository with `flow/`; the source the batch stages |

The run makes `repo/`, `wt/` and `data/`, and git ignores them.

## The five-minute run

```sh
cd examples/local-demo
bash setup.sh                 # repo/ with one commit
edr check                     # ok: 1 hosts, 4 stages, 5 metrics, 1 batches
edr checkout HEAD             # <src> and the path of the pinned worktree
edr plan demo                 # one run id, host and root per job; writes nothing
edr launch demo               # 2 started, 0 queued, 0 with problems
edr status --watch            # redraws every 5 s; Ctrl-C to leave
```

Both runs end `done` within a minute. Then:

```sh
edr watch --once              # collect the reports, extract the metrics, write data/board/
edr status a@demo             # the stages, the metrics and the log tail of one run
edr metrics --design <src> --csv
edr export --design <src> --out data/exports/<src>
edr retire --batch demo --why "demo done"
```

`<src>` is the short hash that `edr checkout` printed. `edr metrics` needs
it, because one table holds one design.

Where things land:

| Path | Holds |
|---|---|
| `wt/<src>/` | the checked-out worktree |
| `/tmp/edr-demo/<user>/edr/demo/<run_id>/` | the run tree; `log/` holds one file per stage and task |
| `~/.edr/demo/demo/` | `RUN_DATE`, the specs, the heartbeats, the queues, the driver log |
| `~/.edr/demo/bin/demo/edr_driver.py` | the driver copy of the batch |
| `data/edr.db`, `data/results/`, `data/board/` | the run database, the collected files, `status.html` and `compare.html` |

## Try a failure

A batch name is used once, so each try gets a new job file:

```sh
sed 's/^batch = .*/batch = "gate"/' jobs/demo.toml > jobs/gate.toml
DEMO_SEATS_USED=10 edr launch gate   # no free seat: the gate blocks
edr status --triage                  # after 30 s: failed, FAILED:synth, exit 4
```

Add `"k_bad"` to the `tasks` of job `a` in a copy and launch it. The run
ends `INCOMPLETE:1f0s` with exit 8, and `edr status a@<batch>` shows the
signature `boom: kernel bad failed`.

## Clean up

`edr retire --batch demo --why "demo done"` removes the run trees and
marks the batch `RETIRED`. The state, the database, the source and the
worktrees stay:

```sh
rm -rf ~/.edr/demo data repo wt
```
