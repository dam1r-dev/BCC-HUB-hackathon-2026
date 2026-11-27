"""Отчёт в markdown (templates/report.md.j2) и, по желанию, в HTML (templates/report.html.j2)."""
from __future__ import annotations

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .pipeline import ROOT
from .schema import CRITERION_RU, SEVERITY_RU, Report


def _short(s: str, n: int = 110) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _cell(s: str) -> str:
    return " ".join(str(s).split()).replace("|", "\\|")


def render_error(document: str, date: str, model: str, version: str, stage: str, reason: str) -> str:
    """Короткий отчёт, когда проверку выполнить нельзя: читатель видит причину, а не пустой файл."""
    return (f"# Отчёт о проверке требований\n\n"
            f"## Проверка не выполнена\n\n"
            f"| Поле | Значение |\n| :--- | :--- |\n"
            f"| Документ | {_cell(document)} |\n| Дата прогона | {date} |\n| Модель | {_cell(model)} |\n"
            f"| Версия агента | {version} |\n| Этап | {stage} |\n\n"
            f"**Причина:** {reason}\n\n"
            f"Вывод о готовности документа к разработке не сделан: исправьте причину и запустите прогон снова.\n")


def _env(html: bool) -> Environment:
    # в HTML всё экранируется: текст документа и ответы модели — данные, а не разметка
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), trim_blocks=True, lstrip_blocks=True,
                      keep_trailing_newline=True, autoescape=select_autoescape(["html", "j2"]) if html else False)
    env.filters["short"] = _short
    env.filters["cell"] = _cell
    env.filters["sev"] = lambda s: SEVERITY_RU.get(s, s)
    env.filters["crit"] = lambda s: CRITERION_RU.get(s, s)
    return env


def _context(report: Report) -> dict:
    d = report.to_dict()
    by_req: dict[str, list] = {}
    for f in d["findings"]:
        for rid in f["req_id"].split(","):
            by_req.setdefault(rid.strip(), []).append(f)
    critical = [f for f in d["findings"] + d["set_findings"] if f["severity"] == "critical"]
    return {**d, "by_req": by_req, "critical": critical, "severity_ru": SEVERITY_RU, "criterion_ru": CRITERION_RU}


def render(report: Report) -> str:
    return _env(False).get_template("report.md.j2").render(**_context(report))


def render_html(report: Report) -> str:
    """Самодостаточная HTML-страница: стили и скрипт встроены, открывается двойным кликом без интернета."""
    return _env(True).get_template("report.html.j2").render(**_context(report))


def render_error_html(document: str, date: str, model: str, version: str, stage: str, reason: str) -> str:
    return _env(True).get_template("report.html.j2").render(
        error={"document": document, "date": date, "model": model, "version": version, "stage": stage,
               "reason": reason})
