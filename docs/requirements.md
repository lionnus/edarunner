# Requirements

`edr check` names every tool the head node lacks and every host that does
not answer its probe. Run it after an install and after a host change.
Nothing is installed on a host; the driver is one file that `edr launch`
copies into the state directory.

## Head node

The machine that runs `edr` and the watcher.

- Linux with GNU coreutils and procps-ng 3.3.10 or newer.
- Python 3.11 or newer for the controller, with the `rich` package for
  the tables; `uv tool install` brings it.
- `ssh` with keys that work under `BatchMode=yes`: no password prompt and
  no host key prompt for any host.
- `rsync`, `git` and `python3` on `PATH`, and `nproc`, `df`, `ps`, `awk`,
  `stat` and `readlink` for the host `local`. `check` names each one
  that is missing.
- The `state` directory of `edr.toml` on a filesystem that every host
  mounts. The hosts read the driver and the spec from it; the head node
  reads the heartbeats.

## Compute host

Every host in `[hosts]` of the site file, and `local`, the head node.

- Linux with `/proc`. The probe reads `/proc/loadavg` and
  `/proc/meminfo`; the orphan check reads `/proc/<pid>/cwd`.
- A POSIX `sh`. Every remote command runs under `sh -c`, so the login
  shell can be `tcsh` or `csh`.
- GNU coreutils: `nproc`, `df -Pk`, `stat -c %s`, `readlink`, `nohup`.
- `nvidia-smi` on `PATH` when the host has GPUs to report; without it the
  probe reports none.
- procps-ng 3.3.10 or newer: `ps -o etimes,pcpu,cputimes` and `ps -o pgid`.
- util-linux `setsid`. The driver starts in its own session.
- `python3` 3.6 or newer on the login `PATH`. The driver uses the
  standard library only.
- `rsync` for the sync of the source tree and the collect of the results.
- `awk`, and `kill` from the shell.
