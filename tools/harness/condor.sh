#!/usr/bin/env bash
# A one-machine HTCondor pool in a container, and the local demo through the condor backend.
#
#   RUNTIME=docker       the htcondor/mini container; jobs run as a user with the caller's uid (CI)
#   RUNTIME=singularity  a personal pool as the calling user, bound to 127.0.0.1
#
# Nothing here contacts another pool: the singularity pool binds 127.0.0.1 on a free port and reads
# only its own config, and the docker pool lives in its own network namespace.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
RUNTIME=${RUNTIME:-docker}
IMAGE=${IMAGE:-htcondor/mini:latest}
SIF=${SIF:-$HERE/mini.sif}
NAME=${NAME:-edr-mini}
WORK=${WORK:-$HERE/work}
EDR=${EDR:-edr}

case $WORK in */harness/work) ;; *) echo "refuse: WORK=$WORK does not end in /harness/work"; exit 2 ;; esac
rm -rf "$WORK"
mkdir -p "$WORK"/{tmp,condor/local/{log,spool,execute,lock,run}}

# FC_LIMIT is the concurrency limit `fc`: one job at a time holds it.
EDR_CONF=$WORK/condor/99-edr.conf
cat > "$EDR_CONF" <<EOF
FC_LIMIT = 1
NUM_CPUS = 3
START = TRUE
SUSPEND = FALSE
PREEMPT = FALSE
KILL = FALSE
SCHEDD_INTERVAL = 2
NEGOTIATOR_INTERVAL = 2
NEGOTIATOR_CYCLE_DELAY = 2
EOF

if [ "$RUNTIME" = singularity ]; then
    [ -f "$SIF" ] || singularity pull "$SIF" "docker://$IMAGE"
    PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
    # The startd finds its helpers under LIBEXEC; the image's own config is not read here.
    cat > "$WORK/condor/condor_config" <<EOF
RELEASE_DIR = /usr
LIBEXEC = /usr/libexec/condor
LOCAL_DIR = $WORK/condor/local
LOG = \$(LOCAL_DIR)/log
SPOOL = \$(LOCAL_DIR)/spool
EXECUTE = \$(LOCAL_DIR)/execute
LOCK = \$(LOCAL_DIR)/lock
RUN = \$(LOCAL_DIR)/run
CONDOR_HOST = 127.0.0.1
NETWORK_INTERFACE = 127.0.0.1
COLLECTOR_HOST = 127.0.0.1:$PORT
USE_SHARED_PORT = False
DAEMON_LIST = MASTER COLLECTOR NEGOTIATOR SCHEDD STARTD
UID_DOMAIN = edr.local
FILESYSTEM_DOMAIN = edr.local
SEC_DEFAULT_AUTHENTICATION = REQUIRED
SEC_DEFAULT_AUTHENTICATION_METHODS = FS
ALLOW_READ = $USER@*
ALLOW_WRITE = $USER@*
ALLOW_ADMINISTRATOR = $USER@*
ALLOW_DAEMON = $USER@*
ALLOW_NEGOTIATOR = $USER@*
BASE_CGROUP =
DISCARD_SESSION_KEYRING_ON_STARTUP = False
include : $EDR_CONF
EOF
    # --cleanenv keeps a host CONDOR_CONFIG out; /tmp is ours, so the FS auth files stay in WORK.
    VIA=(singularity exec --cleanenv --no-home -B "$WORK/tmp:/tmp" -B "$WORK"
         --env "CONDOR_CONFIG=$WORK/condor/condor_config" "$SIF")
    "${VIA[@]}" condor_master -pidfile "$WORK/condor/master.pid" &
    stop_pool() {
        "${VIA[@]}" condor_off -master -fast >/dev/null 2>&1 || true
        sleep 3
        local pid; pid=$(cat "$WORK/condor/master.pid" 2>/dev/null || true)
        if [ -n "$pid" ] && [ "$pid" -gt 1 ] && kill -0 "$pid" 2>/dev/null; then kill "$pid"; fi
    }
elif [ "$RUNTIME" = docker ]; then
    docker run -d --name "$NAME" -v "$WORK:$WORK" -v "$EDR_CONF:/etc/condor/config.d/99-edr.conf:ro" "$IMAGE" >/dev/null
    stop_pool() { docker rm -f "$NAME" >/dev/null; }
    # The jobs write the state directory of the caller, so they run with the caller's uid.
    JOB_USER=$(docker exec "$NAME" getent passwd "$(id -u)" | cut -d: -f1)
    if [ -z "$JOB_USER" ]; then
        JOB_USER=edr
        docker exec "$NAME" useradd -m -u "$(id -u)" "$JOB_USER"
    fi
    VIA=(docker exec -u "$JOB_USER" "$NAME")
else
    echo "RUNTIME is docker or singularity"; exit 2
fi
trap stop_pool EXIT

for _ in $(seq 90); do
    [ "$("${VIA[@]}" condor_status -af Name 2>/dev/null | wc -l)" -ge 1 ] && break
    sleep 2
done
"${VIA[@]}" condor_status -af Name | sed 's/^/slot: /'
"${VIA[@]}" condor_version | head -1

BACKEND=condor
SUBMIT_VIA=$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "${VIA[@]}")
# shellcheck source=demo.sh
source "$HERE/demo.sh"
rc=0
run_demo || rc=$?
"${VIA[@]}" condor_history -af ClusterId JobStatus ExitCode EdrRunId 2>/dev/null | sed 's/^/history: /' || true
exit $rc
