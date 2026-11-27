"""Чтение документа: битые, пустые и неподходящие файлы дают понятную ошибку, а не падение."""
import pytest

from validator.ingest import DocumentError, QuoteIndex, load

from conftest import SAMPLE


@pytest.mark.parametrize("name, data, words", [
    ("empty.md", b"", "пустой"),
    ("broken.pdf", b"%PDF-1.4 garbage", "PDF"),
    ("broken.docx", b"PK\x03\x04junk", "DOCX"),
    ("bin.txt", bytes(range(256)) * 2, "двоичный"),
    ("short.md", b"hi\n", "почти нет текста"),
    ("old.doc", b"x" * 100, ".doc"),
    ("img.png", b"x" * 100, "Неподдерживаемый"),
])
def test_bad_files(tmp_path, name, data, words):
    p = tmp_path / name
    p.write_bytes(data)
    with pytest.raises(DocumentError, match=words):
        load(p)


def test_missing_file(tmp_path):
    with pytest.raises(DocumentError, match="не найден"):
        load(tmp_path / "nope.md")


def test_cp1251_is_read_with_warning(tmp_path):
    p = tmp_path / "cp.txt"
    p.write_bytes(("Система должна отправлять уведомление клиенту. " * 3).encode("cp1251"))
    doc = load(p)
    assert "уведомление" in doc.text
    assert any("1251" in w for w in doc.warnings)


def test_sample_loads_and_quotes_are_found():
    doc = load(SAMPLE)
    idx = QuoteIndex(doc)
    line = next(l for l in doc.lines if len(l.strip()) > 40).strip()
    found, where = idx.find(line[:50])
    assert found and where
    assert idx.find("этой фразы точно нет в документе требований")[0] is False
