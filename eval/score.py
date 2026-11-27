"""Сколько эталонных дефектов нашёл агент.

Запуск: python eval/score.py reports/report.json [eval/golden_defects.json]
Эталонный дефект считается найденным, если цитата хотя бы одного замечания агента
пересекается с его цитатой (одна содержит другую или совпадает ≥ 60% слов).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from validator.ingest import norm  # noqa: E402


def words(s: str) -> set[str]:
    return {w for w in norm(s).replace(",", " ").replace(".", " ").split() if len(w) > 2}


def gmatch(g: dict, q: str) -> bool:
    """Эталонный дефект совпал с цитатой агента: по основной или альтернативной цитате (вторая сторона противоречия)."""
    return any(match(x, q) for x in [g["quote"], *g.get("alt_quotes", [])])


def match(a: str, b: str) -> bool:
    na, nb = norm(a), norm(b)
    if na and nb and (na in nb or nb in na):
        return True
    wa, wb = words(a), words(b)
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.6


KIND = {"consistent": "contra", "correct": "contra", "comprehensible": "contra",
        "unambiguous": "vague", "verifiable": "vague",
        "complete": "gap", "conforming": "gap", "traceable": "gap",
        "appropriate": "design", "feasible": "design", "necessary": "design", "singular": "design"}


def same_kind(a: str, b: str) -> bool:
    return a == b or KIND.get(a, a) == KIND.get(b, b)


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    if len(sys.argv) < 2 or not Path(sys.argv[1]).is_file():
        print("Использование: python eval/score.py reports/report.json — файл отчёта не найден", file=sys.stderr)
        return 2
    report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if "error" in report:
        err = report["error"]
        print(f"Прогон не выполнен ({err.get('stage')}): {err.get('reason')}\nОценивать нечего.", file=sys.stderr)
        return 2
    golden_path = Path(sys.argv[2] if len(sys.argv) > 2 else Path(__file__).parent / "golden_defects.json")
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    main = report["findings"] + report["set_findings"]
    found = main + [x for f in main for x in f.get("related", [])]  # вложенные замечания тоже есть в отчёте
    hit, miss = [], []
    for g in golden:
        (hit if any(gmatch(g, f["quote"]) for f in found) else miss).append(g)
    crit = [g for g in golden if g.get("severity") == "critical"]
    crit_hit = [g for g in hit if g.get("severity") == "critical"]
    strict = [g for g in golden if any(gmatch(g, f["quote"]) and any(same_kind(c, f["criterion"]) for c in [g["criterion"], *g.get("alt_criteria", [])])
                                       for f in found)]
    print(f"Найдено эталонных дефектов: {len(hit)} из {len(golden)} ({len(hit) / max(1, len(golden)):.0%})")
    print(f"Строго (место + тип дефекта): {len(strict)} из {len(golden)} ({len(strict) / max(1, len(golden)):.0%})")
    print("Строго, критичные: " + ", ".join(f"{g['id']}{'+' if g in strict else '-'}" for g in crit))
    print(f"Из них критичных: {len(crit_hit)} из {len(crit)}")
    print(f"Замечаний у агента: главных {len(main)}, вместе с вложенными {len(found)}")
    # калибровка: совпадает ли критичность агента с эталонной у найденных дефектов
    order = {"critical": 0, "major": 1, "minor": 2}
    same = near = 0
    for g in hit:
        sevs = [f.get("severity") for f in found if gmatch(g, f["quote"])]
        d = min(abs(order.get(s_, 1) - order.get(g.get("severity"), 1)) for s_ in sevs)
        same += d == 0
        near += d <= 1
    if hit:
        print(f"Критичность совпала с эталоном: {same} из {len(hit)} ({same / len(hit):.0%}), "
              f"с разницей не больше уровня: {near} из {len(hit)}")
    dist = {k: sum(f.get("severity") == k for f in main) for k in order}
    print(f"Распределение у агента: критичных {dist['critical']}, существенных {dist['major']}, незначительных {dist['minor']}"
          f" (эталон: {sum(g.get('severity') == 'critical' for g in golden)}/"
          f"{sum(g.get('severity') == 'major' for g in golden)}/{sum(g.get('severity') == 'minor' for g in golden)})")
    if miss:
        print("\nПропущены:")
        for g in miss:
            print(f"  {g['id']} [{g.get('severity')}] {g.get('section')}: «{g['quote'][:90]}»")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
