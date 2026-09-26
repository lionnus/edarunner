"""config.py against examples/local-demo. Every file write goes to tmp_path."""

import shutil
from pathlib import Path

import pytest

from edarunner import config
from edarunner.config import ConfigError

DEMO = Path(__file__).resolve().parents[1] / "examples" / "local-demo"


def demo_copy(tmp_path: Path, old: str = "", new: str = "") -> Path:
    """Copy the demo project and replace `old` with `new` in edr.toml."""
    root = tmp_path / "demo"
    shutil.copytree(DEMO, root, ignore=shutil.ignore_patterns("repo", "wt", "data"))
    if old:
        edr = root / "edr.toml"
        text = edr.read_text()
        assert old in text
        edr.write_text(text.replace(old, new))
    return root


def test_demo_end_to_end():
    p = config.load_project(DEMO)
    assert p.project == "demo" and p.root == DEMO
    assert p.site.path == DEMO / "site.toml"
    assert p.state == Path("~/.edr/demo").expanduser()
    assert p.data == DEMO / "data" and p.source.repo == DEMO / "repo"
    assert p.run_prefix == "{user}/edr/{project}"
    assert p.safety.min_depth == 3 and p.limits.heartbeat_s == 5 and p.placement.max_per_host == 4
    assert list(p.stages) == ["synth", "pnr", "export", "power"]
    power = p.stages["power"]
    assert power.is_group and power.after == "export" and power.parallel == 2
    assert power.needs.licence == {"demo": 1} and power.budget.per == "task"
    assert p.stages["synth"].needs.licence == "demo" and p.stages["synth"].retry.max == 2
    assert p.stages["pnr"].retry is None and p.stages["export"].prune == {"netlist": ["out"]}
    assert p.metrics["area_cell_um2"].stage == ["synth", "pnr"] and p.metrics["area_cell_um2"].step == "*"
    assert p.metrics["power_w"].stage == ["power"] and p.metrics["power_w"].csv["column"] == "total_w"
    assert p.metrics["energy_nj"].stage == [] and p.metrics["energy_nj"].expr == "power_w * window_ns"
    assert p.tasks["k_big"].fields == {"kernel": "softmax", "test": "SOFTMAX_R197", "args": "ROWS=197"}
    assert p.tasks["k_big"].budget.hours == 2 and p.tasks["k_small"].budget is None
    assert p.task_resolver == ""
    assert p.site.hosts["local"].cores == 4 and p.site.hosts["local"].scratch is None
    assert p.site.licences["demo"].floor == 2 and p.site.telegram is None
    assert p.site.ssh_timeout_s == 20 and p.site.scratch == ["/tmp/edr-demo"]

    b = config.load_batch(p, "demo")
    assert b.batch == "demo" and b.source == "HEAD" and b.path == DEMO / "jobs" / "demo.toml"
    assert [j.label for j in b.jobs] == ["a", "b_nodw"]
    assert b.jobs[0].stages[-1] == "power" and b.jobs[0].tasks == ["k_small", "k_big"]
    assert b.jobs[1].overrides == {"DW": "0"} and b.jobs[1].host == "local"
    assert config.load_batch(p, "jobs/demo.toml").path == b.path
    assert config.resolve_task(p, "k_small").fields["kernel"] == "gemm"


def test_render_and_placeholders():
    p = config.load_project(DEMO)
    v = config.placeholders(p, run_id="r1", task={"kernel": "gemm"}, overrides={"DW": "0", "N": 8})
    assert v["project"] == "demo" and v["project_root"] == str(DEMO) and v["site_dir"] == str(DEMO)
    assert v["user"] and v["task.kernel"] == "gemm" and v["overrides"] == "DW=0 N=8"
    assert config.render("x {run_id} {task.kernel} ${SHELL} {overrides}", v) == "x r1 gemm ${SHELL} DW=0 N=8"
    with pytest.raises(ConfigError, match=r"missing placeholder \{task\.test\}"):
        config.render("cd {task.test}", v)


@pytest.mark.parametrize(
    "old, new, msg",
    [
        ('project = "demo"', 'project = "demo"\ncolour = 1', "unknown key 'colour'"),
        ("parallel = 2", "parallel = 2\nbogus = 1", "unknown key 'stages.power.bogus'"),
        ("stagger_s = 0", "stagger_s = 0\nfoo = 1", "unknown key 'limits.foo'"),
        ('after = "export"', 'after = "nope"', "after names unknown stage 'nope'"),
        ('stage = ["synth", "pnr"]', 'stage = ["synth", "gone"]', "stage names unknown stage 'gone'"),
        ('task_dir = "simulation/tests/{config}/{task.test}"', "", "task group and needs task_dir"),
        ('licence = "demo"', 'licence = "fc"', "unknown licence 'fc'"),
        ("licence = { demo = 1 }", "licence = { questa = 1 }", "unknown licence 'questa'"),
        ('regex = \'^i_top\\s+(\\S+)\'', "", "exactly one of"),
    ],
)
def test_refused(tmp_path, old, new, msg):
    with pytest.raises(ConfigError, match=msg):
        config.load_project(demo_copy(tmp_path, old, new))


def test_duplicate_label(tmp_path):
    root = demo_copy(tmp_path)
    jobs = root / "jobs" / "demo.toml"
    jobs.write_text(jobs.read_text().replace('label = "b_nodw"', 'label = "a"'))
    with pytest.raises(ConfigError, match="two jobs share the label 'a'"):
        config.load_batch(config.load_project(root), "demo")


def test_site_path_forms(tmp_path, monkeypatch):
    root = demo_copy(tmp_path, 'site = "site.toml"', 'site = "etc/site.toml"')
    (root / "etc").mkdir()
    (root / "site.toml").rename(root / "etc" / "site.toml")
    assert config.load_project(root).site.path == root / "etc" / "site.toml"
    assert config.load_project(root, "etc/site.toml").site.path == root / "etc" / "site.toml"
    monkeypatch.setenv("HOME", str(tmp_path))
    shutil.copy(root / "etc" / "site.toml", tmp_path / "site.toml")
    p = config.load_project(root, "~/site.toml")
    assert p.site.path == tmp_path / "site.toml" and p.state == tmp_path / ".edr" / "demo"


def test_pattern_resolver(tmp_path):
    root = demo_copy(tmp_path)
    tasks = root / "tasks.toml"
    tasks.write_text(tasks.read_text() + '\n[pattern]\nresolver = "python:hooks/tasks.py:spec_of"\n')
    (root / "hooks").mkdir()
    (root / "hooks" / "tasks.py").write_text(
        "def spec_of(task_id):\n"
        '    k, _, n = task_id.partition("_")\n'
        '    return {"kernel": k, "test": task_id.upper(), "args": "N=" + n} if n.isdigit() else None\n'
    )
    p = config.load_project(root)
    assert config.resolve_task(p, "gemm_64").fields == {"kernel": "gemm", "test": "GEMM_64", "args": "N=64"}
    assert config.resolve_task(p, "k_small").fields["kernel"] == "gemm"
    with pytest.raises(ConfigError, match="unknown task 'gemm_x'"):
        config.resolve_task(p, "gemm_x")
