#!/usr/bin/env bash
# Run the GCD example end to end on the host local, as tests/test_e2e_local.py does
# for the demo. Needs edr (EDR names the binary), git, rsync and the ORFS tools.
set -euo pipefail
cd "$(dirname "$0")"
EDR=${EDR:-edr}
CAP_S=${CAP_S:-900}

rm -rf wt data
bash setup.sh

source=$($EDR --json checkout HEAD | python3 -c 'import json, sys; print(json.load(sys.stdin)["data"]["source"])')
$EDR check
$EDR plan gcd
$EDR launch gcd

end=$((SECONDS + CAP_S))
while :; do
  # The collect cycle runs after the terminal state is seen, so the last stage is collected too.
  $EDR status --narrow | grep -q "nothing live" && over=1 || over=0
  $EDR watch --once
  [ "$over" = 1 ] && break
  [ "$SECONDS" -lt "$end" ] || { echo "run still live after $CAP_S s"; $EDR status; exit 1; }
  sleep 10
done
$EDR status
$EDR status gcd@gcd
$EDR --json status | python3 -c 'import json, sys
runs = json.load(sys.stdin)["data"]["runs"]
assert [r["phase"] for r in runs] == ["done"], [(r["label"], r["phase"]) for r in runs]'
csv=$($EDR metrics --source "$source" --csv)
echo "$csv"
grep -q ",area_synth_um2,design__instance__area," <<<"$csv"
grep -q ",wns_place_ns,timing__setup__ws," <<<"$csv"
$EDR export --source "$source" --out "data/exports/$source"
