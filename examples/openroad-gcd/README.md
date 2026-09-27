# The OpenROAD GCD example

A real open flow under `edr`: the GCD design of OpenROAD-flow-scripts (ORFS)
on the nangate45 platform, through `synth`, `floorplan` and `place`, on the
head node. CI runs it in the `openroad/orfs` image; the job `openroad` in
`.github/workflows/ci.yml` shows the steps.

## What it needs

The ORFS tools. `ORFS` names the checkout with built tools; the default
`/OpenROAD-flow-scripts` is the path in the image. On the head node: `edr`,
`git`, `rsync`, and a `python3` for the driver.

The flow stops after placement. CTS in the image needs AVX-512, and a CI
runner may lack it.

## The files

| File | Holds |
|---|---|
| `edr.toml` | three stages, one `make` target each, and five metrics |
| `site.toml` | one host, `local`, with the scratch `/tmp` |
| `jobs/gcd.toml` | one job, `gcd`, config `nangate45`, on `local` |
| `design/config.mk` | the design config of the run tree; it includes the ORFS one |
| `run.sh` | the whole run: the source repository, `stage`, `check`, `plan`, `launch`, `watch --once` until the run ends, `status`, `metrics`, `export` |

`repo/`, `wt/` and `data/` are made by the run, and git ignores them.

## The stages

Each stage is one `make` call in the ORFS flow directory, after `env.sh`:

```sh
make -C $FLOW_HOME DESIGN_CONFIG=<run tree>/config.mk WORK_HOME=<run tree> synth
```

`WORK_HOME` sends `logs/`, `reports/`, `results/` and `objects/` into the
run tree, so two runs never share a file and the ORFS tree stays clean. The
watcher collects `reports/` and `logs/`. A job `overrides` table becomes
`make` variables, so `{ CORE_UTILIZATION = "60" }` is a sweep point.

## The metrics

| Metric | File under the run tree | Key or pattern |
|---|---|---|
| `area_synth_um2` | `reports/nangate45/gcd/base/synth_stat.txt` | `Chip area for module '\gcd':` |
| `area_floorplan_um2` | `logs/nangate45/gcd/base/2_1_floorplan.json` | `floorplan__design__instance__area` |
| `wns_floorplan_ns` | `logs/nangate45/gcd/base/2_1_floorplan.json` | `floorplan__timing__setup__ws` |
| `area_place_um2` | `logs/nangate45/gcd/base/3_5_place_dp.json` | `detailedplace__design__instance__area` |
| `wns_place_ns` | `logs/nangate45/gcd/base/3_5_place_dp.json` | `detailedplace__timing__setup__ws` |

The JSON files are the METRICS2.1 output that `flow/scripts/flow.sh` asks
of every OpenROAD step with `-metrics`. The stage of a key is the prefix
before the first `__`.

## Run it

```sh
cd examples/openroad-gcd
bash run.sh
```

The three stages take about 30 s on two cores; the CI job takes about
2 min, 1 min of it the image pull.

`EDR` names the `edr` binary (default `edr` on `PATH`) and `CAP_S` the wait
for the run in seconds (default 900). The script fails when the run does
not end `done`, or when the area and the timing rows are missing from
`edr metrics --csv`. The export lands in `data/exports/<src>/`.
