# The local demo

This project runs a stand-in flow of shell scripts that write example
reports, on the head node alone. It needs no EDA tool, no licence and no
second host. The unit tests,
`tests/test_e2e_local.py` and the condor and slurm CI jobs run it. For a first
run with real tools, take `examples/openroad-gcd` instead.

## What the scripts stand in for

| Script | Stands in for | Does |
|---|---|---|
| `flow/flow.sh <stage> <run_id> <config> [FIRST_STAGE=x] [LAST_STAGE=y] [NETLIST_STAGE=n] [KEY=VALUE ...]` | synthesis, place and route, export | one step per second; writes `reports/<n>/area.rpt` and `reports/<n>/qor.rpt`; `export` writes `out/<NETLIST_STAGE>/netlist.v` (`NETLIST_STAGE` defaults to 11) |
| `flow/kernel.sh <kernel> <test> [KEY=VALUE ...]` | a gate-level power simulation | sleeps `DEMO_SLEEP` seconds (default 2); writes a 1 MiB `wave.vcd`, `power/reports/power.csv` and `power/phases.json` |
| `flow/seats.sh` | the seat probe of the tool `demo` | prints `free total`; 10 seats, `DEMO_SEATS_USED` in use (default 2) |

The steps are `setup`, `analyze`, `elaborate` and `synth` (stage `synth`),
then `cts` and `route` (stage `pnr`), then `export`. `FIRST_STAGE` makes a
resume, and `LAST_STAGE` an early end. The numbers are made up: an area of
`1000 + 10.5 * step` um2, a power of 0.250 W and a window of 3400 ns, so
the energy is 850 nJ. `qor.rpt` has one block per scenario and path
group, as a `report_qor` has: a hold block first, then the setup groups
`in2reg` and `reg2reg`. At each step the worst setup slack is
`-0.0<step>` ns, and `<step>` setup paths fail, so every step after the
first fails the `pass` rule of `setup_violations`.

Two switches make failures:

- The config `fail_licence` makes step 2 fail once with `license checkout
  failed` in the log; the retry then passes.
- The kernel `bad`, task `k_bad`, fails with `boom`.

## The files

| File | Holds |
|---|---|
| `edr.toml` | the stages `synth`, `pnr`, `export` and the task group `power`; six metrics, two of them tied to the setup scenario of `qor.rpt`, and the area with its step of record at `route`; short limits (heartbeat 5 s, stale 30 s, dead 90 s) |
| `site.toml` | one host, `local`, with the tool `demo` in version `1.0`; the scratch `/tmp/edr-demo`; the tool `demo` with 10 seats and its probe |
| `hooks/energy.py` | the metric hook of `energy_nj`: the power times the window of one task |
| `tasks.toml` | `k_small`, `k_big` (budget 2 h), `k_bad` |
| `jobs/demo.toml` | job `a`: every stage, tasks `k_small` and `k_big`, and `vars = { netlist_stage = 11 }` for `export`; job `b_nodw`: `synth` and `pnr` with `DW=0` |
| `setup.sh` | creates `repo/`, a git repository that holds `flow/`; the batch checks out its source from it |

The run makes `repo/`, `wt/` and `data/`, and git ignores them.

## The five-minute run

```sh
cd examples/local-demo
bash setup.sh                 # repo/ with one commit
edr checkout HEAD             # <source> and the path of the pinned clone
edr check                     # load the config, probe the host, check the hooks
edr plan demo                 # one run id, host and root per job; writes nothing
edr launch demo               # 2 started, 0 queued, 0 with problems
edr status --watch            # redraws every 5 s; Ctrl-C to leave
```

Both runs end `done` within a minute. Then collect and read the results:

```sh
edr watch --once              # collect the reports, extract the metrics, write data/board/
edr status a@demo             # the stages, the metrics and the log tail of one run
edr metrics --run b_nodw@demo --over steps   # the steps of b_nodw and the verdict of each
edr compare a@demo b_nodw@demo   # both runs side by side, the area at its step of record
edr metrics --source <source> --csv
edr export --source <source> --out data/exports/<source>
edr retire --batch demo --why "demo done"
```

`<source>` is the short hash that `edr checkout` printed. `edr metrics` needs
it, because one table holds one source.

The run puts its files in these places:

| Path | Holds |
|---|---|
| `wt/<source>/` | the checked-out clone |
| `/tmp/edr-demo/<user>/edr/demo/<run_id>/` | the run tree; `log/` holds one file per stage and task |
| `~/.edr/demo/demo/` | `RUN_DATE`, the specs, the heartbeats, the queues, the driver log |
| `~/.edr/demo/bin/edr_driver-<hash>.py` | the driver, one copy per driver version |
| `data/edr.db`, `data/results/`, `data/board/` | the project database, the collected files, `status.html` and `compare.html` |

## Try a failure

A batch name is used once, so each try gets a new job file:

```sh
sed 's/^batch = .*/batch = "gate"/' jobs/demo.toml > jobs/gate.toml
DEMO_SEATS_USED=10 edr launch gate   # no free seat: the gate blocks
edr status --triage                  # after 30 s: failed, FAILED:synth, exit 4
```

To make a task fail, add `k_bad` to the tasks of job `a` in another copy:

```sh
sed -e 's/^batch = .*/batch = "bad"/' -e 's/"k_big"]/"k_big", "k_bad"]/' jobs/demo.toml > jobs/bad.toml
edr launch bad
```

The run `a@bad` ends `INCOMPLETE:1f0s0h` with exit 8, and `edr status a@bad`
shows the signature `boom: kernel bad failed`.

## Clean up

`edr retire --batch <batch>` removes the run trees of a batch and marks
it `RETIRED`. Collect the results first, then retire every batch you
launched, including the ones from "Try a failure":

```sh
edr watch --once
edr retire --batch demo --why "demo done"
edr retire --batch gate --why "demo done"
edr retire --batch bad --why "demo done"
```

The state, the database, the source, the clones and the extra job
files stay. Remove them by hand:

```sh
rm -rf ~/.edr/demo data repo wt
rm -f jobs/gate.toml jobs/bad.toml
```
