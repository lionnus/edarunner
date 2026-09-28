"""Made-up reports with the block structure of a real flow's reports; the names and numbers are invented."""

from __future__ import annotations

RULE = "-" * 40


def report_qor(*blocks: tuple[str, str, dict[str, object]]) -> str:
    """A report_qor: one block per (scenario, path group, {field: value}), a blank line between blocks."""
    return "\n".join(f"Scenario           '{scenario}'\nTiming Path Group  '{group}'\n{RULE}\n"
                     + "".join(f"{k + ':':<32}{v}\n" for k, v in fields.items()) + f"{RULE}\n"
                     for scenario, group, fields in blocks)


def demo_qor(step: int) -> str:
    """The qor.rpt that examples/local-demo/flow/flow.sh writes at `step`."""
    return report_qor(("func_fast", "reg2reg", {"Worst Hold Violation": "-0.001", "No. of Hold Violations": 1}),
                      ("func_slow", "in2reg", {"Critical Path Slack": "0.01", "No. of Violating Paths": 0}),
                      ("func_slow", "reg2reg", {"Critical Path Slack": f"-0.0{step}", "No. of Violating Paths": step}))


def area_report(top: float, rows: list[tuple[str, float]]) -> str:
    """A Synopsys `report_area -hierarchy` of `top` um2 with one line per (path from the top, area)."""
    body = "".join(f"{name:<34}{area:>12.3f}{area / top * 100:>9.1f}{0:>13.3f}{0:>13.3f}{0:>14.3f}  {name.split('/')[-1]}\n"
                   for name, area in [("top_wrap", top), *rows])
    return f"Report : area\nTotal cell area:{top:>32.3f}\n\nHierarchical cell{'Absolute':>29}{'Percent':>9}\n{RULE}\n{body}{RULE}\n"


def area_hier(top: float) -> str:
    """A Synopsys `report_area -hierarchy` of `top` um2: i_top with 99 % of it, blk_a with 60 % and blk_b with 30 %."""
    return area_report(top, [("i_top", top * 0.99), ("i_top/blk_a", top * 0.6), ("i_top/blk_b", top * 0.3)])


# A design of 1024 um2 as (path from the top, area): the adder u_add sits at depth 3, four lanes at depth 5.
TREE = [("i_top", 960.0), ("i_top/u_core", 512.0), ("i_top/u_sum", 256.0), ("i_top/u_sum/u_add", 64.0),
        ("i_top/u_vec", 128.0), ("i_top/u_vec/u_bank", 128.0), ("i_top/u_vec/u_bank/u_lanes", 128.0),
        *((f"i_top/u_vec/u_bank/u_lanes/u_lane_{k}", 32.0) for k in range(4))]


def power_table(rows: list[tuple[str, float]], parts: dict[str, float]) -> str:
    """A tidy per-instance power CSV of the design `rows` of 1024 um2: per part, such as the whole window or a trace
    slice, the top `chip` and each instance draw the part's watts times their share of the area. `full` is the path
    from the top, `instance` the leaf name."""
    lines = ["phase,instance,full,depth,total_w"]
    for part, watts in parts.items():
        for path, area in [("", 1024.0), *rows]:
            full = f"chip/{path}".rstrip("/")
            lines.append(f"{part},{full.split('/')[-1]},{full},{full.count('/')},{watts * area / 1024:.6e}")
    return "\n".join(lines) + "\n"


# The hold scenario first, and its reg2reg block has no setup slack; the setup slacks are on lines 11, 18 and 25.
QOR = report_qor(("func_fast", "reg2reg", {"Worst Hold Violation": "-0.002", "No. of Hold Violations": 3}),
                 ("func_slow", "in2reg_default", {"Critical Path Slack": "0.004", "No. of Violating Paths": 0}),
                 ("func_slow", "reg2out", {"Critical Path Slack": "-0.087", "No. of Violating Paths": 41}),
                 ("func_slow", "reg2reg", {"Critical Path Slack": "-0.031", "No. of Violating Paths": 212}))


# A tidy per-instance power report: the whole window, then a trace slice. A build without the block u_blk_b
# gives POWER_NO_BLK.
POWER = """\
phase,instance,total_w
WHOLE,top,0.2500
WHOLE,u_lane_0,0.1000
WHOLE,u_blk_b,0.0200
TRACE_0,top,0.3100
TRACE_0,u_lane_0,0.1500
"""
POWER_NO_BLK = "".join(ln + "\n" for ln in POWER.splitlines() if "u_blk_b" not in ln)

# The cycle counts of a bench suite, one row per test.
SUITE = "name,cycles\nGEMM_M64_N64,4100\nSOFTMAX_R197,9800\n"
