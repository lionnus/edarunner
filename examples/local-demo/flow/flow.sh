#!/usr/bin/env bash
# The stand-in flow: flow.sh <stage> <run_id> <config> [FIRST_STAGE=x] [LAST_STAGE=y] [NETLIST_STAGE=n] [KEY=VALUE ...]
# Each step sleeps one second and writes reports/<n>/area.rpt and qor.rpt;
# step n has a setup slack of -0.0n ns and n failing setup paths.
# The config "fail_licence" fails once at step 2 with a licence line in the
# log, then succeeds on the retry (a marker file remembers the first try).
set -uo pipefail
stage=$1 run_id=$2 config=$3; shift 3
first=""; last=""; netlist=11
for a in "$@"; do case "$a" in FIRST_STAGE=*) first=${a#*=};; LAST_STAGE=*) last=${a#*=};; NETLIST_STAGE=*) netlist=${a#*=};; esac; done
names=(setup analyze elaborate synth cts route export)
idx() { local i; for i in "${!names[@]}"; do [ "${names[$i]}" = "$1" ] && { echo "$i"; return; }; done; echo -1; }
case "$stage" in synth) lo=0; hi=3;; pnr) lo=4; hi=5;; export) lo=6; hi=6;; *) echo "unknown stage $stage" >&2; exit 2;; esac
[ -n "$first" ] && lo=$(idx "$first")
[ -n "$last" ] && hi=$(idx "$last")
for ((n=lo; n<=hi; n++)); do
  name=${names[$n]}
  echo "[$(date +%T)] step $n $name"
  if [ "$config" = fail_licence ] && [ "$n" = 2 ] && [ ! -e .failed_once ]; then
    touch .failed_once; echo "Error: license checkout failed for demo"; exit 1
  fi
  sleep 1
  mkdir -p "reports/$n"
  awk -v n="$n" 'BEGIN { printf "i_top %.1f\n", 1000 + n * 10.5 }' > "reports/$n/area.rpt"
  # One block per scenario and path group, as a report_qor has: the hold scenario first.
  cat > "reports/$n/qor.rpt" <<EOF
Scenario           'func_fast'
Timing Path Group  'reg2reg'
----------------------------------------
Worst Hold Violation:           -0.001
No. of Hold Violations:              1
----------------------------------------

Scenario           'func_slow'
Timing Path Group  'in2reg'
----------------------------------------
Critical Path Slack:              0.01
No. of Violating Paths:              0
----------------------------------------

Scenario           'func_slow'
Timing Path Group  'reg2reg'
----------------------------------------
Critical Path Slack:             -0.0$n
No. of Violating Paths:              $n
----------------------------------------
EOF
  if [ "$name" = export ]; then mkdir -p "out/$netlist" && echo "module top; endmodule" > "out/$netlist/netlist.v"; fi
done
echo "[$(date +%T)] $stage done"
