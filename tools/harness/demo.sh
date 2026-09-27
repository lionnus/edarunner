# The local demo through a scheduler backend, for condor.sh and slurm.sh; source it, then call run_demo.
#
# Needs: EDR (the edr command), WORK (a directory the head node and every job see at the same path),
# BACKEND and SUBMIT_VIA (a TOML array). Checks: two runs end done, a stopped run ends KILLED, and the
# licence fc:1 serialises the two runs that need the tool demo.

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
FAILS=0
fail() { echo "FAIL: $*"; FAILS=$((FAILS + 1)); }
pass() { echo "ok: $*"; }

make_project() {
    local p=$WORK/demo
    cp -r "$REPO/examples/local-demo" "$p"
    rm -rf "$p/repo" "$p/wt" "$p/data" "$p/jobs"/*
    sed -i 's|^state_dir = .*|state_dir = "{project_root}/state"|' "$p/edr.toml"
    cat >> "$p/edr.toml" <<'EOF'

[stages.hold]
cmd = "sleep 600"
EOF
    cat > "$p/site.toml" <<EOF
schema = 1
scratch = []

[scheduler]
backend = "$BACKEND"
submit_via = $SUBMIT_VIA
tree_root = "$WORK/trees"

[tools.demo]
seats = 10
probe = ["bash", "{root}/flow/seats.sh"]
licence = "fc"
EOF
    cat > "$p/jobs/h.toml" <<'EOF'
batch = "h"
source = "HEAD"

[[job]]
label = "a"
config = "demo"
stages = ["synth", "pnr"]

[[job]]
label = "b"
config = "demo"
stages = ["synth", "pnr"]

[[job]]
label = "c"
config = "demo"
stages = ["hold"]
EOF
    (cd "$p" && bash setup.sh >/dev/null)
}

# The field `key` of the run `label` in `edr --json status`, or "-".
field() {
    (cd "$WORK/demo" && "$EDR" --json status) | python3 -c '
import json, sys
rows = {r["label"]: r for r in json.load(sys.stdin)["data"]["runs"]}
print(rows.get(sys.argv[1], {}).get(sys.argv[2]) or "-")' "$1" "$2"
}

# One watcher cycle, then the heartbeat field `expr` (a python expression over d) of `label`.
hb() {
    local f
    f=$(ls "$WORK"/demo/state/h/*_"$1"_*.json 2>/dev/null | grep -v -e '\.spec\.json$' -e '\.keep\.json$' | head -1)
    [ -n "$f" ] && python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))" "$f" "$2" \
        2>/dev/null || echo "-"
}

wait_for() {  # label regex timeout_s
    local t=0
    while [ "$t" -lt "$3" ]; do
        (cd "$WORK/demo" && "$EDR" watch --once >/dev/null 2>&1) || true
        [[ $(hb "$1" 'd["phase"]') =~ $2 ]] && return 0
        sleep 3; t=$((t + 3))
    done
    return 1
}

run_demo() {
    make_project
    cd "$WORK/demo"
    "$EDR" check && pass "edr check" || fail "edr check"
    "$EDR" checkout HEAD >/dev/null
    "$EDR" launch h | tail -1
    [ "$(field a handle)" != - ] && pass "a submitted as $(field a handle)" || fail "a has no handle"

    if wait_for c '^stage:hold' 180; then
        pass "c runs on $(hb c 'd["host"]'), job $(hb c 'd["sched_id"]')"
        "$EDR" stop c@h --why harness
        wait_for c '^KILLED' 90 && pass "edr stop ended c as $(hb c 'd["phase"]')" || fail "c after stop: $(hb c 'd["phase"]')"
    else
        fail "c never reached stage:hold, phase $(hb c 'd["phase"]')"
    fi
    for r in a b; do
        wait_for "$r" '^done$' 300 && pass "$r done on $(hb "$r" 'd["host"]')" || fail "$r phase $(hb "$r" 'd["phase"]')"
    done
    "$EDR" watch --once >/dev/null 2>&1 || true
    for r in a b; do
        [ "$(field "$r" state)" = done ] && pass "$r state done" || fail "$r state $(field "$r" state)"
    done
    [ "$(field c state)" = killed ] && pass "c state killed" || fail "c state $(field c state)"
    local a0 a1 b0 b1
    a0=$(hb a 'd["stages"]["synth"]["started"]'); a1=$(hb a 'd["stages"]["pnr"]["ended"]')
    b0=$(hb b 'd["stages"]["synth"]["started"]'); b1=$(hb b 'd["stages"]["pnr"]["ended"]')
    if [ "$a1" != - ] && [ "$b1" != - ] && { [ "$b0" -ge "$a1" ] || [ "$a0" -ge "$b1" ]; }; then
        pass "licence fc:1 held: a $a0-$a1, b $b0-$b1"
    else
        fail "a and b overlapped: a $a0-$a1, b $b0-$b1"
    fi
    "$EDR" status
    echo "failures: $FAILS"
    return $((FAILS > 0))
}
