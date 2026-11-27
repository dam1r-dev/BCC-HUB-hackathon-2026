"""Отчёт в markdown по шаблону templates/report.md.j2."""
from __future__ import annotations

from jinja2 import Environment, FileSystemLoader

from .pipeline import ROOT
from .schema import CRITERION_RU, SEVERITY_RU, Report


def _short(s: str, n: int = 110) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _cell(s: str) -> str:
    return " ".join(str(s).split()).replace("|", "\\|")


def render(report: Report) -> str:
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), trim_blocks=True, lstrip_blocks=True,
                      keep_trailing_newline=True)
    env.filters["short"] = _short
    env.filters["cell"] = _cell
    env.filters["sev"] = lambda s: SEVERITY_RU.get(s, s)
    env.filters["crit"] = lambda s: CRITERION_RU.get(s, s)
    d = report.to_dict()
    by_req: dict[str, list] = {}
    for f in d["findings"]:
        for rid in f["req_id"].split(","):
            by_req.setdefault(rid.strip(), []).append(f)
    critical = [f for f in d["findings"] + d["set_findings"] if f["severity"] == "critical"]
    return env.get_template("report.md.j2").render(**d, by_req=by_req, critical=critical)
