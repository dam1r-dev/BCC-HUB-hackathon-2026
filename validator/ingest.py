"""Чтение документа требований и поиск цитат в нём."""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path


@dataclass
class Document:
    name: str
    text: str                  # полный текст, как его видит модель
    lines: list[str]
    page_of_line: list[int]    # номер страницы для каждой строки (PDF), иначе 0


def load(path: str | Path) -> Document:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Файл не найден: {p}")
    ext = p.suffix.lower()
    if ext == ".pdf":
        import pymupdf

        lines: list[str] = []
        pages: list[int] = []
        with pymupdf.open(p) as pdf:
            for i, page in enumerate(pdf, start=1):
                for ln in page.get_text("text").splitlines():
                    lines.append(ln)
                    pages.append(i)
        return Document(p.name, "\n".join(lines), lines, pages)
    if ext == ".docx":
        try:
            import docx  # python-docx
        except ImportError as e:
            raise RuntimeError("Для .docx установите python-docx: pip install python-docx") from e
        d = docx.Document(str(p))
        lines = [para.text for para in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                lines.append(" | ".join(c.text for c in row.cells))
        return Document(p.name, "\n".join(lines), lines, [0] * len(lines))
    if ext in (".md", ".txt", ".markdown"):
        text = p.read_text(encoding="utf-8")
        lines = text.splitlines()
        return Document(p.name, text, lines, [0] * len(lines))
    raise ValueError(f"Неподдерживаемый формат: {ext}. Поддерживаются .pdf, .md, .txt, .docx")


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
