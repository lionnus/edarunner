# Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request.

| Job | Runs |
|---|---|
| `tests` | `pytest` with coverage on Python 3.11 and 3.12. The run fails under 88 % (measured: 93 %). The 3.11 run uploads `coverage.svg`. |
| `driver` | `tests/test_driver.py` in a `python:3.6` container, the floor of the compute hosts. The image's interpreter is linked to `/usr/bin/python3`, the fallback of `tests/helpers_driver.py` and of `test_compiles_on_py36`. |
| `openroad` | `examples/openroad-gcd/run.sh` in the `openroad/orfs` image: the GCD design through synth, floorplan and place under `edr`. About 2 min, 1 min of it the image pull. |
| `status` | after every other job, also after a failure: writes the outcome into the branch `ci-status`. |

## The ci-status branch

`status` force-pushes an orphan branch `ci-status` with `GITHUB_TOKEN`.
The branch has one commit and holds these files.

| File | Holds |
|---|---|
| `status.json` | the commit, the run id and url, the timestamp, the result and the steps of every job, and the conclusion |
| `coverage.svg` | the badge of the `tests` job |
| `openroad/` | `run.log`, `metrics.csv` and `files.txt` of the OpenROAD run |
| `logs/` | the log of every job, from the Actions API |

Read a result without the Actions UI:

```sh
git fetch origin ci-status
git show origin/ci-status:status.json
```

Wait for the run of the checked-out commit; a run takes several minutes:

```sh
until git fetch -q origin ci-status && git show origin/ci-status:status.json | grep -q "$(git rev-parse HEAD)"; do sleep 60; done
```

Every branch writes the same branch, so `ci-status` shows the newest run
of the repository, not the newest run of one branch.
