"""Запуск: python -m validator <документ требований> <файл отчёта.md>"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:  # без зависимостей — понятная подсказка вместо трассировки
    from dotenv import load_dotenv
    import jinja2  # noqa: F401
except ImportError as _e:
    sys.stderr.write(f"Ошибка: не установлена библиотека «{_e.name}». "
                     "Выполните: python -m pip install -r requirements.txt\n")
    raise SystemExit(2) from None

from . import __version__
from .ingest import DocumentError, QuoteIndex, load
from .llm import LLM, LLMError
from .pipeline import ROOT, PROMPTS, check_requirements, check_set, consolidate, dedupe, extract, make_verdict, rule_checks, verify
from .report import render, render_error, render_error_html, render_html
from .schema import Passport, Report


def _commit() -> str:
    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True,
                                      stderr=subprocess.DEVNULL).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                                        text=True, stderr=subprocess.DEVNULL).strip()
        return sha + ("-dirty" if dirty else "")
    except Exception:  # noqa: BLE001
        return "нет git"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="validator", description="Статическая проверка документа требований с помощью LLM")
    ap.add_argument("input", help="путь к документу требований (.pdf, .md, .txt, .docx)")
    ap.add_argument("output", help="путь к файлу отчёта (.md или .html); рядом ляжет .json")
    ap.add_argument("--no-cache", action="store_true", help="не использовать кэш ответов модели")
    ap.add_argument("--html", action="store_true", help="дополнительно сохранить наглядный отчёт .html")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # кириллица в консоли Windows
        except Exception:  # noqa: BLE001
            pass

    load_dotenv(ROOT / ".env")
    load_dotenv()
    if args.no_cache:
        import os
        os.environ["LLM_CACHE"] = "0"

    out = Path(args.output)
    want_html = args.html or out.suffix.lower() == ".html"
    if out.suffix.lower() == ".html":
        out = out.with_suffix(".md")  # markdown пишется всегда (его требует формат сдачи), HTML — рядом
    started = datetime.now().strftime("%Y-%m-%d %H:%M")
    model_name = "—"

    def fail(stage: str, reason: str, code: int) -> int:
        """Ошибку видно и в консоли, и в файле отчёта — чтобы по отчёту было понятно, почему проверки нет."""
        print(f"Ошибка ({stage}): {reason}", file=sys.stderr)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(render_error(Path(args.input).name, started, model_name, f"{__version__} ({_commit()})",
                                        stage, reason), encoding="utf-8")
            out.with_suffix(".json").write_text(json.dumps(
                {"error": {"stage": stage, "reason": reason}, "document": Path(args.input).name, "date": started},
                ensure_ascii=False, indent=1), encoding="utf-8")
            if want_html:
                out.with_suffix(".html").write_text(render_error_html(
                    Path(args.input).name, started, model_name, f"{__version__} ({_commit()})", stage, reason),
                    encoding="utf-8")
            print(f"Причина записана в {out}" + (f" и {out.with_suffix('.html')}" if want_html else ""), file=sys.stderr)
        except OSError as e:
            print(f"Отчёт записать не удалось: {e}", file=sys.stderr)
        return code

    if out.suffix.lower() != ".md":
        print(f"Ошибка: файл отчёта должен быть .md или .html, получено «{out.name}»", file=sys.stderr)
        return 2
    try:
        doc = load(args.input)
    except DocumentError as e:
        return fail("чтение документа", str(e), 2)
    except Exception as e:  # noqa: BLE001 — любой неожиданный сбой чтения показываем, а не роняем трассировкой
        return fail("чтение документа", f"не удалось прочитать файл: {type(e).__name__}: {e}", 2)
    try:
        llm = LLM()
    except LLMError as e:
        return fail("подключение к модели", str(e), 2)
    model_name = llm.name

    print(f"Документ: {doc.name} · модель: {llm.name}", file=sys.stderr)
    for w in doc.warnings:
        print(f"Предупреждение: {w}", file=sys.stderr)
    idx = QuoteIndex(doc)
    criteria = (PROMPTS / "criteria.md").read_text(encoding="utf-8")

    try:
        reqs = extract(llm, doc, idx)
        if not reqs:
            return fail("нарезка на требования",
                        "модель не нашла в документе ни одного требования. Проверьте, что передан документ "
                        "требований, а не другой файл.", 4)
        per_req = check_requirements(llm, doc, reqs, criteria)
        per_set = check_set(llm, doc, reqs, criteria)
    except LLMError as e:
        return fail("обращение к модели", str(e), 2 if str(e).startswith("Провайдер отклонил") else 3)
    rules = rule_checks(doc)

    print("4/4 Проверяю цитаты и собираю отчёт…", file=sys.stderr)
    per_req, dropped_r = verify(dedupe(per_req), idx)
    per_set, dropped_s = verify(dedupe(per_set + rules), idx)
    dropped = dropped_r + dropped_s
    raw_total = len(per_req) + len(per_set)
    per_req, per_set = consolidate(per_req, per_set)

    allf = per_req + per_set
    stats = {
        "requirements": len(reqs),
        "findings": len(allf),
        "related": sum(len(f.related) for f in allf),
        "raw_findings": raw_total,
        "critical": sum(f.severity == "critical" for f in allf),
        "major": sum(f.severity == "major" for f in allf),
        "minor": sum(f.severity == "minor" for f in allf),
        "quotes_checked": len(allf) + len(dropped),
        "quotes_dropped": len(dropped),
        "dropped_examples": [f.quote[:120] for f in dropped[:5]],
        "warnings": doc.warnings + ([f"Цитаты {sum(not r.quote_verified for r in reqs)} требований не найдены "
                                     "в документе дословно, место для них не указано."]
                                    if any(not r.quote_verified for r in reqs) else []),
    }
    report = Report(
        passport=Passport(document=doc.name, date=datetime.now().strftime("%Y-%m-%d %H:%M"),
                          model=llm.name, commit=_commit(), agent_version=__version__),
        requirements=reqs, findings=per_req, set_findings=per_set,
        verdict=make_verdict(reqs, per_req, per_set), stats=stats,
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(report), encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    if want_html:
        out.with_suffix(".html").write_text(render_html(report), encoding="utf-8")
    print(f"Готово: {out} (требований {stats['requirements']}, нарушений {stats['findings']}, "
          f"критичных {stats['critical']}, отброшено неподтверждённых цитат {stats['quotes_dropped']})", file=sys.stderr)
    if want_html:
        print(f"Наглядный отчёт: {out.with_suffix('.html')} — откройте в браузере", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
