#!/usr/bin/env bash
# The stand-in kernel: kernel.sh <kernel> <test> [KEY=VALUE ...]
# Writes simulation/tests/<config>/<test>/power/{reports/power.csv,phases.json}
# after a short sleep, and a placeholder VCD. The kernel "bad" fails with "boom".
set -uo pipefail
kernel=$1 test=$2; shift 2
config=${DEMO_CONFIG:-demo}
d="simulation/tests/$config/$test"
mkdir -p "$d/power/reports"
[ "$kernel" = bad ] && { echo "boom: kernel $kernel failed"; exit 1; }
sleep "${DEMO_SLEEP:-2}"
head -c 1048576 /dev/zero > "$d/wave.vcd"
printf 'phase,total_w\nPHASE_A,0.100\nPHASE_B,0.150\nWHOLE,0.250\n' > "$d/power/reports/power.csv"
printf '{"window_ns": 3400, "kernel": "%s"}\n' "$kernel" > "$d/power/phases.json"
echo "kernel $kernel $test done"
