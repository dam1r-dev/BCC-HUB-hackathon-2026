"""Чтение документа требований и поиск цитат в нём."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

FORMATS = ".pdf, .docx, .md, .txt"
MIN_CHARS = 50          # меньше — проверять нечего
LARGE_CHARS = 200_000   # больше — прогон будет долгим и дорогим


class DocumentError(ValueError):
    """Документ нельзя проверить: нет файла, битый, пустой, не тот формат. Текст — для пользователя."""


@dataclass
class Document:
    name: str
    text: str                  # полный текст, как его видит модель
    lines: list[str]
    page_of_line: list[int]    # номер страницы для каждой строки (PDF), иначе 0
    warnings: list[str] = field(default_factory=list)   # проблемы чтения, которые не мешают проверке


def load(path: str | Path) -> Document:
    """Читает документ. Если проверять нечего или файл повреждён — DocumentError с понятной причиной."""
    p = Path(path)
    if not p.exists():
        raise DocumentError(f"Файл не найден: {p}")
    if not p.is_file():
        raise DocumentError(f"Это не файл, а папка: {p}. Укажите путь к документу ({FORMATS}).")
    if p.stat().st_size == 0:
        raise DocumentError(f"Файл пустой (0 байт): {p.name}")
    ext = p.suffix.lower()
    if ext == ".pdf":
        doc = _load_pdf(p)
    elif ext == ".docx":
        doc = _load_docx(p)
    elif ext in (".md", ".txt", ".markdown"):
        doc = _load_text(p)
    elif ext == ".doc":
        raise DocumentError("Старый формат .doc не поддерживается. Сохраните документ как .docx или .pdf.")
    else:
        raise DocumentError(f"Неподдерживаемый формат «{ext or 'без расширения'}». Поддерживаются {FORMATS}.")

    chars = len(doc.text.strip())
    if chars < MIN_CHARS:
        raise DocumentError(f"В документе {p.name} почти нет текста ({chars} симв.): проверять нечего.")
    if chars > LARGE_CHARS:
        doc.warnings.append(f"Документ большой ({chars:,} симв.): прогон займёт заметно больше времени и лимита модели."
                            .replace(",", " "))
    return doc


def _load_pdf(p: Path) -> Document:
    import pymupdf

    try:
        pdf = pymupdf.open(p)
    except Exception as e:  # noqa: BLE001 — pymupdf бросает разные типы на битых файлах
        raise DocumentError(f"PDF повреждён или это не PDF: {p.name}") from e
    with pdf:
        if pdf.needs_pass:
            raise DocumentError(f"PDF защищён паролем: {p.name}. Снимите защиту и запустите снова.")
        if pdf.page_count == 0:
            raise DocumentError(f"В PDF нет ни одной страницы: {p.name}")
        raw: list[tuple[str, float, int]] = []   # текст строки, размер шрифта, страница
        empty_pages: list[int] = []
        try:
            for i, page in enumerate(pdf, start=1):
                before = len(raw)
                for block in page.get_text("dict")["blocks"]:
                    for ln in block.get("lines", []):
                        spans = [s for s in ln["spans"] if s["text"].strip()]
                        if spans:
                            raw.append(("".join(s["text"] for s in ln["spans"]).rstrip(),
                                        round(max(s["size"] for s in spans), 1), i))
                if len(raw) == before:
                    empty_pages.append(i)
        except Exception as e:  # noqa: BLE001
            raise DocumentError(f"PDF повреждён, страница не читается: {p.name} ({str(e)[:80]})") from e
        if not raw:
            raise DocumentError(f"В PDF нет текстового слоя (похоже на скан): {p.name}. "
                                "Распознайте текст (OCR) или передайте исходный .docx/.md.")
        lines, pages = _mark_pdf_headings(raw)
        doc = Document(p.name, "\n".join(lines), lines, pages)
        if empty_pages:
            doc.warnings.append(f"Страницы без текста (сканы или картинки), их содержимое не проверено: "
                                f"{', '.join(map(str, empty_pages))} из {pdf.page_count}.")
        return doc


def _load_docx(p: Path) -> Document:
    try:
        import docx  # python-docx
    except ImportError as e:
        raise DocumentError("Для .docx установите python-docx: pip install python-docx") from e
    try:
        d = docx.Document(str(p))
    except Exception as e:  # noqa: BLE001
        raise DocumentError(f"DOCX повреждён или это не DOCX: {p.name}") from e
    lines = [para.text for para in d.paragraphs]
    for table in d.tables:
        for row in table.rows:
            lines.append(" | ".join(c.text for c in row.cells))
    return Document(p.name, "\n".join(lines), lines, [0] * len(lines))


def _load_text(p: Path) -> Document:
    data = p.read_bytes()
    if b"\x00" in data[:8192]:
        raise DocumentError(f"Файл {p.name} двоичный, а не текстовый: возможно, у него неверное расширение.")
    warnings = []
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1251")
            warnings.append("Файл не в UTF-8, прочитан в кодировке Windows-1251.")
        except UnicodeDecodeError as e:
            raise DocumentError(f"Не удалось определить кодировку {p.name}. Сохраните файл в UTF-8.") from e
    lines = text.splitlines()
    return Document(p.name, text, lines, [0] * len(lines), warnings)


def _mark_pdf_headings(raw: list[tuple[str, float, int]]) -> tuple[list[str], list[int]]:
    """Строки, набранные шрифтом крупнее основного текста, помечаются как markdown-заголовки
    (# — самый крупный размер), чтобы нарезка по разделам работала так же, как для .md."""
    from collections import Counter

    if not raw:
        return [], []
    body = Counter(sz for _, sz, _ in raw).most_common(1)[0][0]
    head_sizes = sorted({sz for t, sz, _ in raw if sz > body + 0.5}, reverse=True)[:6]
    lines, pages = [], []
    for text, sz, page in raw:
        if sz in head_sizes and len(text) < 120:
            text = "#" * (head_sizes.index(sz) + 1) + " " + text.strip()
        lines.append(text)
        pages.append(page)
    return lines, pages


_PUNCT = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "’": "'", "‘": "'",
                        "—": "-", "–": "-", " ": " ", "ё": "е", "Ё": "Е"})


def norm(s: str) -> str:
    s = s.translate(_PUNCT)
    s = re.sub(r"(?<=\d)\s+(?=\d{3}\b)", "", s)   # «48 500» = «48500», какие бы пробелы ни стояли
    s = re.sub(r"[^\w\s]|_", " ", s)         # пунктуация, разметка markdown и таблиц — не важны для поиска
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


class QuoteIndex:
    """Проверяет, что цитата из ответа модели реально есть в документе, и находит её место."""

    def __init__(self, doc: Document):
        self.doc = doc
        self.norm_lines = [norm(l) for l in doc.lines]
        # склеенный нормализованный текст и карта «позиция → строка»
        self.flat = ""
        self.pos2line: list[int] = []
        for i, nl in enumerate(self.norm_lines):
            if not nl:
                continue
            chunk = nl + " "
            self.flat += chunk
            self.pos2line.extend([i] * len(chunk))

    def _loc(self, pos: int) -> str:
        line = self.pos2line[min(pos, len(self.pos2line) - 1)] if self.pos2line else 0
        page = self.doc.page_of_line[line] if self.doc.page_of_line else 0
        return f"стр. {page}, строка {line + 1}" if page else f"строка {line + 1}"

    def find(self, quote: str, min_ratio: float = 0.85) -> tuple[bool, str]:
        ok, loc = self._find_one(quote, min_ratio)
        if ok:
            return ok, loc
        # составная цитата: части через «…», «|» или «;» (две стороны противоречия) — каждая должна быть в документе
        parts = [p for p in re.split(r"\.\.\.|…|\||;", quote) if len(norm(p)) >= 4]
        if len(parts) > 1:
            res = [self._find_one(p, min_ratio) for p in parts]
            if all(ok for ok, _ in res):
                return True, res[0][1]
        return False, ""

    def _find_one(self, quote: str, min_ratio: float) -> tuple[bool, str]:
        q = norm(quote)
        if len(q) < 4:
            return False, ""
        pos = self.flat.find(q)
        if pos >= 0:
            return True, self._loc(pos)
        # нечёткий поиск: модель могла слегка переформулировать или склеить фрагменты
        sm = SequenceMatcher(None, self.flat, q, autojunk=False)
        m = sm.find_longest_match(0, len(self.flat), 0, len(q))
        if m.size == 0:
            return False, ""
        start = max(0, m.a - m.b)
        window = self.flat[start:start + len(q) + 10]
        ratio = SequenceMatcher(None, window, q, autojunk=False).ratio()
        if ratio >= min_ratio:
            return True, self._loc(start)
        return False, ""
