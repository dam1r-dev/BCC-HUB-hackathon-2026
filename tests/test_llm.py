"""Разбор ответа модели и поведение при ошибках провайдера (без сети)."""
import pytest

import validator.llm as L


def test_parse_json_variants():
    assert L._parse_json('```json\n{"items": [1]}\n```') == {"items": [1]}
    assert L._parse_json('{"a": 1}\n{"b": 2}') == [{"a": 1}, {"b": 2}]  # NDJSON собирается в список
    with pytest.raises(ValueError):
        L._parse_json("   ")


def test_retry_delay_uses_provider_hint_and_is_capped():
    assert L._retry_delay(RuntimeError("429 Please try again in 1m2.5s"), 0) == pytest.approx(63.5)
    assert L._retry_delay(RuntimeError("429 slow down"), 9) <= 90
    assert L._retry_delay(RuntimeError("connection reset"), 9) <= 60


def _llm_failing_with(monkeypatch, message):
    llm = L.LLM()
    monkeypatch.setattr(llm, "_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(message)))
    monkeypatch.setattr(L.time, "sleep", lambda s: pytest.fail("не должен ждать"))
    return llm


def test_daily_limit_fails_fast(monkeypatch):
    llm = _llm_failing_with(monkeypatch, '429 {"error":{"message":"Rate limit reached on tokens per day (TPD)"}}')
    with pytest.raises(L.LLMError, match="Исчерпан лимит"):
        llm.json("x", label="t")


def test_bad_key_fails_fast(monkeypatch):
    llm = _llm_failing_with(monkeypatch, '401 {"error":{"code":"invalid_api_key"}}')
    with pytest.raises(L.LLMError, match="Провайдер отклонил"):
        llm.json("x", label="t")


def test_placeholder_key_is_rejected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "your_groq_key_here")
    with pytest.raises(L.LLMError, match="заглушка"):
        L.LLM()


def test_deterministic_defaults():
    assert L.TEMPERATURE == 0 and isinstance(L.SEED, int)
