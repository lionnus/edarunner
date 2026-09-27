# Install and first run

After this page `edr` is on your head node, a real OpenROAD flow ran
under it on one machine, and you know what a compute host needs.

## Install the controller

`edr` runs on the head node, the machine you work from. It needs Python
3.11 or newer; `rich`, its one dependency, comes with the install.

```sh
uv tool install git+https://github.com/lionnus/edarunner
```

`pipx install git+https://github.com/lionnus/edarunner` does the same. In
a checkout, `uv venv --python 3.11 .venv && uv pip install -e '.[dev]'`
gives you `.venv/bin/edr`.

`edr` works from any directory below an `edr.toml`. Without one it stops
with `no edr.toml in <dir> or above; run edr init`.

## Five minutes on one machine

`examples/openroad-gcd` takes the GCD design of OpenROAD-flow-scripts
(ORFS) through Yosys and OpenROAD on the head node. You need `edr`, git,
and either an ORFS install or the `openroad/orfs` image that CI uses. With
Docker, start the image in a clone of the repository and install `edr`
inside it:

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
edr launch gcd                # one driver on the host `local`
edr status                    # the board
edr watch --once              # collect the reports and extract the metrics
edr metrics --design <src>    # <src> is the hash that `edr checkout` printed
```

Synthesis, floorplan and placement take about half a minute. Once the
board says `done`, `edr watch --once` collects the reports, and
`edr metrics` prints the area and the setup slack of each stage.
`examples/openroad-gcd/README.md` says which report each number comes
from.

## The complete setup

[edarunner-example](https://github.com/lionnus/edarunner-example) is a
complete setup split the way a lab would split it: a site template with
the hosts, the licences and the bot, and a project that takes the Croc
SoC from RTL to GDS. Copy the site once per lab and the project once per
design.

## Without any EDA tool

`examples/local-demo` runs a scripted stand-in for a flow, so it needs no
EDA tool and no licence. The tests use it, and it is the quickest way to
try a failure, a resume or the seat gate. The commands are the same as
above with the batch `demo`:

```sh
cd examples/local-demo && bash setup.sh
edr checkout HEAD && edr launch demo
```

The two runs end `done` within a minute. `examples/local-demo/README.md`
says what the flow fakes, where the files land and how to make a run
fail. `edr retire --batch demo --why "demo done"` removes the run trees
at the end.

## What the head node needs

- Linux with GNU coreutils and procps-ng 3.3.10 or newer.
- Python 3.11 or newer for the controller, with the `rich` package for
  the tables; `uv tool install` brings it.
- `ssh` with keys that work under `BatchMode=yes`: no password prompt and
  no host key prompt for any host.
- `rsync`, `git` and `python3` on `PATH`, and `nproc`, `df`, `ps`, `awk`,
  `stat` and `readlink` for the host `local`.
- The `state_dir` directory of `edr.toml` on a filesystem that every host
  mounts. The hosts read the driver and the spec from it; the head node
  reads the heartbeats.

## What a compute host needs

Nothing is installed on a host. The driver is one file that `edr launch`
copies into the state directory, and the host runs it with its own
`python3`. Every host in `[hosts]` of the site file needs:

- Linux with `/proc`. The probe reads `/proc/loadavg` and
  `/proc/meminfo`; the orphan check reads `/proc/<pid>/cwd`.
- A POSIX `sh`. Every remote command runs under `sh -c`, so the login
  shell can be `tcsh` or `csh`.
- GNU coreutils: `nproc`, `df -Pk`, `stat -c %s`, `readlink`, `nohup`.
- procps-ng 3.3.10 or newer: `ps -o etimes,pcpu,cputimes` and `ps -o pgid`.
- util-linux `setsid`. The driver starts in its own session.
- `python3` 3.6 or newer on the login `PATH`. The driver uses the
  standard library only.
- `rsync` for the copy of the source tree and the collect of the results.
- `awk`, and `kill` from the shell.
- `nvidia-smi` on `PATH` when the host has GPUs to report; without it the
  probe reports none.

`edr check` names every tool the head node lacks and every host that does
not answer its probe. Run it after an install and after a host change.

## Next

[concepts.md](concepts.md) names the parts you just used.
[configure.md](configure.md) turns your own flow into a project.
