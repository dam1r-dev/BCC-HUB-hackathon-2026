"""Полный прогон одной командой на подставной модели: отчёт из 6 разделов, JSON, HTML, коды выхода."""
import json

import validator.llm as L
from validator.__main__ import main

from conftest import SAMPLE, FakeLLM

SECTIONS = ["## 1. Паспорт прогона", "## 2. Как агент нарезал требования", "## 3. Проверка каждого требования",
            "## 4. Проверка всего набора требований", "## 5. Сводка", "## 6. Итог"]


def test_full_run(monkeypatch, tmp_path, sample_quotes):
    fake = FakeLLM(sample_quotes, review=lambda n: {"n": n, "verdict": "drop" if n == 2 else "keep"})
    monkeypatch.setattr(L.LLM, "json", lambda self, *a, **k: fake.json(*a, **k))
    out = tmp_path / "out" / "report.md"
    assert main([str(SAMPLE), str(out), "--html"]) == 0

    md = out.read_text(encoding="utf-8")
    pos = [md.index(s) for s in SECTIONS]
    assert pos == sorted(pos), "разделы отчёта идут по порядку"
    assert "Документ НЕ готов" in md  # подставная модель выдаёт критичные противоречия
    assert "Ревизия замечаний" in md

    rep = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    s = rep["stats"]
    assert s["requirements"] == len(sample_quotes)
    assert s["review_dropped"] == 1
    assert s["findings"] == s["critical"] + s["major"] + s["minor"] > 0
    assert all(f["quote_verified"] and f["location"] for f in rep["findings"] + rep["set_findings"])
    assert out.with_suffix(".html").exists()
    assert any(c.startswith("review") for c in fake.calls)


def test_bad_input_writes_error_report(tmp_path):
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"%PDF-1.4 garbage")
    out = tmp_path / "r.md"
    assert main([str(bad), str(out)]) == 2
    assert "повреждён" in out.read_text(encoding="utf-8")
    assert json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))["error"]
