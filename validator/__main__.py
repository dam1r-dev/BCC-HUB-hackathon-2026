"""Запуск: python -m validator <документ требований> <файл отчёта.md>"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from . import __version__
from .ingest import QuoteIndex, load
from .llm import LLM, LLMError
from .pipeline import ROOT, PROMPTS, check_requirements, check_set, dedupe, extract, make_verdict, rule_checks, verify
from .report import render
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
    ap.add_argument("output", help="путь к файлу отчёта (.md); рядом ляжет .json")
    ap.add_argument("--no-cache", action="store_true", help="не использовать кэш ответов модели")
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

    try:
        doc = load(args.input)
        llm = LLM()
    except (FileNotFoundError, ValueError, RuntimeError, LLMError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2

    print(f"Документ: {doc.name} · модель: {llm.name}", file=sys.stderr)
    idx = QuoteIndex(doc)
    criteria = (PROMPTS / "criteria.md").read_text(encoding="utf-8")

    try:
        reqs = extract(llm, doc, idx)
        per_req = check_requirements(llm, doc, reqs, criteria)
        per_set = check_set(llm, doc, reqs, criteria)
    except LLMError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 3
    rules = rule_checks(doc)

    print("4/4 Проверяю цитаты и собираю отчёт…", file=sys.stderr)
    per_req, dropped_r = verify(dedupe(per_req), idx)
    per_set, dropped_s = verify(dedupe(per_set + rules), idx)
    dropped = dropped_r + dropped_s

    allf = per_req + per_set
    stats = {
        "requirements": len(reqs),
        "findings": len(allf),
        "critical": sum(f.severity == "critical" for f in allf),
        "major": sum(f.severity == "major" for f in allf),
        "minor": sum(f.severity == "minor" for f in allf),
        "quotes_checked": len(allf) + len(dropped),
        "quotes_dropped": len(dropped),
        "dropped_examples": [f.quote[:120] for f in dropped[:5]],
    }
    report = Report(
        passport=Passport(document=doc.name, date=datetime.now().strftime("%Y-%m-%d %H:%M"),
                          model=llm.name, commit=_commit(), agent_version=__version__),
        requirements=reqs, findings=per_req, set_findings=per_set,
        verdict=make_verdict(reqs, per_req, per_set), stats=stats,
    )

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(report), encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Готово: {out} (требований {stats['requirements']}, нарушений {stats['findings']}, "
          f"критичных {stats['critical']}, отброшено неподтверждённых цитат {stats['quotes_dropped']})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
