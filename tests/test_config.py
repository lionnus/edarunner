"""config.py against examples/local-demo. Every file write goes to tmp_path."""

import shutil
from pathlib import Path

import pytest

from edarunner import config, hosts
from edarunner.config import ConfigError
from helpers_driver import DEMO


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
    assert p.state_dir == Path("~/.edr/demo").expanduser()
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
    assert p.metrics["energy_nj"].stage == ["power"] and p.metrics["energy_nj"].python == "hooks/energy.py:energy_nj"
    assert p.tasks["k_big"].fields == {"kernel": "softmax", "test": "SOFTMAX_R197", "args": "ROWS=197"}
    assert p.tasks["k_big"].budget.hours == 2 and p.tasks["k_small"].budget is None
    assert p.task_resolver == ""
    assert p.site.hosts["local"].cores == 4 and p.site.hosts["local"].scratch is None
    assert p.site.hosts["local"].tools == {"demo": "1.0"} and p.site.hosts["local"].has("demo")
    assert p.site.tools["demo"].seats == 10 and p.site.tools["demo"].probe == ["bash", "{root}/flow/seats.sh"]
    assert p.site.commands == {}
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
        ('stage = ["synth", "pnr"]', 'stage = ["synth", "gone"]', "stage names unknown stage 'gone'"),
        ('task_dir = "simulation/tests/{config}/{task.test}"', "", "task group and needs task_dir"),
        ('tools = ["demo"]', 'tools = ["fc"]', "stages.synth.needs.tools names unknown tool 'fc'"),
        ("tools = { demo = 1 }", "tools = { questa = 1 }", "unknown tool 'questa'"),
        ('tools = ["demo"]', "tools = 1", "stages.synth.needs.tools must be a list of names or"),
        ('tools = ["demo"]', 'tools = { demo = "2" }', "stages.synth.needs.tools must be a list of names or"),
        ('regex = \'^i_top\\s+(\\S+)\'', "", "exactly one of"),
        ("stale_s = 30", 'stale_s = "30"', "limits.stale_s must be int, not str"),
        ("stale_s = 30", 'stale_s = "30"', "limits.stale_s must be int, not str"),
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
                            ('probe = ["bash", "{root}/flow/seats.sh"]', 'probe = "bash seats.sh"', "tools.demo.probe must be list, not str")):
        site.write_text(text.replace(old, new))
        with pytest.raises(ConfigError, match=match):
            config.load_project(root)
    site.write_text(text)
    tasks = root / "tasks.toml"
    tasks.write_text(tasks.read_text().replace("budget = { hours = 2 }", "budget = { hours = 2 }\nneeds = { tools = { demo = 2 } }"))
    assert config.load_project(root).tasks["k_big"].needs.tools == {"demo": 2}


def test_scheduler_backend(tmp_path):
    root = demo_copy(tmp_path)
    site = root / "site.toml"
    text = site.read_text()
    assert config.load_project(root).site.scheduler.backend == "ssh"
    site.write_text(text + '\n[scheduler]\nbackend = "local"\n')
    assert config.load_project(root).site.scheduler.backend == "local"
    site.write_text(text.replace('probe = ["bash"', 'licence = "fc"\nprobe = ["bash"') + '\n[scheduler]\nbackend = "condor"\n'
                    'submit_via = ["ssh", "sub"]\ntree_root = "/net/{user}"\nmax_jobs = 3\noptions = ["+Owner = 1"]\n')
    s = config.load_project(root).site
    assert (s.scheduler.backend, s.scheduler.submit_via, s.scheduler.max_jobs, s.tools["demo"].licence) == \
        ("condor", ["ssh", "sub"], 3, "fc")
    for extra, match in (('backend = "pbs"', "scheduler.backend 'pbs' is not one of ssh, local, condor, slurm, lsf"),
                         ('backend = "slurm"', "needs scheduler.tree_root"),
                         ('backend = "ssh"\nqueues = "x"', "unknown key 'scheduler.queues'"),
                         ('max_jobs = "3"', "scheduler.max_jobs must be int"),
                         ('backend = "local"\n[hosts.far]\ncores = 1\nram_gb = 1', "may name only local")):
        site.write_text(text + "\n[scheduler]\n" + extra + "\n")
        with pytest.raises(ConfigError, match=match):
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


def test_the_chat_belongs_to_the_user_file(tmp_path):
    root, user = demo_copy(tmp_path), tmp_path / "user.toml"
    user.write_text('[telegram]\nchat_id = 42\nuser_id = 7\ntopics = true\n[telegram.commands.x]\nhelp = "x"\nrun = ["true"]\n')
    tg = config.load_user(user).telegram
    token = tmp_path / ".config" / "edarunner" / "telegram.token"  # HOME is tmp_path
    assert (tg.chat_id, tg.user_id, tg.topics, tg.token_file, list(tg.commands)) == (42, 7, True, token, ["x"])
    user.write_text("[telegram]\nchat_id = 42\n")
    assert config.load_user(user).telegram.user_id is None and not config.load_user(user).telegram.topics
    for bad, match in (('chat_id = "1"', "telegram.chat_id must be int, not str"), ("token_file = 5", "token_file must be str"),
                       ("user_id = 9", "missing key 'telegram.chat_id'"), ('chat_id = 1\nuser_id = "7"', "user_id must be int"),
                       ("chat_id = 1\ntopics = 1", "telegram.topics must be bool")):
        user.write_text(f"[telegram]\n{bad}\n")
        with pytest.raises(ConfigError, match=match):
            config.load_user(user)
    edr = root / "edr.toml"
    edr_text = edr.read_text()
    for text, key in (("telegram_poll = false\n" + edr_text, "telegram_poll"), (edr_text + "\n[telegram]\nchat_id = 5\n", "telegram"),
                      (edr_text.replace("[limits]", '[limits]\ndigest_at = "08:00"'), "limits.digest_at")):
        edr.write_text(text)
        with pytest.raises(ConfigError, match=f"unknown key '{key}'"):
            config.load_project(root)


def test_the_disk_floor_belongs_to_the_site_and_a_host_may_set_its_own(tmp_path):
    root = demo_copy(tmp_path)
    site, edr = root / "site.toml", root / "edr.toml"
    site_text, edr_text = site.read_text(), edr.read_text()
    p = config.load_project(root)
    assert hosts.floor(p.site, "local") == 1 and hosts.floor(p.site, "elsewhere") == 1
    site.write_text(site_text.replace("host_free_min_gb = 1\n", "") + "\n[hosts.big]\ncores = 64\nram_gb = 256\n"
                    "host_free_min_gb = 500\n")
    p = config.load_project(root)
    assert hosts.floor(p.site, "local") == 100 and hosts.floor(p.site, "big") == 500
    site.write_text(site_text.replace("host_free_min_gb = 1", 'host_free_min_gb = "1"'))
    with pytest.raises(ConfigError, match="host_free_min_gb must be float, not str"):
        config.load_project(root)
    site.write_text(site_text)
    for extra, match in (("\n[marks]\ncores = [0.5, 0.7, 0.8]\n", "unknown key 'marks'"),
                         ("", "unknown key 'limits.host_free_min_gb'")):
        text = edr_text + extra if extra else edr_text.replace("grace_s = 10\n", "grace_s = 10\nhost_free_min_gb = 5\n")
        edr.write_text(text)
        with pytest.raises(ConfigError, match=match):
            config.load_project(root)

def test_digest_at_is_a_time_of_day(tmp_path):
    user = tmp_path / "user.toml"
    assert config.load_user(user).digest_at == ""
    user.write_text('digest_at = "08:00"\n')
    assert config.load_user(user).digest_at == "08:00"
    for bad in ('"8:00"', '"24:00"', '"08:00:00"', "8"):
        user.write_text(f"digest_at = {bad}\n")
        with pytest.raises(ConfigError, match="digest_at must be HH:MM"):
            config.load_user(user)


def test_alerts_names_the_opt_in_kinds(tmp_path):
    user = tmp_path / "user.toml"
    assert config.load_user(user).alerts == []
    user.write_text('alerts = ["done", "metrics"]\n')
    assert config.load_user(user).alerts == ["done", "metrics"]
    for bad in ('["dead"]', '"done"', '[{ kind = "done" }]'):
        user.write_text(f"alerts = {bad}\n")
        with pytest.raises(ConfigError, match="alerts is a list of done, metrics"):
            config.load_user(user)


def test_duplicate_label(tmp_path):
    root = demo_copy(tmp_path)
    jobs = root / "jobs" / "demo.toml"
    jobs.write_text(jobs.read_text().replace('label = "b_nodw"', 'label = "a"'))
    with pytest.raises(ConfigError, match="two jobs share the label 'a'"):
        config.load_batch(config.load_project(root), "demo")


def test_job_vars_and_optional_config(tmp_path):
    root = demo_copy(tmp_path)
    jobs = root / "jobs" / "demo.toml"
    text = jobs.read_text()
    b = config.load_batch(config.load_project(root), "demo")
    assert b.jobs[0].vars == {"netlist_stage": "11"} and b.jobs[1].vars == {}
    jobs.write_text(text.replace('label = "b_nodw"\nconfig = "demo"\n', 'label = "b_nodw"\n'))
    assert config.load_batch(config.load_project(root), "demo").jobs[1].config == ""
    for old, new, match in (("vars = { netlist_stage = 11 }", 'vars = { "not-id" = 1 }', "vars key 'not-id' is not an identifier"),
                            ("vars = { netlist_stage = 11 }", "vars = { x = [1] }", r"job\[0\].vars.x must be a string or a number")):
        jobs.write_text(text.replace(old, new))
        with pytest.raises(ConfigError, match=match):
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
    assert p.site.path == tmp_path / "site.toml" and p.state_dir == tmp_path / ".edr" / "demo"


def test_pattern_resolver(tmp_path):
    root = demo_copy(tmp_path)
    tasks = root / "tasks.toml"
    tasks.write_text(tasks.read_text() + '\n[pattern]\nresolver = "python:hooks/tasks.py:spec_of"\n')
    (root / "hooks").mkdir(exist_ok=True)
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


def test_metric_reduce_and_pass_rule(tmp_path):
    root = demo_copy(tmp_path)
    p = config.load_project(root)
    assert (p.metrics["wns_ns"].reduce, p.metrics["setup_violations"].reduce) == ("min", "sum")
    assert (p.metrics["setup_violations"].pass_, p.metrics["area_cell_um2"].pass_) == ("== 0", "")
    toml = root / "edr.toml"
    text = toml.read_text()
    for old, new, match in (('reduce = "min"', 'reduce = "mean"', "wns_ns.reduce needs regex and is one of first, last"),
                            ('json = "window_ns"', 'json = "window_ns"\nreduce = "max"', "window_ns.reduce needs regex"),
                            ('pass = "== 0"', 'pass = "0"', 'setup_violations.pass is an operator and a number'),
                            ('pass = "== 0"', 'pass = "== none"', "pass is an operator and a number"),
                            ('pass = "== 0"', "pass = 0", "pass is an operator and a number"),
                            ('pass = "== 0"', 'pass_ = "== 0"', "unknown key 'metrics.setup_violations.pass_'")):
        toml.write_text(text.replace(old, new))
        with pytest.raises(ConfigError, match=match):
            config.load_project(root)


def test_metric_scale(tmp_path):
    root = demo_copy(tmp_path)
    toml = root / "edr.toml"
    text, window = toml.read_text(), 'json = "window_ns"'
    toml.write_text(text.replace(window, window + "\nscale = 1e-3"))
    p = config.load_project(root)
    assert (p.metrics["window_ns"].scale, p.metrics["power_w"].scale) == (1e-3, 1.0)
    toml.write_text(text.replace(window, window + "\nscale = 0"))
    with pytest.raises(ConfigError, match="window_ns.scale is a factor other than 0"):
        config.load_project(root)


def test_metric_record(tmp_path):
    root = demo_copy(tmp_path)
    p = config.load_project(root)
    assert (p.metrics["area_cell_um2"].record, p.metrics["wns_ns"].record) == ({"stage": "pnr", "from": 5}, None)
    toml = root / "edr.toml"
    text = toml.read_text()
    rec, window = 'record = { stage = "pnr", from = 5 }', 'json = "window_ns"'
    for old, new, match in ((rec, 'record = { stage = "power" }', "area_cell_um2.record is"),  # not a stage of the metric
                            (window, window + '\nstep = "*"\nrecord = { stage = "power" }', "window_ns.record is"),  # no steps
                            ('step = "*"\nfile = "reports/{step}/area.rpt"', 'file = "reports/5/area.rpt"',
                             "the metric needs step"),
                            (rec, 'record = { stage = "pnr", from = "5" }', "area_cell_um2.record is"),
                            (rec, 'record = { stage = "pnr", upto = 5 }', "unknown key 'metrics.area_cell_um2.record.upto'"),
                            (rec, 'record = "pnr"', "'metrics.area_cell_um2.record' must be a table")):
        toml.write_text(text.replace(old, new))
        with pytest.raises(ConfigError, match=match):
            config.load_project(root)
    toml.write_text(text.replace(rec, 'record = { stage = "pnr" }'))
    assert config.load_project(root).metrics["area_cell_um2"].record == {"stage": "pnr"}


def test_metric_table_and_ge_um2(tmp_path):
    root = demo_copy(tmp_path)
    toml = root / "edr.toml"
    table = 'table = { instance = "full", value = "total_w", top = { phase = "WHOLE" }, where = { phase = ["WHOLE", "T*"] }, max_depth = 3 }'
    text = toml.read_text().replace("csv = { where = { phase = \"WHOLE\" }, column = \"total_w\" }", table)
    toml.write_text(text.replace("run_prefix =", "ge_um2 = 0.2\nrun_prefix ="))
    p = config.load_project(root)
    assert p.metrics["power_w"].table["top"] == {"phase": "WHOLE"} and p.ge_um2 == 0.2
    for old, new, match in ((table, 'table = { instance = "full", value = "total_w" }', "power_w.table needs instance, value and top"),
                            (table, table.replace("max_depth = 3", "max_depth = -1"), "max_depth is the deepest depth"),
                            (table, table.replace("where = {", "keep = {"), "unknown key 'metrics.power_w.table.keep'"),
                            (table, 'table = "full"', "'metrics.power_w.table' must be a table"),
                            ("run_prefix =", "ge_um2 = 0\nrun_prefix =", "ge_um2 is the area of one gate equivalent")):
        toml.write_text(text.replace(old, new))
        with pytest.raises(ConfigError, match=match):
            config.load_project(root)
