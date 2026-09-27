#!/usr/bin/env bash
# A Slurm cluster in docker compose (giovtorres/slurm-docker-cluster), and the local demo through the
# slurm backend. Needs docker with compose, and root in the containers; singularity cannot run it.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TAG=${TAG:-3.0.0}
SLURM_VERSION=${SLURM_VERSION:-26.05.2}
WORK=${WORK:-$HERE/work}
EDR=${EDR:-edr}
PROJECT=edr-slurm

case $WORK in */harness/work) ;; *) echo "refuse: WORK=$WORK does not end in /harness/work"; exit 2 ;; esac
rm -rf "$WORK"
mkdir -p "$WORK"
git clone -q --depth 1 -b "$TAG" https://github.com/giovtorres/slurm-docker-cluster.git "$WORK/cluster"
docker pull -q "giovtorres/slurm-docker-cluster:$SLURM_VERSION-$TAG"
docker tag "giovtorres/slurm-docker-cluster:$SLURM_VERSION-$TAG" "slurm-docker-cluster:$SLURM_VERSION"

# The controller and the workers see WORK at the path the head node uses.
cat > "$WORK/cluster/docker-compose.override.yml" <<EOF
services:
  slurmctld:
    volumes: ["$WORK:$WORK"]
  cpu-worker:
    volumes: ["$WORK:$WORK"]
EOF
compose() { docker compose -p "$PROJECT" --project-directory "$WORK/cluster" "$@"; }
stop_pool() { compose down -v >/dev/null 2>&1 || true; }
trap stop_pool EXIT
SLURM_VERSION=$SLURM_VERSION compose up -d --no-build mysql slurmdbd slurmctld cpu-worker

VIA=(docker exec slurmctld)
idle=
for _ in $(seq 90); do
    [ "$("${VIA[@]}" sinfo -h -t idle -o %n 2>/dev/null | wc -l)" -ge 1 ] && { idle=1; break; }
    sleep 2
done
[ -n "$idle" ] || { echo "no idle slurm node after 180 s"; exit 1; }
"${VIA[@]}" sinfo
for w in $(docker ps --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=cpu-worker" -q); do
    docker exec "$w" python3 --version || docker exec "$w" dnf install -q -y python3
done

# The licence fc: one seat, so two jobs that each ask fc:1 run one after the other.
"${VIA[@]}" bash -c 'grep -q "^Licenses=" /etc/slurm/slurm.conf || echo "Licenses=fc:1" >> /etc/slurm/slurm.conf'
"${VIA[@]}" scontrol reconfigure
for _ in $(seq 30); do
    "${VIA[@]}" scontrol show lic fc >/dev/null 2>&1 && break
    sleep 1
done
"${VIA[@]}" scontrol show lic fc

BACKEND=slurm
SUBMIT_VIA=$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "${VIA[@]}")
# shellcheck source=demo.sh
source "$HERE/demo.sh"
rc=0
run_demo || rc=$?
"${VIA[@]}" sacct -X -n -P -o JobID,JobName,State,ExitCode,NodeList | sed 's/^/sacct: /' || true
exit $rc
