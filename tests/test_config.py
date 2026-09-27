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
    assert power.is_group and power.parallel == 2
    assert power.needs.tools == {"demo": 1} and power.budget.per == "task"
    assert p.stages["synth"].needs.tools == {"demo": 1} and p.stages["synth"].retry.max == 2
    assert p.stages["pnr"].retry is None and p.stages["export"].prune == {"netlist": ["out"]}
    assert p.metrics["area_cell_um2"].stage == ["synth", "pnr"] and p.metrics["area_cell_um2"].step == "*"
    assert p.metrics["power_w"].stage == ["power"] and p.metrics["power_w"].csv["column"] == "total_w"
    assert p.metrics["energy_nj"].stage == [] and p.metrics["energy_nj"].expr == "power_w * window_ns"
    assert p.tasks["k_big"].fields == {"kernel": "softmax", "test": "SOFTMAX_R197", "args": "ROWS=197"}
    assert p.tasks["k_big"].budget.hours == 2 and p.tasks["k_small"].budget is None
    assert p.task_resolver == ""
    assert p.site.hosts["local"].cores == 4 and p.site.hosts["local"].scratch is None
    assert p.site.hosts["local"].tools == {"demo": "1.0"} and p.site.hosts["local"].has("demo")
    assert p.site.tools["demo"].seats == 10 and p.site.tools["demo"].probe == ["bash", "{root}/flow/seats.sh"]
    assert p.site.telegram is None
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
        ("parallel = 2", 'parallel = 2\nafter = "export"', "unknown key 'stages.power.after'"),
        ('stage = ["synth", "pnr"]', 'stage = ["synth", "gone"]', "stage names unknown stage 'gone'"),
        ('task_dir = "simulation/tests/{config}/{task.test}"', "", "task group and needs task_dir"),
        ('tools = ["demo"]', 'tools = ["fc"]', "stages.synth.needs.tools names unknown tool 'fc'"),
        ("tools = { demo = 1 }", "tools = { questa = 1 }", "unknown tool 'questa'"),
        ('tools = ["demo"]', 'licence = "demo"', "stages.synth.needs.licence is gone; use stages.synth.needs.tools"),
        ('tools = ["demo"]', "tools = 1", "stages.synth.needs.tools must be a list of names or"),
        ('tools = ["demo"]', 'tools = { demo = "2" }', "stages.synth.needs.tools must be a list of names or"),
        ('regex = \'^i_top\\s+(\\S+)\'', "", "exactly one of"),
        ("stale_s = 30", 'stale_s = "30"', "limits.stale_s must be int, not str"),
        ("host_free_min_gb = 1", 'host_free_min_gb = "1"', "limits.host_free_min_gb must be float, not str"),
        ("stagger_s = 0", "stagger_s = 0\nkill_hung = 1", "limits.kill_hung must be bool, not int"),
        ('marker = "/edr/"', "marker = 1", "safety.marker must be str, not int"),
        ('steps = ["setup", "analyze", "elaborate", "synth"]', 'steps = "setup"', "stages.synth.steps must be list, not str"),
        ('prune = { netlist = ["out"] }', 'prune = ["out"]', "stages.export.prune must be dict, not list"),
        ("budget = { hours = 1 }", "budget = { hours = true }", "budget.hours must be float, not bool"),
    ],
)
def test_refused(tmp_path, old, new, msg):
    with pytest.raises(ConfigError, match=msg):
        config.load_project(demo_copy(tmp_path, old, new))


def test_site_tools_and_host_tools(tmp_path):
    root = demo_copy(tmp_path)
    site = root / "site.toml"
    text = site.read_text()
    site.write_text(text.replace('tools = { demo = "1.0" }', 'tools = ["demo"]'))
    assert config.load_project(root).site.hosts["local"].tools == {"demo": ""}
    site.write_text(text.replace('tools = { demo = "1.0" }\n', ""))
    host = config.load_project(root).site.hosts["local"]
    assert host.tools is None and host.has("demo") and host.has("anything")
    site.write_text(text.replace('tools = { demo = "1.0" }', "tools = []"))
    assert not config.load_project(root).site.hosts["local"].has("demo")
    site.write_text(text + '\n[tools.plain]\n')
    plain = config.load_project(root).site.tools["plain"]
    assert plain.seats is None and plain.probe == []
    for old, new, match in (('tools = { demo = "1.0" }', 'tools = ["nope"]', "hosts.local.tools names unknown tool 'nope'"),
                            ('tools = { demo = "1.0" }', "tools = 1", "hosts.local.tools must be dict, not int"),
                            ("seats = 10", 'seats = "10"', "tools.demo.seats must be int, not str"),
                            ('probe = ["bash", "{root}/flow/seats.sh"]', 'probe = "bash seats.sh"', "tools.demo.probe must be list, not str"),
                            ("[tools.demo]", "[licences.demo]", r"\[licences\] is gone; declare \[tools.<name>\]")):
        site.write_text(text.replace(old, new))
        with pytest.raises(ConfigError, match=match):
            config.load_project(root)
    site.write_text(text)
    tasks = root / "tasks.toml"
    tasks.write_text(tasks.read_text().replace("budget = { hours = 2 }", "budget = { hours = 2 }\nneeds = { tools = { demo = 2 } }"))
    assert config.load_project(root).tasks["k_big"].needs.tools == {"demo": 2}
    tasks.write_text(tasks.read_text().replace("tools = { demo = 2 }", 'licence = "demo"'))
    with pytest.raises(ConfigError, match="tasks.k_big.needs.licence is gone"):
        config.load_project(root)


def test_int_fits_a_float_field(tmp_path):
    p = config.load_project(demo_copy(tmp_path, "disk_gb = 1 }", "disk_gb = 2 }"))
    assert p.stages["synth"].budget.disk_gb == 2 and p.tasks["k_big"].budget.hours == 2


def test_site_types_and_user_id(tmp_path):
    root = demo_copy(tmp_path)
    site = root / "site.toml"
    text = site.read_text()
    site.write_text(text.replace("cores = 4", 'cores = "4"'))
    with pytest.raises(ConfigError, match="hosts.local.cores must be int, not str"):
        config.load_project(root)
    site.write_text(text + "\n[telegram]\nchat_id = 42\n")
    assert config.load_project(root).site.telegram.user_id is None
    site.write_text(text + "\n[telegram]\nchat_id = 42\nuser_id = 7\n")
    tg = config.load_project(root).site.telegram
    assert (tg.chat_id, tg.user_id) == (42, 7)
    site.write_text(text + "\n[telegram]\nchat_id = 42\nuser_id = \"7\"\n")
    with pytest.raises(ConfigError, match="telegram.user_id must be int, not str"):
        config.load_project(root)
    site.write_text(text + "\n[telegram]\nchat_id = \"42\"\n")
    with pytest.raises(ConfigError, match="telegram.chat_id must be int, not str"):
        config.load_project(root)


def test_project_telegram_overrides_the_site(tmp_path):
    root = demo_copy(tmp_path)
    site, edr = root / "site.toml", root / "edr.toml"
    site_text, edr_text = site.read_text(), edr.read_text()
    site.write_text(site_text + '\n[telegram]\nchat_id = 42\nuser_id = 7\n[telegram.commands.x]\nhelp = "x"\nrun = ["true"]\n')
    edr.write_text(edr_text + '\n[telegram]\ntoken_file = "bot.token"\nchat_id = -100\n')
    tg = config.load_project(root).site.telegram
    assert (tg.token_file, tg.chat_id, tg.user_id, list(tg.commands)) == (root / "bot.token", -100, 7, ["x"])
    edr.write_text(edr_text + '\n[telegram]\nuser_id = 9\ntopic_id = 17\n')
    tg = config.load_project(root).site.telegram
    assert (tg.chat_id, tg.user_id, tg.token_file.name, tg.topic_id) == (42, 9, "telegram.token", 17)
    for bad, match in (('chat_id = "1"', "telegram.chat_id must be int, not str"), ("token_file = 5", "token_file must be str"),
                       ('topic_id = "17"', "telegram.topic_id must be int, not str"),
                       ("[telegram.commands.y]", "unknown key 'telegram.commands'")):
        edr.write_text(edr_text + f"\n[telegram]\n{bad}\n")
        with pytest.raises(ConfigError, match=match):
            config.load_project(root)
    site.write_text(site_text)
    edr.write_text(edr_text + "\n[telegram]\nuser_id = 9\n")
    with pytest.raises(ConfigError, match="missing key 'telegram.chat_id'"):
        config.load_project(root)
    edr.write_text(edr_text + "\n[telegram]\nchat_id = 5\n")
    assert config.load_project(root).site.telegram.chat_id == 5


def test_marks_defaults_site_and_project_override(tmp_path):
    root = demo_copy(tmp_path)
    site, edr = root / "site.toml", root / "edr.toml"
    site_text, edr_text = site.read_text(), edr.read_text()
    m = config.load_project(root).site.marks
    assert (m.cores, m.ram, m.scratch, m.gpu) == ([0.6, 0.8, 0.9], [0.6, 0.8, 0.9], [0.7, 0.85, 0.95], [0.6, 0.8, 0.9])
    site.write_text(site_text + "\n[marks]\ncores = [0.5, 0.7, 0.8]\nram = [0, 0.5, 1]\n")
    edr.write_text(edr_text + "\n[marks]\ncores = [0.1, 0.2, 0.3]\n")
    m = config.load_project(root).site.marks
    assert (m.cores, m.ram, m.scratch) == ([0.1, 0.2, 0.3], [0, 0.5, 1], [0.7, 0.85, 0.95])
    for bad, match in (("gpu = [0.9, 0.8, 0.95]", "marks.gpu must be three ascending numbers between 0 and 1"),
                       ("ram = [0.6, 0.8]", "marks.ram must be three ascending"),
                       ("scratch = [0.5, 0.8, 1.2]", "marks.scratch must be three ascending"),
                       ("cores = [0.5, 0.8, true]", "marks.cores must be three ascending"),
                       ('cores = "0.5"', "marks.cores must be list, not str"),
                       ("disk = [0.5, 0.8, 0.9]", "unknown key 'marks.disk'")):
        edr.write_text(edr_text + f"\n[marks]\n{bad}\n")
        with pytest.raises(ConfigError, match=match):
            config.load_project(root)


def test_digest_at_is_a_time_of_day(tmp_path):
    root = demo_copy(tmp_path)
    edr = root / "edr.toml"
    text = edr.read_text()
    assert config.load_project(root).limits.digest_at == ""
    edr.write_text(text.replace("[limits]", '[limits]\ndigest_at = "08:00"'))
    assert config.load_project(root).limits.digest_at == "08:00"
    for bad in ('"8:00"', '"24:00"', '"08:00:00"'):
        edr.write_text(text.replace("[limits]", f"[limits]\ndigest_at = {bad}"))
        with pytest.raises(ConfigError, match="limits.digest_at must be HH:MM"):
            config.load_project(root)


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
