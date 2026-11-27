"""Конвейер: нарезка → проверка каждого требования → проверка набора → правила → проверка цитат → итог."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .ingest import Document, QuoteIndex
from .llm import LLM
from .schema import CRITERIA, SEVERITIES, Finding, Requirement, Verdict

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"
BATCH = 6


def _prompt(name: str, **vars: str) -> str:
    text = (PROMPTS / name).read_text(encoding="utf-8")
    for k, v in vars.items():
        text = text.replace("{" + k + "}", v)
    return text


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _as_list(data: object, *keys: str) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                return data[k]
    return []


# --- 1. нарезка ------------------------------------------------------------
CHUNK_CHARS = 3500

FOCUS_LOGIC = """ФОКУС ЭТОЙ ПРОВЕРКИ — логика продукта:
- Пересчитай КАЖДЫЙ критерий приёмки по правилам функциональных требований (даты, месяцы, суммы, границы, включено ли событие в расчёт). Если ожидаемый результат не следует из правил — это противоречие (critical).
- Попарно сравни функциональные требования: ограничения («максимум один…», «по умолчанию…») против правил, которые требуют обратного («независимо от…», «всегда…»).
- Сравни цели и нефункциональные требования (сроки, «в момент…» против «в течение…»).
- Единицы и форматы данных в разных разделах (тенге/тиыны и т.п.), названия систем в архитектуре и диаграммах.
- Какие требования не покрыты критериями приёмки; какие граничные случаи не описаны."""

FOCUS_API = """ФОКУС ЭТОЙ ПРОВЕРКИ — спецификация API против основного документа:
- HTTP-метод против операции (изменение данных через GET, параметры в query-строке), заявленный Content-Type против отсутствия тела.
- Описание метода против его назначения (сохранение/чтение), есть ли все методы, которые нужны основному документу (например, чтение настроек).
- Коды ответа: успех со статусом ERROR, описанные, но не используемые коды, используемые, но не перечисленные коды, 200/201.
- Типы и форматы параметров запроса против ответа (integer/string), обязательность параметров при разных значениях флагов.
- Проверки из функциональных требований (валюта, статус карты, значение по умолчанию), которых нет в логике метода."""


def chunks(doc: Document, limit: int = CHUNK_CHARS) -> list[str]:
    """Режет документ по заголовкам на куски не длиннее limit (чтобы влезть в лимиты бесплатных моделей).
    В начало каждого куска добавляется путь заголовков, чтобы модель знала раздел."""
    blocks: list[tuple[str, list[str]]] = []
    path: list[str] = []
    cur: list[str] = []
    has_md = any(re.match(r"^\s*#{1,6}\s", l) for l in doc.lines)
    for line in doc.lines:
        is_head = (re.match(r"^\s*#{1,6}\s", line) if has_md else
                   (re.match(r"^\s*(Часть|Раздел|\d+(\.\d+)*\.?)\s+\S", line) and len(line) < 90))
        if is_head:
            if cur:
                blocks.append((" / ".join(path), cur))
            level = len(line) - len(line.lstrip("#")) if line.startswith("#") else 2
            path = path[: max(0, level - 1)] + [line.strip("# ").strip()]
            cur = [line]
        elif re.match(r"^\s*\|\s*:?-{3,}", line):
            continue  # разделитель таблицы markdown — только шум
        else:
            cur.append(line)
    if cur:
        blocks.append((" / ".join(path), cur))
    # общий заголовок документа (единственный H1) не повторяем в пути каждого раздела
    if has_md and sum(1 for l in doc.lines if re.match(r"^\s*#\s", l)) == 1:
        blocks = [(" / ".join(b.split(" / ")[1:]) or b, ls) for b, ls in blocks]
    out, buf = [], ""
    for p, lines in blocks:
        text = "\n".join(l for l in lines if l.strip())
        if not text:
            continue
        piece = f"[Раздел: {p}]\n{text}\n"
        if buf and len(buf) + len(piece) > limit:
            out.append(buf)
            buf = ""
        while len(piece) > limit:  # очень длинный раздел — режем по строкам
            cut = piece.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            out.append(piece[:cut])
            piece = f"[Раздел: {p}, продолжение]\n" + piece[cut:]
        buf += piece
    if buf:
        out.append(buf)
    return out


def extract(llm: LLM, doc: Document, idx: QuoteIndex) -> list[Requirement]:
    parts = chunks(doc)
    raw_items: list[dict] = []
    for n, part in enumerate(parts, start=1):
        _log(f"1/4 Нарезаю документ на требования: часть {n}/{len(parts)}")
        data = llm.json(_prompt("extract.md", document=part), f"extract{n}")
        raw_items += [r for r in _as_list(data, "items", "requirements") if isinstance(r, dict)]
    reqs: list[Requirement] = []
    seen: set[str] = set()
    for r in raw_items:
        if not r.get("quote"):
            continue
        key = re.sub(r"\W+", "", str(r["quote"]).lower())[:120]
        if key in seen:
            continue
        seen.add(key)
        req = Requirement(
            id=f"R{len(reqs) + 1:02d}",
            doc_id=(r.get("doc_id") or None),
            section=str(r.get("section", "")),
            quote=str(r["quote"]).strip(),
            type=str(r.get("type", "other")),
        )
        req.quote_verified, req.location = idx.find(req.quote)
        reqs.append(req)
    _log(f"   выделено требований: {len(reqs)}, цитаты подтверждены у {sum(r.quote_verified for r in reqs)}")
    return reqs


def _finding(raw: dict, scope: str, default_req: str = "SET") -> Finding | None:
    if not isinstance(raw, dict) or not raw.get("quote"):
        return None
    crit = str(raw.get("criterion", "")).strip().lower()
    sev = str(raw.get("severity", "major")).strip().lower()
    req_id = raw.get("req_id", default_req)
    if isinstance(req_id, list):
        req_id = ",".join(map(str, req_id))
    return Finding(
        req_id=str(req_id or default_req),
        criterion=crit if crit in CRITERIA else (crit or "correct"),
        severity=sev if sev in SEVERITIES else "major",
        quote=str(raw["quote"]).strip(),
        explanation=str(raw.get("explanation", "")).strip(),
        fix=str(raw.get("fix", "")).strip(),
        scope=scope,
    )


# --- 2. проверка каждого требования ----------------------------------------
def check_requirements(llm: LLM, doc: Document, reqs: list[Requirement], criteria: str) -> list[Finding]:
    out: list[Finding] = []
    batches = [reqs[i:i + BATCH] for i in range(0, len(reqs), BATCH)]
    # ключевые правила продукта — контекст для каждой пачки, чтобы не делать ложных выводов
    context = [r for r in reqs if r.type == "functional"][:8]
    for n, batch in enumerate(batches, start=1):
        _log(f"2/4 Проверяю требования: пачка {n}/{len(batches)}")
        payload = json.dumps([{"id": r.id, "doc_id": r.doc_id, "section": r.section, "quote": r.quote} for r in batch],
                             ensure_ascii=False, indent=1)
        ctx = "\n".join(f"{r.id} ({r.doc_id or r.section}): {r.quote}" for r in context if r not in batch) or "—"
        data = llm.json(_prompt("check_batch.md", criteria=criteria, requirements_json=payload, context=ctx),
                        f"batch{n}")
        for raw in _as_list(data, "findings", "items"):
            f = _finding(raw, "requirement", batch[0].id)
            if f:
                out.append(f)
    return out


# --- 3. проверка набора ----------------------------------------------------
def check_set(llm: LLM, doc: Document, reqs: list[Requirement], criteria: str) -> list[Finding]:
    _log("3/4 Проверяю набор требований целиком…")
    # компактно: без отступов и без полного текста документа — укладываемся в лимит токенов
    def line(r: Requirement) -> str:
        return json.dumps({"id": r.id, "doc_id": r.doc_id, "section": r.section, "type": r.type, "quote": r.quote},
                          ensure_ascii=False)

    api = [r for r in reqs if r.type == "api"]
    core = [r for r in reqs if r.type != "api"]
    focuses = [
        ("логика", core, FOCUS_LOGIC),
        ("api", api + [r for r in core if r.type in ("functional", "integration", "data")], FOCUS_API),
    ]
    out: list[Finding] = []
    for name, subset, focus in focuses:
        if not subset:
            continue
        _log(f"3/4 Проверяю набор целиком: {name}")
        payload = "\n".join(line(r) for r in subset)
        data = llm.json(_prompt("check_set.md", criteria=criteria, requirements_json=payload, focus=focus),
                        f"set-{name}", reasoning=os.getenv("LLM_REASONING_SET", "medium"))
        out += [f for f in (_finding(raw, "set") for raw in _as_list(data, "findings", "items")) if f]
    valid = {r.id for r in reqs}
    for f in out:  # если модель не дала корректные R-идентификаторы — привязать по цитате
        ids = [x.strip() for x in f.req_id.split(",") if x.strip() in valid]
        if not ids:
            nq = re.sub(r"\W+", "", f.quote.lower())
            ids = [r.id for r in reqs if nq and nq[:40] in re.sub(r"\W+", "", r.quote.lower())]
        f.req_id = ",".join(ids) if ids else "SET"
    return out


# --- детерминированные правила (без LLM) -----------------------------------
VAGUE = ["и т.п.", "и т.д.", "и др.", "при необходимости", "по возможности", "быстро", "удобно",
         "оптимально", "своевременно", "достаточно", "как правило", "и тому подобное"]
PLACEHOLDERS = [
    (r"заполняются тестировщиками", "Раздел не заполнен: требования к тестированию отсутствуют."),
    (r"Да / Нет", "Чек-лист ревью не заполнен: отметки не проставлены."),
    (r"в источнике не заполнен", "Версия и дата документа не указаны — нарушена управляемость изменений."),
]


def rule_checks(doc: Document) -> list[Finding]:
    out: list[Finding] = []
    seen: set[str] = set()
    for i, line in enumerate(doc.lines):
        low = line.lower()
        for w in VAGUE:
            if re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", low) and (w, i) not in seen:
                seen.add((w, i))
                out.append(Finding("SET", "unambiguous", "minor", line.strip()[:200],
                                   f"Размытая формулировка «{w}» без измеримого критерия.",
                                   "Заменить на конкретное измеримое условие.", scope="rule"))
        for pat, expl in PLACEHOLDERS:
            if re.search(pat, line) and pat not in seen:
                seen.add(pat)
                out.append(Finding("SET", "complete", "major", line.strip()[:200], expl,
                                   "Заполнить раздел до начала разработки.", scope="rule"))
    return out


# --- 4. проверка цитат и итог ---------------------------------------------
def verify(findings: list[Finding], idx: QuoteIndex) -> tuple[list[Finding], list[Finding]]:
    kept, dropped = [], []
    for f in findings:
        f.quote_verified, f.location = idx.find(f.quote)
        (kept if f.quote_verified else dropped).append(f)
    return kept, dropped


def dedupe(findings: list[Finding]) -> list[Finding]:
    seen, out = set(), []
    for f in findings:
        key = (f.criterion, re.sub(r"\W+", "", f.quote.lower())[:80])
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


ORDER = {s: i for i, s in enumerate(SEVERITIES)}


def make_verdict(reqs: list[Requirement], findings: list[Finding], set_findings: list[Finding]) -> Verdict:
    # сначала критичные дефекты набора (противоречия), затем критичные по требованиям, затем остальные
    allf = sorted(set_findings + findings, key=lambda f: (ORDER.get(f.severity, 9), f.scope != "set"))
    crit = [f for f in allf if f.severity == "critical"]
    major = [f for f in allf if f.severity == "major"]
    by_id = {r.id: r for r in reqs}

    def label(f: Finding) -> str:
        first = f.req_id.split(",")[0].strip()
        r = by_id.get(first)
        ref = f"{f.req_id}" + (f" ({r.doc_id})" if r and r.doc_id else "")
        return f"[{ref}, {f.location}] {f.explanation} → {f.fix}".strip()

    top = [label(f) for f in (crit + major)[:7]]
    if crit:
        summary = (f"Начинать разработку и тестирование по документу нельзя. Критичных дефектов: {len(crit)}, "
                   f"существенных: {len(major)}. Сначала устранить критичные дефекты из списка ниже.")
    elif major:
        summary = (f"Критичных дефектов нет, но до начала разработки нужно исправить существенные дефекты "
                   f"(найдено: {len(major)}).")
    else:
        summary = "Документ пригоден для начала разработки и тестирования; найдены только незначительные замечания."
    return Verdict(ready=not crit, summary=summary, top_fixes=top)
