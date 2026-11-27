"""Обращение к LLM: Gemini (по умолчанию) или локальная Ollama. Кэш ответов и повторы при лимитах."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CACHE_DIR = Path(os.getenv("LLM_CACHE_DIR", ".cache"))


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self):
        self.provider = os.getenv("LLM_PROVIDER", "gemini").lower()
        self.use_cache = os.getenv("LLM_CACHE", "1") != "0"
        if self.provider == "gemini":
            key = os.getenv("GEMINI_API_KEY", "")
            if not key or key.startswith("your_"):
                raise LLMError("Не задан GEMINI_API_KEY. Скопируйте .env.example в .env и вставьте ключ из https://aistudio.google.com/")
            from google import genai

            self.model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
            self._client = genai.Client(api_key=key)
        elif self.provider == "openai":  # любой OpenAI-совместимый API: Groq, OpenRouter, OpenAI…
            self._key = os.getenv("OPENAI_API_KEY", "")
            if not self._key or self._key.startswith("your_"):
                raise LLMError("Не задан OPENAI_API_KEY: в .env осталась заглушка из .env.example. "
                               "Получите ключ на https://console.groq.com/keys и вставьте его в .env")
            self._base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
            self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        elif self.provider == "ollama":
            self.model = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")
            self._base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        else:
            raise LLMError(f"Неизвестный LLM_PROVIDER: {self.provider}")

    @property
    def name(self) -> str:
        return f"{self.provider}:{self.model}"

    # --- публичный метод -------------------------------------------------
    def json(self, prompt: str, label: str = "", reasoning: str | None = None) -> object:
        """Отправить промпт, получить разобранный JSON. Повторяет при сбоях и лимитах.
        reasoning — глубина рассуждения (low/medium/high) для моделей, которые это поддерживают."""
        reasoning = reasoning or os.getenv("LLM_REASONING", "low")
        key = hashlib.sha256(f"{self.name}\n{reasoning}\n{prompt}".encode()).hexdigest()[:24]
        cache_file = CACHE_DIR / f"{key}.json"
        if self.use_cache and cache_file.exists():
            return json.loads(cache_file.read_text(encoding="utf-8"))

        last_err: Exception | None = None
        for attempt in range(10):
            try:
                raw = self._call(prompt, reasoning)
                data = _parse_json(raw)
                if self.use_cache:
                    CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                return data
            except Exception as e:  # noqa: BLE001 — сеть, лимиты, битый JSON
                last_err = e
                if re.match(r"^(401|403|404)\b", str(e)) or any(c in str(e) for c in ("PERMISSION_DENIED", "API_KEY_INVALID", "model_not_found", "invalid_api_key")):
                    raise LLMError(f"Провайдер отклонил запрос (ключ или доступ): {str(e)[:300]}") from e
                wait = _retry_delay(e, attempt)
                if any(c in str(e) for c in ("per day", "insufficient_quota", "exceeded your current quota")) or wait > 180:
                    raise LLMError(f"Исчерпан лимит провайдера (суточный или по оплате), повторы не помогут — "
                                   f"смените ключ/модель или дождитесь сброса: {str(e)[:300]}") from e
                print(f"  [{label}] попытка {attempt + 1} не удалась: {str(e)[:160]} — жду {wait:.0f} с", file=sys.stderr)
                time.sleep(wait)
        raise LLMError(f"Модель не ответила после повторов ({label}): {last_err}")

    # --- провайдеры ------------------------------------------------------
    def _call(self, prompt: str, reasoning: str = "low") -> str:
        if self.provider == "gemini":
            from google.genai import types

            resp = self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                ),
            )
            return resp.text or ""
        if self.provider == "openai":
            body = json.dumps({
                "model": self.model, "temperature": 0.1,
                "messages": [{"role": "system", "content": "Отвечай строго одним валидным JSON-объектом без markdown."},
                             {"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "max_completion_tokens": int(os.getenv("LLM_MAX_OUTPUT", "3500")),
                **({"reasoning_effort": reasoning} if "gpt-oss" in self.model else {}),
            }).encode()
            req = urllib.request.Request(f"{self._base}/chat/completions", data=body, headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {self._key}",
                "User-Agent": "req-validator/0.1"})
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    return json.loads(r.read())["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                raise RuntimeError(f"{e.code} {e.read()[:300].decode(errors='replace')}") from e
        body = json.dumps({
            "model": self.model, "stream": False, "format": "json",
            "options": {"temperature": 0.1, "num_ctx": 16384},
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(f"{self._base}/api/chat", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read())["message"]["content"]


def _parse_json(raw: str) -> object:
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    if not s:
        raise ValueError("пустой ответ модели")
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # несколько JSON-значений подряд (NDJSON и т.п.) — собрать в один список
    dec, pos, items = json.JSONDecoder(), 0, []
    while pos < len(s):
        while pos < len(s) and s[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(s):
            break
        start = s.find("{", pos) if s[pos] not in "[{" else pos
        if start < 0:
            break
        obj, pos = dec.raw_decode(s, start)
        items.extend(obj if isinstance(obj, list) else obj.get("items", [obj]) if isinstance(obj, dict) else [obj])
    if not items:
        raise ValueError("в ответе модели нет JSON")
    return items


def _retry_delay(e: Exception, attempt: int) -> float:
    msg = str(e)
    m = re.search(r"try again in (?:(\d+)m)?(\d+(?:\.\d+)?)s", msg, re.I)  # Groq/OpenAI
    if m:
        return int(m.group(1) or 0) * 60 + float(m.group(2)) + 1
    m = re.search(r"retry(?:Delay)?[^0-9]{0,10}(\d+(?:\.\d+)?)s", msg, re.I)  # Gemini
    if m:
        return float(m.group(1)) + 2
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        return min(20 * (attempt + 1), 90)
    return min(3 * (2 ** attempt), 60)
