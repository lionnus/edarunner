"""The private flow: the knobs, the private file, the redaction, the publish converters, and the demo end to end with a
private stage whose raw output, reports and commands never reach a public output."""

from __future__ import annotations

import io
import json
import subprocess
import time
from pathlib import Path

import pytest
from helpers_cli import edr
from helpers_results import area_report

from edarunner import board, cli, config, launch, private
from edarunner.config import ConfigError
from edarunner.model import Job, Private, Publish

# Every string below that holds SECRET or SC8T stands for text under the agreement.
SECRETS = ("SECRET", "SC8T", "signoff.sh", "set_clock_period", "\\\\w")

TIMING_SYNOPSYS = """\
****************************************
Report : timing
Design : top_wrap
****************************************

  Startpoint: i_top/a_q_reg[0]
              (rising edge-triggered flip-flop clocked by clk)
  Endpoint: i_top/b_q_reg[3]
            (rising edge-triggered flip-flop clocked by clk)
  Path Group: clk
  Path Type: max

  Des/Clust/Port     Wire Load Model       Library
  ------------------------------------------------
  top_wrap           SC8T_SECRET_WLM       SC8T_SECRETLIB

  Point                                    Incr       Path
  -----------------------------------------------------------
  clock clk (rise edge)                    0.00       0.00
  clock network delay (ideal)              0.00       0.00
  i_top/a_q_reg[0]/CK (SC8T_DFFX1_SECRET)  0.00       0.00 r
  i_top/a_q_reg[0]/Q (SC8T_DFFX1_SECRET)   0.45       0.45 f
  n12 (net)                      2
  i_top/U12/Y (SC8T_ND2X4_SECRET)          0.12 *     0.57 r
  i_top/U13/Y (SC8T_MYSTERY_SECRET)        1.38 &     1.95 f
  i_top/b_q_reg[3]/D (SC8T_DFFX1_SECRET)   0.00       1.95 f
  data arrival time                                   1.95

  clock clk (rise edge)                    2.00       2.00
  clock network delay (ideal)              0.00       2.00
  i_top/b_q_reg[3]/CK (SC8T_DFFX1_SECRET)  0.00       2.00 r
  library setup time                      -0.10       1.90
  data required time                                  1.90
  -----------------------------------------------------------
  data required time                                  1.90
  data arrival time                                  -1.95
  -----------------------------------------------------------
  slack (VIOLATED)                                   -0.05


  Startpoint: in_valid (input port clocked by clk)
  Endpoint: i_top/c_q_reg (rising edge-triggered flip-flop clocked by clk)
  Path Group: in2reg
  Path Type: max

  Point                                    Incr       Path
  -----------------------------------------------------------
  clock clk (rise edge)                    0.00       0.00
  input external delay                     0.50       0.50 r
  in_valid (in)                            0.00       0.50 r
  i_top/U2/Y (SC8T_INVX1_SECRET)           0.05       0.55 f
  i_top/c_q_reg/D (SC8T_DFFX1_SECRET)      0.00       0.55 f
  data arrival time                                   0.55

  clock clk (rise edge)                    2.00       2.00
  library setup time                      -0.10       1.90
  data required time                                  1.90
  -----------------------------------------------------------
  data required time                                  1.90
  data arrival time                                  -0.55
  -----------------------------------------------------------
  slack (MET)                                         1.35
"""

TIMING_OPENROAD = """\
Startpoint: _692_ (rising edge-triggered flip-flop clocked by core_clock)
Endpoint: _693_ (rising edge-triggered flip-flop clocked by core_clock)
Path Group: core_clock
Path Type: max

Fanout     Cap    Slew   Delay    Time   Description
-----------------------------------------------------------------------------
                  0.00    0.00    0.00   clock core_clock (rise edge)
                          0.00    0.00   clock network delay (ideal)
                  0.03    0.00    0.00 ^ _692_/CLK (sky130_fd_sc_hd__dfxtp_1)
                  0.04    0.31    0.31 v _692_/Q (sky130_fd_sc_hd__dfxtp_1)
     1    0.00                           _100_ (net)
                  0.05    0.10    0.41 ^ _400_/Y (sky130_fd_sc_hd__nand2_2)
                          0.00    0.41 ^ _693_/D (sky130_fd_sc_hd__dfxtp_1)
                                  0.41   data arrival time

                  0.00    5.00    5.00   clock core_clock (rise edge)
                          0.00    5.00   clock network delay (ideal)
                                  5.00 ^ _693_/CLK (sky130_fd_sc_hd__dfxtp_1)
                         -0.12    4.88   library setup time
                                  4.88   data required time
-----------------------------------------------------------------------------
                                  4.88   data required time
                                 -0.41   data arrival time
-----------------------------------------------------------------------------
                                  4.47   slack (MET)
"""

SIGNOFF = """\
#!/usr/bin/env bash
# The stand-in of a tool session under the agreement: signoff.sh <knob file>
set -eu
echo "Vendor Shell SECRET-2026.03 (c) the vendor; confidential"
echo "Information: Loading library SC8T_SECRETLIB (LIB-001)"
cat "$1"
echo "[flow] retime=$EDR_KNOB_RETIME clock_ns=$EDR_KNOB_CLOCK_NS effort=$EDR_KNOB_EFFORT pdk=$PDK_ROOT"
echo "Warning: cell SC8T_ND2X1_SECRET has no footprint (PHYS-12)"
mkdir -p reports/signoff
cat > reports/signoff/area.rpt <<'EOF'
@AREA@
EOF
cat > reports/signoff/timing.rpt <<'EOF'
@TIMING@
EOF
printf 'Leaf Cell Count: 4242\\nCore Area: 500.0\\n' > reports/signoff/qor.rpt
printf 'Warning: SC8T_SECRET corner is old\\nInformation: fine\\n' > reports/signoff/check.rpt
echo "[flow] done"
"""


def private_file(nda: Path, extra: str = "") -> Path:
    path = nda / "private.toml"
    path.write_text(f"""\
schema = 1
ge_um2 = 0.5
redact = ['SECRET\\w*', 'SC8T_\\w+']
keep = ['^\\[flow\\]', '^Warning: ']
cells = {{ NAND2 = 'ND2X(?P<drive>\\d+)', DFF = 'DFF', INV = 'INV' }}
env = {{ PDK_ROOT = "/opt/SECRETPDK/v1" }}

[stages.signoff]
cmd = "bash {nda}/signoff.sh {{knob_file}}"
collect = ["reports/signoff/"]

[knobs.retime]
line = {{ true = "set_app_option SECRET_RETIME true", false = "" }}

[knobs.clock_ns]
line = "set_clock_period_SECRET {{value}}"

[publish.area]
stage = "signoff"
file = "reports/signoff/area.rpt"
kind = "area"

[publish.timing]
stage = "signoff"
file = "reports/signoff/timing.rpt"
kind = "timing"

[publish.qor]
stage = "signoff"
file = "reports/signoff/qor.rpt"
kind = "values"
values = {{ cells = 'Leaf Cell Count:\\s+(\\d+)', core_ge = {{ regex = 'Core Area:\\s+([0-9.]+)', ge = true }} }}

[publish.warnings]
stage = "signoff"
file = "reports/signoff/check.rpt"
kind = "text"
keep = ['^Warning']
{extra}""")
    return path


PUBLIC = """
[knobs.retime]
default = false
help = "move registers across the logic"
stages = ["signoff"]

[knobs.clock_ns]
default = 2.0
min = 0.5
max = 20.0
help = "the target clock period"

[knobs.effort]
choices = ["low", "medium", "high"]
default = "medium"

[stages.signoff]
private = true
needs = { cores = 1, disk_gb = 0.01 }

[metrics.area_ge]
stage = "signoff"
file = "public/area.csv"
table = { instance = "instance", value = "ge", local = "local_ge", depth = "depth", top = { instance = "<top>" } }
unit = "GE"

[metrics.wns_signoff_ns]
stage = "signoff"
file = "public/timing.json"
json = "summary.max.worst_slack"
unit = "ns"

[metrics.core_ge]
stage = "signoff"
file = "public/qor.json"
json = "values.core_ge"
unit = "GE"
"""


@pytest.fixture
def nda(demo: Path, tmp_path: Path) -> Path:
    """The demo with a private stage `signoff`, three knobs, and a batch `nda`; the private file in tmp_path/nda."""
    nda = tmp_path / "nda"
    nda.mkdir()
    area = area_report(1000.0, [("i_top", 990.0), ("i_top/u_SC8T_SECRET_macro", 600.0), ("i_top/u_core", 300.0)])
    (nda / "signoff.sh").write_text(SIGNOFF.replace("@AREA@", area.rstrip("\n")).replace("@TIMING@", TIMING_SYNOPSYS.rstrip("\n")))
    private_file(nda)
    toml = (demo / "edr.toml").read_text()
    toml = toml.replace('run_prefix = "{user}/edr/{project}"\n', f'run_prefix = "{{user}}/edr/{{project}}"\nprivate = "{nda}/private.toml"\n')
    (demo / "edr.toml").write_text(toml + PUBLIC)
    (demo / "jobs" / "nda.toml").write_text('batch = "nda"\nsource = "HEAD"\n\n[[job]]\nlabel = "fast"\nstages = ["signoff"]\n'
                                            'knobs = { retime = true, clock_ns = 1.6 }\n')
    return demo


def clean(text: str) -> None:
    for s in SECRETS:
        assert s not in text, f"{s} leaked: {text}"


# --- config

def test_knobs_load_in_file_order_with_their_types(nda: Path) -> None:
    p = config.load_project(nda)
    assert list(p.knobs) == ["retime", "clock_ns", "effort"]
    assert [private.knob_type(k) for k in p.knobs.values()] == ["bool", "float", "choice"]
    assert p.stages["signoff"].private and p.stages["signoff"].cmd == ""
    assert p.private is not None and p.private.ge_um2 == 0.5 and list(p.private.publish) == ["area", "timing", "qor", "warnings"]


@pytest.mark.parametrize("knobs, error", [
    ("{ retimer = true }", "unknown knob retimer (edr.toml has: retime, clock_ns, effort)"),
    ("{ clock_ns = 0.1 }", "knob clock_ns is 0.1, below its min 0.5"),
    ("{ clock_ns = 30 }", "knob clock_ns is 30, above its max 20"),
    ("{ retime = 1 }", "knob retime is 1, not a bool"),
    ("{ effort = \"max\" }", "knob effort is 'max', not one of low, medium, high"),
])
def test_a_job_knob_outside_the_api_stops_the_load(nda: Path, knobs: str, error: str) -> None:
    (nda / "jobs" / "bad.toml").write_text(f'source = "HEAD"\n[[job]]\nlabel = "x"\nknobs = {knobs}\n')
    with pytest.raises(ConfigError, match=error.replace("(", r"\(").replace(")", r"\)")):
        config.load_batch(config.load_project(nda), "bad")


def test_a_job_knob_becomes_its_type(nda: Path) -> None:
    (nda / "jobs" / "ok.toml").write_text('source = "HEAD"\n[[job]]\nlabel = "x"\nknobs = { clock_ns = 3 }\n')
    job = config.load_batch(config.load_project(nda), "ok").jobs[0]
    assert job.knobs == {"clock_ns": 3.0} and type(job.knobs["clock_ns"]) is float


@pytest.mark.parametrize("edit, error", [
    (lambda t: t.replace("[stages.signoff]\nprivate = true", "[stages.signoff]\nprivate = true\ncmd = \"x\""),
     "stages.signoff is private, so its cmd come from the private file"),
    (lambda t: t.replace("[knobs.effort]\nchoices", "[knobs.effort]\nchoice"), "unknown key 'knobs.effort.choice'"),
    (lambda t: t.replace('default = "medium"', 'default = "max"'), "knobs.effort.default: knob effort is 'max'"),
    (lambda t: t.replace('stages = ["signoff"]', 'stages = ["nope"]'), "knobs.retime.stages names unknown stage 'nope'"),
    (lambda t: t.replace("needs = { cores = 1, disk_gb = 0.01 }", 'collect = ["public/"]'),
     "stages.signoff is private, so its collect come from the private file"),
    (lambda t: t.replace('collect = ["reports/"]', 'collect = [".edr/private/log/"]', 1),
     "outside the public part of the run tree"),
    (lambda t: t.replace('min = 0.5\n', '').replace("[knobs.effort]\n", "[knobs.effort]\nmin = 1\n"),
     "min and max belong to an int or a float knob"),
])
def test_edr_toml_errors_of_the_private_flow(nda: Path, edit, error: str) -> None:
    (nda / "edr.toml").write_text(edit((nda / "edr.toml").read_text()))
    with pytest.raises(ConfigError, match=error.replace("(", r"\(").replace("[", r"\[")):
        config.load_project(nda)


@pytest.mark.parametrize("extra, error", [
    ("[knobs.nope]\nline = 'x'\n", "knobs.nope maps no knob of edr.toml"),
    ("[stages.synth]\ncmd = 'x'\n", "stages.synth is no stage of edr.toml with private = true"),
    ("[publish.bad]\nstage = 'signoff'\nfile = 'x'\nkind = 'pdf'\n", "publish.bad.kind is one of area, timing"),
    ("[publish.bad]\nstage = 'signoff'\nfile = 'x'\nkind = 'values'\nvalues = { a = 'no group' }\n", "needs group 1"),
    ("[publish.bad]\nstage = 'signoff'\nfile = 'x'\nkind = 'text'\n", "publish.bad is text and needs keep"),
])
def test_private_file_errors(nda: Path, tmp_path: Path, extra: str, error: str) -> None:
    private_file(tmp_path / "nda", extra)
    with pytest.raises(ConfigError, match=error):
        config.load_project(nda)


@pytest.mark.parametrize("old, new, error", [
    ('line = { true = "set_app_option SECRET_RETIME true", false = "" }', 'line = { ture = "x" }',
     "knobs.retime.line names ture, not a value of the knob"),
    ('line = "set_clock_period_SECRET {value}"', 'line = { "1.6" = "x" }', "a float knob takes one line with"),
])
def test_a_table_of_knob_lines_names_the_values_of_the_knob(nda: Path, tmp_path: Path, old: str, new: str,
                                                              error: str) -> None:
    path = tmp_path / "nda" / "private.toml"
    path.write_text(path.read_text().replace(old, new))
    with pytest.raises(ConfigError, match=error):
        config.load_project(nda)


def test_a_private_stage_needs_its_command_in_the_private_file(nda: Path, tmp_path: Path) -> None:
    path = tmp_path / "nda" / "private.toml"
    path.write_text(path.read_text().replace("[stages.signoff]", "[stages.other]"))
    with pytest.raises(ConfigError, match="stages.other is no stage"):
        config.load_project(nda)
    path.unlink()
    with pytest.raises(ConfigError, match="the private file does not exist"):
        config.load_project(nda)


def test_an_area_publish_needs_ge_um2(nda: Path, tmp_path: Path) -> None:
    path = tmp_path / "nda" / "private.toml"
    path.write_text(path.read_text().replace("ge_um2 = 0.5\n", ""))
    with pytest.raises(ConfigError, match="publish.area gives gate equivalents and needs ge_um2"):
        config.load_project(nda)


# --- knobs and the spec

def test_build_tag_takes_a_token_per_knob_off_its_default(nda: Path) -> None:
    p = config.load_project(nda)
    assert launch.build_tag(p, Job(label="a", config="base", knobs={"retime": True, "clock_ns": 1.6})) == \
        "base_retime_clock_ns1p6"
    assert launch.build_tag(p, Job(label="a", knobs={"retime": False, "effort": "high", "clock_ns": 2.0})) == "efforthigh"
    p.knobs["retime"].default = True
    assert launch.build_tag(p, Job(label="a", knobs={"retime": False})) == "noretime"


def test_a_knob_value_keeps_its_digits_and_a_tag_tells_text_values_apart(nda: Path) -> None:
    p = config.load_project(nda)
    assert private.knob_text(1.2345678) == "1.2345678" and private.knob_text(2.0) == "2.0"
    assert private.knob_tokens(p, {"clock_ns": 1.6000001}) != private.knob_tokens(p, {"clock_ns": 1.6})
    p.knobs["effort"].type, p.knobs["effort"].choices = "str", []
    a, b = private.knob_tokens(p, {"effort": "a_b"}), private.knob_tokens(p, {"effort": "ab"})
    assert a != b and a[0].startswith("effortabh") and b == ["effortab"]


def test_knob_lines_follow_edr_toml_and_the_stages_of_each_knob(nda: Path) -> None:
    p = config.load_project(nda)
    values = private.knob_values(p, {"retime": True, "clock_ns": 1.6})
    assert private.knob_lines(p, "signoff", values) == \
        "# edr: the knobs of stage signoff\nset_app_option SECRET_RETIME true\nset_clock_period_SECRET 1.6\n"
    assert private.knob_lines(p, "pnr", values) == "# edr: the knobs of stage pnr\nset_clock_period_SECRET 1.6\n"
    assert "SECRET_RETIME" not in private.knob_lines(p, "signoff", private.knob_values(p, {}))


def test_the_spec_of_a_private_stage_and_its_public_view(nda: Path) -> None:
    subprocess.run(["bash", "setup.sh"], cwd=nda, check=True, capture_output=True)
    p = config.load_project(nda)
    from edarunner.db import Database
    from edarunner.hosts import Ssh
    batch = config.load_batch(p, "nda")
    from edarunner import checkout
    batch.source = checkout.ensure(p, batch.source).source
    with Database(":memory:") as db:
        (plan,) = launch.plan(p, batch, Ssh(p.site), db)
    spec = plan.spec
    st = spec["stages"][0]
    kf = f"{plan.root}/.edr/private/knobs/signoff.knobs"
    assert st["cmd"] == f"bash {Path(p.private.path).parent}/signoff.sh {kf}" and st["private"]["stage"] == "signoff"
    assert st["files"][kf].endswith("set_clock_period_SECRET 1.6\n")
    assert spec["env"]["EDR_KNOB_RETIME"] == "true" and spec["env"]["EDR_KNOB_CLOCK_NS"] == "1.6"
    assert spec["env"]["EDR_KNOB_EFFORT"] == "medium" and spec["env"]["PDK_ROOT"] == "/opt/SECRETPDK/v1"
    assert spec["knobs"] == {"retime": True, "clock_ns": 1.6, "effort": "medium"}
    assert {k: v for k, v in spec["private"].items() if k != "literals"} == \
        {"dir": f"{plan.root}/.edr/private", "redact": ["SECRET\\w*", "SC8T_\\w+"], "env": ["PDK_ROOT"]}
    assert "/opt/SECRETPDK/v1" in spec["private"]["literals"]
    pub = private.public_spec(spec)
    assert pub["stages"][0]["cmd"] == private.PRIVATE and "files" not in pub["stages"][0]
    assert pub["env"]["PDK_ROOT"] == private.PRIVATE and "redact" not in pub["private"]
    clean(json.dumps(pub))
    clean(launch.spec_text(p, spec))


# --- redaction

def test_redact_replaces_private_strings_and_patterns(nda: Path) -> None:
    config.load_project(nda)
    assert private.active()
    text = private.redact(f"ran bash {nda.parent.parent / 'nda'}/signoff.sh /x/knobs with SC8T_ND2X1 and SECRET_LIB")
    assert text == f"ran {private.PRIVATE} /x/knobs with {private.NDA} and {private.NDA}"
    assert private.redact_obj({"SC8T_A": ["SECRET", 3]}) == {private.NDA: [private.NDA, 3]}


def test_stream_redacts_whole_lines_and_the_rest_at_flush(nda: Path) -> None:
    config.load_project(nda)
    raw = io.StringIO()
    s = private.Stream(raw)
    s.write("a SEC")
    assert raw.getvalue() == ""
    s.write("RET b\nc SC8T_X")
    assert raw.getvalue() == f"a {private.NDA} b\n"
    s.flush()
    assert raw.getvalue() == f"a {private.NDA} b\nc {private.NDA}"


def test_a_private_config_error_names_no_private_string(nda: Path, tmp_path: Path, capsys) -> None:
    path = tmp_path / "nda" / "private.toml"
    path.write_text(path.read_text().replace("{knob_file}", "{knob_file} {nope}"))
    subprocess.run(["bash", "setup.sh"], cwd=nda, check=True, capture_output=True)
    code, out, err = edr(capsys, "plan", "nda")
    assert code == 1 and "unknown placeholder {nope}" in out + err
    clean(out + err)


def test_no_public_collect_copies_a_withheld_path(nda: Path, tmp_path: Path) -> None:
    from edarunner.collect import _Copier
    from edarunner.db import Database
    path = tmp_path / "nda" / "private.toml"
    path.write_text(path.read_text().replace("schema = 1\n", 'schema = 1\npaths = ["nonfree/"]\n', 1))
    p = config.load_project(nda)
    root = tmp_path / "scratch" / "edr" / "demo" / "20260926_1200_nda_fast_gabc"
    for f in (".edr/private/log/signoff.log", ".edr/container.sh", "reports/signoff/area.rpt", "reports/0/area.rpt",
              "nonfree/synth.tcl", "ok.txt"):
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text("SECRET\n")
    run = {"run_id": root.name, "host": "local", "root": str(root), "batch": "nda"}
    for entry in ("./", ".", ".edr/", ".edr", "reports/", "reports"):
        with Database(":memory:") as db:
            c = _Copier(p, None, db, run, {}, False)  # type: ignore[arg-type]
            c.copy_all([entry], "always")
            assert not c.result.failures, (entry, c.result.failures)
    got = sorted(str(f.relative_to(p.data / "results" / root.name)) for f in (p.data / "results" / root.name).rglob("*")
                 if f.is_file())
    assert got == [".edr/container.sh", "ok.txt", "reports/0/area.rpt"]
    with Database(":memory:") as db:
        c = _Copier(p, None, db, run, {}, False)  # type: ignore[arg-type]
        c.copy_all(["/etc/", "../x/", ".edr/private/", "nonfree/"], "always")
        assert len(c.result.failures) == 4 and all("outside the public part" in f for f in c.result.failures)


# --- publish

def test_parse_timing_of_synopsys() -> None:
    a, b = private.parse_timing(TIMING_SYNOPSYS)
    assert (a["startpoint"], a["endpoint"], a["group"], a["type"]) == ("i_top/a_q_reg[0]", "i_top/b_q_reg[3]", "clk", "max")
    assert (a["slack"], a["arrival"], a["required"], a["cells"]) == (-0.05, 1.95, 1.90, 4)
    pins = [p.get("pin") or p["what"] for p in a["points"]]
    assert pins == ["clock clk (rise edge)", "clock network delay (ideal)", "i_top/a_q_reg[0]/CK", "i_top/a_q_reg[0]/Q",
                    "i_top/U12/Y", "i_top/U13/Y", "i_top/b_q_reg[3]/D"]
    assert a["points"][4] == {"pin": "i_top/U12/Y", "cell": "SC8T_ND2X4_SECRET", "incr": 0.12, "time": 0.57, "edge": "r"}
    assert (b["group"], b["slack"], b["cells"]) == ("in2reg", 1.35, 2)
    assert b["points"][2] == {"pin": "in_valid", "cell": "in", "incr": 0.0, "time": 0.5, "edge": "r"}


def test_publish_timing_keeps_setup_and_hold_apart(tmp_path: Path) -> None:
    raw = tmp_path / "timing.rpt"
    hold = TIMING_OPENROAD.replace("Path Type: max", "Path Type: min").replace("4.47   slack (MET)", "-0.02   slack (VIOLATED)")
    raw.write_text(hold + TIMING_OPENROAD)
    doc = json.loads(private._timing(_private(tmp_path), Publish(name="t", stage="s", file="x", kind="timing"), raw))
    assert doc["summary"]["max"]["worst_slack"] == 4.47 and doc["summary"]["min"]["worst_slack"] == -0.02
    assert doc["summary"]["min"]["violated"] == 1 and doc["summary"]["max"]["violated"] == 0


def test_parse_timing_of_openroad() -> None:
    (a,) = private.parse_timing(TIMING_OPENROAD)
    assert (a["startpoint"], a["group"], a["slack"], a["arrival"], a["required"], a["cells"]) == \
        ("_692_", "core_clock", 4.47, 0.41, 4.88, 3)
    assert a["points"][4] == {"pin": "_400_/Y", "cell": "sky130_fd_sc_hd__nand2_2", "incr": 0.10, "time": 0.41, "edge": "r"}
    assert a["points"][1] == {"what": "clock network delay (ideal)", "incr": 0.0, "time": 0.0}


def _private(tmp_path: Path, **kw) -> Private:
    p = Private(path=tmp_path / "private.toml", ge_um2=0.5, redact=["SECRET\\w*", "SC8T_\\w+"],
                cells={"NAND2": "ND2X(?P<drive>\\d+)", "DFF": "DFF"}, **kw)
    private.register(p)
    return p


def test_publish_timing_classes_cells_and_sums_per_group(tmp_path: Path) -> None:
    raw = tmp_path / "timing.rpt"
    raw.write_text(TIMING_SYNOPSYS)
    doc = json.loads(private._timing(_private(tmp_path), Publish(name="t", stage="s", file="x", kind="timing"), raw))
    clean(json.dumps(doc))
    assert doc["summary"] == {"max": {"paths": 2, "worst_slack": -0.05, "violated": 1, "negative_sum": -0.05, "groups": {
        "clk": {"paths": 1, "worst_slack": -0.05, "violated": 1, "negative_sum": -0.05},
        "in2reg": {"paths": 1, "worst_slack": 1.35, "violated": 0, "negative_sum": 0.0}}}}
    cells = [p.get("cell") for p in doc["paths"][0]["points"] if "cell" in p]
    assert cells == ["DFF", "DFF", "NAND2x4", private.CELL, "DFF"]
    assert doc["paths"][1]["points"][2]["cell"] == "port"
    doc = json.loads(private._timing(_private(tmp_path), Publish(name="t", stage="s", file="x", kind="timing", paths=1,
                                                                 points=False), raw))
    assert len(doc["paths"]) == 1 and "points" not in doc["paths"][0] and doc["paths_in_file"] == 2


def test_publish_area_in_gate_equivalents(tmp_path: Path) -> None:
    raw = tmp_path / "area.rpt"
    raw.write_text(area_report(1000.0, [("i_top", 990.0), ("i_top/u_SC8T_SECRET", 600.0), ("i_top/u_core/u_deep", 30.0)]))
    text = private._area(_private(tmp_path), Publish(name="a", stage="s", file="x", kind="area", depth=2), raw)
    assert text.splitlines() == ["instance,depth,ge,local_ge,cells", "<top>,0,2000.0,0.0,", "i_top,1,1980.0,0.0,",
                                 f"i_top/u_{private.NDA},2,1200.0,0.0,"]


def test_publish_values_text_and_python(tmp_path: Path) -> None:
    pv = _private(tmp_path)
    raw = tmp_path / "qor.rpt"
    raw.write_text("Leaf Cell Count: 42\nCore Area: 10.0\nWarning: SC8T_X slow\nok\n")
    vals = Publish(name="q", stage="s", file="x", kind="values",
                   values={"cells": r"Leaf Cell Count:\s+(\d+)", "core": {"regex": r"Core Area:\s+(\S+)", "ge": True},
                           "none": r"absent (\d+)"})
    assert json.loads(private._values(pv, vals, raw)) == {"kind": "values", "values": {"cells": 42.0, "core": 20.0, "none": None}}
    assert private._text(pv, Publish(name="t", stage="s", file="x", kind="text", keep=["^Warning"]), raw) == \
        f"Warning: {private.NDA} slow\n"
    (tmp_path / "hook.py").write_text("def pub(path, ctx):\n    return {'ge': 5 / ctx.ge_um2, 'cell': ctx.cell('ND2X2'), 'n': 'SECRET'}\n")
    got = json.loads(private._python(pv, Publish(name="p", stage="s", file="x", kind="python", python="hook.py:pub"), raw))
    assert got == {"ge": 10.0, "cell": "NAND2x2", "n": private.NDA}


def test_publish_run_reports_a_failure_without_the_file(nda: Path) -> None:
    p = config.load_project(nda)
    raw = p.data / "private" / "r1" / "reports" / "signoff"
    raw.mkdir(parents=True)
    (raw / "timing.rpt").write_text("Information: SC8T_SECRET has no path\n")
    (raw / "qor.rpt").write_text("Leaf Cell Count: 7\n")
    written, failures = private.publish_run(p, "r1")
    assert written == ["public/qor.json"] and failures == ["publish timing: no timing path with a slack in the file"]
    assert private.publish_run(p, "r1")[0] == []  # the public file is newer than the raw one
    assert private.publish_run(p, "r1", force=True)[0] == ["public/qor.json"]


# --- end to end

def _wait(capsys, timeout: float = 60) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        code, out, _ = edr(capsys, "--json", "status")
        rows = json.loads(out)["data"]["runs"]
        assert cli.main(["watch", "--once"]) == 0
        capsys.readouterr()
        if rows and not any(board.is_live(r) for r in rows):
            return json.loads(edr(capsys, "--json", "status")[1])["data"]["runs"][0]
        time.sleep(1)
    raise AssertionError("the run did not end")


def test_a_private_stage_end_to_end_leaks_nothing(nda: Path, capsys, tmp_path: Path) -> None:
    subprocess.run(["bash", "setup.sh"], cwd=nda, check=True, capture_output=True)
    code, out, err = edr(capsys, "check")
    assert code == 0, out + err
    for argv in (("plan", "nda", "--show-spec"), ("--json", "plan", "nda"), ("launch", "nda", "--dry-run", "--show-spec"),
                 ("--json", "launch", "nda", "--dry-run")):
        code, out, err = edr(capsys, *argv)
        assert code == 0, out + err
        clean(out + err)
    code, out, err = edr(capsys, "launch", "nda")
    assert code == 0 and "1 started" in out
    clean(out + err)
    run = _wait(capsys)
    assert run["phase"] == "done", run
    run_id = run["run_id"]
    assert "_nda_fast_retime_clock_ns1p6_g" in run_id
    hb = json.loads((tmp_path / ".edr" / "demo" / "nda" / f"{run_id}.json").read_text())
    assert hb["last_cmd"] == "the private command of stage signoff"
    clean(json.dumps(hb))
    root = Path(run["root"])
    raw_log = (root / ".edr" / "private" / "log" / "signoff.log").read_text()
    assert "Vendor Shell SECRET" in raw_log and "signoff.sh" in raw_log
    assert "set_app_option SECRET_RETIME true" in (root / ".edr/private/knobs/signoff.knobs").read_text()

    results, priv = nda / "data" / "results" / run_id, nda / "data" / "private" / run_id
    public_log = (results / "log" / "signoff.log").read_text()
    assert public_log.splitlines() == [
        "# edr: the private command of stage signoff",
        f"[flow] retime=true clock_ns=1.6 effort=medium pdk={private.NDA}",
        f"Warning: cell {private.NDA} has no footprint (PHYS-12)",
        "[flow] done",
        "# edr: 5 lines held back in the private log"]
    assert "Vendor Shell SECRET" in (priv / "log" / "signoff.log").read_text()
    assert (priv / "reports" / "signoff" / "area.rpt").is_file() and not (results / "reports" / "signoff").exists()
    for f in (results / "public").iterdir():
        clean(f.read_text())
    assert sorted(f.name for f in (results / "public").iterdir()) == ["area.csv", "qor.json", "timing.json", "warnings.txt"]

    code, out, _ = edr(capsys, "--json", "metrics", "--source", run["source"])
    rows = {r["name"]: r for r in json.loads(out)["data"]}
    assert rows["area_ge"]["value"] == 2000.0 and rows["area_ge"]["unit"] == "GE"
    assert rows["wns_signoff_ns"]["value"] == -0.05 and rows["core_ge"]["value"] == 1000.0
    code, out, _ = edr(capsys, "metrics", "--source", run["source"], "--unit", "kGE")
    assert code == 0 and "kGE" in out

    for argv in (("brief",), ("brief", "--run", "fast@nda"), ("status", "fast@nda"), ("events",),
                 ("--json", "brief"), ("compare", "fast@nda", "fast@nda", "--instances", "--metric", "area_ge")):
        code, out, err = edr(capsys, *argv)
        assert code in (0, 2), (argv, out, err)
        clean(out + err)
    code, out, _ = edr(capsys, "brief")
    assert "## The knobs" in out and "- `clock_ns` (float from 0.5 to 20, default `2.0`): the target clock period" in out
    assert "## The private flow" in out and "`public/timing.json` from `signoff`: the timing paths" in out
    for f in (nda / "data" / "board").iterdir():
        clean(f.read_text())
    code, out, err = edr(capsys, "export", "--source", run["source"], "--out", str(tmp_path / "snap"), "--files")
    assert code == 0, out + err
    manifest = json.loads((tmp_path / "snap" / "manifest.json").read_text())
    assert any(c["cmd"] == private.PRIVATE for r in manifest["runs"] for c in r["record"]["commands"])
    for f in (tmp_path / "snap").rglob("*"):
        if f.is_file():
            clean(f.read_text(errors="replace"))
    from edarunner.db import Database
    with Database(nda / "data" / "edr.db") as db:
        params = {(r["key"], r["origin"]): r["value"] for r in db.parameters(run_id)}
    assert params[("knob.retime", "spec")] == "true" and params[("knob.clock_ns", "spec")] == "1.6"


def test_the_private_flow_template_loads(capsys, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1] / "examples" / "private-flow" / "project"
    p = config.load_project(root)
    assert [n for n, st in p.stages.items() if st.private] == ["synth", "sta"]
    assert len(p.knobs) == 6 and set(p.private.knobs) == set(p.knobs)
    batch = config.load_batch(p, "sweep")
    assert [launch.build_tag(p, j) for j in batch.jobs] == ["", "clock_ns1p6_retime_efforthigh", "clock_ns1p6_vtmixed"]
    assert {m.file.split("/")[1] for m in p.metrics.values()} <= {f"{n}.{private.EXT[x.kind]}" for n, x in p.private.publish.items()}
    monkeypatch.chdir(root)
    code, out, err = edr(capsys, "brief")
    assert code == 0 and "## The private flow" in out and "fc_shell" not in out + err and "nonfree" not in out + err
