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


def area_hier(top: float) -> str:
    """A Synopsys `report_area -hierarchy` of `top` um2: i_top with 99 % of it, blk_a with 60 % and blk_b with 30 %."""
    rows = [("top_wrap", 1.0), ("i_top", 0.99), ("i_top/blk_a", 0.6), ("i_top/blk_b", 0.3)]
    body = "".join(f"{name:<34}{top * share:>12.3f}{share * 100:>9.1f}{0:>13.3f}{0:>13.3f}{0:>14.3f}  {name.split('/')[-1]}\n"
                   for name, share in rows)
    return f"Report : area\nTotal cell area:{top:>32.3f}\n\nHierarchical cell{'Absolute':>29}{'Percent':>9}\n{RULE}\n{body}{RULE}\n"


# The hold scenario first, and its reg2reg block has no setup slack; the setup slacks are on lines 11, 18 and 25.
QOR = report_qor(("func_fast", "reg2reg", {"Worst Hold Violation": "-0.002", "No. of Hold Violations": 3}),
                 ("func_slow", "in2reg_default", {"Critical Path Slack": "0.004", "No. of Violating Paths": 0}),
                 ("func_slow", "reg2out", {"Critical Path Slack": "-0.087", "No. of Violating Paths": 41}),
                 ("func_slow", "reg2reg", {"Critical Path Slack": "-0.031", "No. of Violating Paths": 212}))
