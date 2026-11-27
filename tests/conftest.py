"""Общие фикстуры. Тесты не ходят в сеть и не требуют ключа LLM."""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SAMPLE = ROOT / "input" / "requirements.md"


@pytest.fixture(autouse=True)
def offline_env(monkeypatch, tmp_path):
    """Провайдер без ключа, без кэша и без чтения настоящего .env."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_CACHE", "0")
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / ".cache"))
    monkeypatch.chdir(tmp_path)


class FakeLLM:
    """Подставная модель: отвечает по метке шага, как настоящая, но детерминированно."""

    name = "fake:model"

    def __init__(self, quotes, review=None):
        self.quotes = quotes
        self.review = review  # функция (n, finding_json) -> dict для шага ревизии
        self.calls = []

    def json(self, prompt, label="", reasoning=None):
        self.calls.append(label)
        if label.startswith("extract"):
            return {"items": [{"doc_id": None, "section": "s", "quote": q, "type": "functional"} for q in self.quotes]}
        if label.startswith("review"):
            rows = re.findall(r'^\{"n": (\d+).*$', prompt, re.M)
            return {"items": [self.review(int(n)) if self.review else {"n": int(n), "verdict": "keep"} for n in rows]}
        ids = re.findall(r'"id": ?"(R\d+)"', prompt)
        is_set = label.startswith("set")
        return {"items": [{"req_id": i, "criterion": "consistent" if is_set else "unambiguous",
                           "severity": "critical" if is_set else "major",
                           "quote": self.quotes[int(i[1:]) - 1][:60], "explanation": "e", "fix": "f"}
                          for i in ids[:6]]}


@pytest.fixture
def sample_quotes():
    text = SAMPLE.read_text(encoding="utf-8")
    return [l.strip("#-* |") for l in text.splitlines() if len(l.strip("#-* |")) > 40][:20]
