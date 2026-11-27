"""Веб-приложение агента проверки требований (интерфейс — web/index.html).

Запуск:  python app.py            (откроется http://127.0.0.1:8000)
         python app.py --port 8080 --no-browser

Страница позволяет загрузить документ (PDF, DOCX, MD, TXT), запустить проверку,
следить за ходом прогона и открыть отчёт. Все прогоны сохраняются в .cache/web/
и видны в истории. Агент запускается той же командой, что и из консоли
(python -m validator ... --html), поэтому результат совпадает с CLI.
Нужен только стандартный Python и зависимости из requirements.txt; ключ LLM берётся из .env.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / ".cache" / "web"
SAMPLE = ROOT / "input" / "requirements.pdf"
ALLOWED = {".pdf", ".docx", ".md", ".txt"}
MAX_BYTES = 25 * 1024 * 1024
FILES = {"report.md": "text/markdown; charset=utf-8", "report.json": "application/json; charset=utf-8",
         "report.html": "text/html; charset=utf-8", "log.txt": "text/plain; charset=utf-8"}
ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")

_lock = threading.Lock()          # одновременно идёт один прогон: бесплатные лимиты LLM не любят параллель
_active: dict[str, subprocess.Popen] = {}


def _env_model() -> str:
    """Какая модель настроена в .env — без чтения ключа."""
    vals: dict[str, str] = {}
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if "KEY" not in k:
                    vals[k.strip()] = v.strip()
    p = vals.get("LLM_PROVIDER", "openai")
    m = {"openai": vals.get("OPENAI_MODEL"), "gemini": vals.get("GEMINI_MODEL"),
         "ollama": vals.get("OLLAMA_MODEL")}.get(p)
    return f"{p}:{m}" if m else ("нет .env — скопируйте .env.example" if not env.exists() else p)


def _safe_name(name: str) -> str:
    name = Path(unquote(name)).name.strip() or "document"
    name = re.sub(r"[^\w.\- ()]+", "_", name, flags=re.UNICODE)
    return name[:120]


def _meta(run: Path) -> dict:
    try:
        return json.loads((run / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_meta(run: Path, **upd) -> dict:
    m = _meta(run)
    m.update(upd)
    (run / "meta.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    return m


def _summary(run: Path) -> dict:
    """Короткая сводка из report.json для истории."""
    try:
        d = json.loads((run / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if "error" in d:
        return {"error": d["error"].get("reason", "")}
    s = d.get("stats", {})
    return {"requirements": s.get("requirements"), "findings": s.get("findings"), "critical": s.get("critical"),
            "ready": d.get("verdict", {}).get("ready"), "model": d.get("passport", {}).get("model")}


def _doc_lines(run: Path) -> dict:
    """Текст документа по строкам — так же, как его прочитал агент (номера строк и страниц совпадают с отчётом)."""
    cache = run / "doc.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            pass
    name = _meta(run).get("document", "")
    src = run / name
    if not name or not src.exists():
        return {"error": "исходный файл не найден"}
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from validator.ingest import load  # тот же разбор, что у агента
        d = load(src)
        out = {"lines": d.lines, "pages": list(d.page_of_line)}
    except Exception as e:  # noqa: BLE001 — документ показываем по возможности, отчёт работает и без него
        return {"error": f"не удалось прочитать документ: {e}"}
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def _start(doc: Path, run: Path, no_cache: bool) -> None:
    cmd = [sys.executable, "-m", "validator", str(doc), str(run / "report.md"), "--html"]
    if no_cache:
        cmd.append("--no-cache")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

    def work() -> None:
        with _lock:
            _write_meta(run, status="running", started=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            with open(run / "log.txt", "w", encoding="utf-8") as log:
                try:
                    p = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, encoding="utf-8", errors="replace")
                except OSError as e:
                    log.write(f"Не удалось запустить агента: {e}\n")
                    _write_meta(run, status="error", code=-1)
                    return
                _active[run.name] = p
                for line in p.stdout:  # type: ignore[union-attr]
                    log.write(line)
                    log.flush()
                code = p.wait()
                _active.pop(run.name, None)
            _write_meta(run, status="done" if code == 0 else "error", code=code,
                        finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    _write_meta(run, status="queued")
    threading.Thread(target=work, daemon=True).start()


def _new_run(filename: str, data: bytes, no_cache: bool) -> str:
    rid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    run = RUNS / rid
    run.mkdir(parents=True)
    doc = run / _safe_name(filename)
    doc.write_bytes(data)
    _write_meta(run, id=rid, document=doc.name, created=datetime.now().strftime("%Y-%m-%d %H:%M"),
                no_cache=no_cache)
    _start(doc, run, no_cache)
    return rid


class Handler(BaseHTTPRequestHandler):
    server_version = "ReqValidator/1.0"

    def log_message(self, fmt: str, *args) -> None:  # тише в консоли
        if args and str(args[1]).startswith(("4", "5")):
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _err(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code)

    # --- GET ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        if not parts:
            if not WEB.exists():
                return self._err(500, "нет файла web/index.html")
            return self._send(200, WEB.read_bytes(), "text/html; charset=utf-8")
        if parts == ["api", "info"]:
            return self._json({"model": _env_model(), "sample": SAMPLE.exists(), "sample_name": SAMPLE.name})
        if parts == ["api", "runs"]:
            runs = []
            if RUNS.exists():
                for run in sorted((p for p in RUNS.iterdir() if p.is_dir() and ID_RE.match(p.name)), reverse=True)[:50]:
                    m = _meta(run)
                    runs.append({**m, "id": run.name, "summary": _summary(run),
                                 "has_html": (run / "report.html").exists()})
            return self._json(runs)
        if len(parts) == 3 and parts[:2] == ["api", "runs"] and ID_RE.match(parts[2]):
            run = RUNS / parts[2]
            if not run.is_dir():
                return self._err(404, "прогон не найден")
            log = (run / "log.txt").read_text(encoding="utf-8", errors="replace") if (run / "log.txt").exists() else ""
            m = _meta(run)
            return self._json({**m, "id": run.name, "log": log[-20000:], "summary": _summary(run),
                               "has_html": (run / "report.html").exists()})
        if len(parts) == 4 and parts[:2] == ["api", "runs"] and ID_RE.match(parts[2]) and parts[3] in ("report", "doc"):
            run = RUNS / parts[2]
            if not run.is_dir():
                return self._err(404, "прогон не найден")
            if parts[3] == "report":
                f = run / "report.json"
                if not f.exists():
                    return self._err(404, "отчёта ещё нет")
                return self._send(200, f.read_bytes(), "application/json; charset=utf-8")
            return self._json(_doc_lines(run))
        if len(parts) == 3 and parts[0] == "runs" and ID_RE.match(parts[1]) and parts[2] in FILES:
            f = RUNS / parts[1] / parts[2]
            if not f.exists():
                return self._err(404, "файла нет")
            q = parse_qs(u.query)
            extra = {}
            if "download" in q:
                doc = _meta(RUNS / parts[1]).get("document", "report")
                extra["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(f"{Path(doc).stem}_{parts[2]}")
            return self._send(200, f.read_bytes(), FILES[parts[2]], extra)
        return self._err(404, "нет такой страницы")

    # --- POST --------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        no_cache = q.get("no_cache", ["0"])[0] == "1"
        if u.path == "/api/run-sample":
            if not SAMPLE.exists():
                return self._err(404, "нет input/requirements.pdf")
            return self._json({"id": _new_run(SAMPLE.name, SAMPLE.read_bytes(), no_cache)})
        if u.path == "/api/run":
            name = q.get("name", [""])[0]
            if Path(name).suffix.lower() not in ALLOWED:
                return self._err(400, "поддерживаются файлы .pdf, .docx, .md, .txt")
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                size = 0
            if size <= 0:
                return self._err(400, "файл пустой")
            if size > MAX_BYTES:
                return self._err(413, "файл больше 25 МБ")
            data = self.rfile.read(size)
            return self._json({"id": _new_run(name, data, no_cache)})
        parts = [p for p in u.path.split("/") if p]
        if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "stop" and ID_RE.match(parts[2]):
            p = _active.get(parts[2])
            if p:
                p.terminate()
                return self._json({"ok": True})
            return self._err(409, "прогон не выполняется")
        if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "delete" and ID_RE.match(parts[2]):
            if parts[2] in _active:
                return self._err(409, "прогон ещё идёт — сначала остановите его")
            run = RUNS / parts[2]
            if run.is_dir():
                shutil.rmtree(run, ignore_errors=True)
            return self._json({"ok": True})
        return self._err(404, "нет такого действия")


WEB = ROOT / "web" / "index.html"



def main() -> int:
    ap = argparse.ArgumentParser(description="Веб-интерфейс агента проверки требований")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1", help="по умолчанию только этот компьютер")
    ap.add_argument("--no-browser", action="store_true", help="не открывать браузер автоматически")
    args = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    RUNS.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '') else args.host}:{args.port}"
    print(f"Агент проверки требований: {url}  (модель: {_env_model()}). Остановить — Ctrl+C")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
