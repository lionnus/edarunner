#!/usr/bin/env bash
# Run the GCD example end to end on the host local, as tests/test_e2e_local.py does
# for the demo. Needs edr (EDR names the binary), git, rsync and the ORFS tools.
set -euo pipefail
cd "$(dirname "$0")"
EDR=${EDR:-edr}
CAP_S=${CAP_S:-900}

rm -rf repo wt data
mkdir repo && cp design/config.mk repo/
git -C repo init -q -b main && git -C repo add -A
git -C repo -c user.name=gcd -c user.email=gcd@example.com commit -q -m "gcd design"

src=$($EDR --json stage HEAD | python3 -c 'import json, sys; print(json.load(sys.stdin)["data"]["src"])')
$EDR check
$EDR plan gcd
$EDR launch gcd

end=$((SECONDS + CAP_S))
while :; do
  $EDR watch --once
  $EDR status --narrow | grep -q "nothing live" && break
  [ "$SECONDS" -lt "$end" ] || { echo "run still live after $CAP_S s"; $EDR status; exit 1; }
  sleep 10
done
$EDR status
$EDR status gcd@gcd
$EDR --json status | python3 -c 'import json, sys
runs = json.load(sys.stdin)["data"]["runs"]
assert [r["phase"] for r in runs] == ["done"], [(r["label"], r["phase"]) for r in runs]'
csv=$($EDR metrics --design "$src" --csv)
echo "$csv"
grep -q ",area_synth_um2,area.cell," <<<"$csv"
grep -q ",wns_place_ns,timing.wns," <<<"$csv"
$EDR export --design "$src" --out "data/exports/$src"
