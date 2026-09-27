# Scheduler harness

`condor.sh` and `slurm.sh` each start a scheduler in containers and run
the local demo through it with `edr`; `demo.sh` holds the part they
share. CI runs both; the `condor` and `slurm` jobs of
`.github/workflows/ci.yml` call them.

| Script | Scheduler | Runtime |
|---|---|---|
| `condor.sh` | a one-machine HTCondor pool from the `htcondor/mini` image | docker, or singularity as a personal pool |
| `slurm.sh` | the `giovtorres/slurm-docker-cluster` compose setup: controller, database and two workers | docker with compose |
| `demo.sh` | the part both share: the project, the batch and the checks | sourced |

Each script makes a copy of `examples/local-demo` under `WORK` with a
site file for its backend, and a batch `h` of three jobs. Jobs `a` and
`b` run `synth` and `pnr` and need the tool `demo`, whose licence `fc`
has one seat in the scheduler. Job `c` runs `sleep 600`. The script
launches the batch, stops `c` with `edr stop`, runs `edr watch --once`
until every run ends, and checks:

- `a` and `b` end `done`, and their stages do not overlap;
- `c` ends `KILLED`;
- `edr check` passes, which includes the licence check.

It prints `failures: N` and exits 1 when N is above 0. The container
stops on exit, by its exact name.

## Run it on a desk machine

Install edarunner into a virtualenv first; `EDR` names its `edr`.
`WORK` must end in `/harness/work`, because the script deletes it at
the start. The default is `tools/harness/work`, which git ignores.

With docker:

```sh
EDR=$PWD/.venv/bin/edr bash tools/harness/condor.sh
EDR=$PWD/.venv/bin/edr bash tools/harness/slurm.sh
```

With singularity, HTCondor runs as a personal pool of the calling user.
The pool binds 127.0.0.1 on a free port, reads only the config the
script writes, and authenticates with the filesystem, so it reaches no
other pool and no other user reaches it:

```sh
RUNTIME=singularity EDR=$PWD/.venv/bin/edr bash tools/harness/condor.sh
```

The first run pulls the image to `tools/harness/mini.sif` (about
325 MB); `SIF` names another copy. The config sets `LIBEXEC`, because
the startd does not find its helpers without it. Slurm does not run
under singularity: `slurmd` needs root to start a job as a user.
