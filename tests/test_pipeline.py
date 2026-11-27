"""Детерминированная часть конвейера: сверка цитат, ревизия, калибровка, группировка, правила."""
from validator import pipeline as P
from validator.ingest import QuoteIndex, load
from validator.llm import LLMError
from validator.schema import Finding

from conftest import SAMPLE


def F(crit="unambiguous", sev="major", quote="q", req="R01", scope="requirement"):
    return Finding(req, crit, sev, quote, "e", "f", scope=scope)


class Reviewer:
    def __init__(self, answer):
        self.answer = answer

    def json(self, prompt, label="", reasoning=None):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_verify_drops_invented_quotes():
    doc = load(SAMPLE)
    real = next(l for l in doc.lines if len(l.strip()) > 40).strip()[:50]
    kept, dropped = P.verify([F(quote=real), F(quote="выдуманная цитата, которой нет")], QuoteIndex(doc))
    assert [f.quote for f in kept] == [real] and len(dropped) == 1 and kept[0].location


def test_review_drops_and_recalibrates():
    fs = [F(quote=f"q{i}") for i in range(5)]
    ans = {"items": [{"n": 1, "verdict": "drop", "reason": "ложное"},
                     {"n": 2, "verdict": "keep", "severity": "minor"}]}
    kept, rejected = P.review(Reviewer(ans), load(SAMPLE), fs)
    assert len(kept) == 4 and rejected[0]["reason"] == "ложное"
    assert kept[0].severity == "minor"


def test_review_never_touches_critical():
    fs = [F(sev="critical", quote="a"), F(quote="b"), F(quote="c")]
    ans = {"items": [{"n": 1, "verdict": "drop"}, {"n": 2, "verdict": "keep", "severity": "minor"}]}
    kept, _ = P.review(Reviewer({"items": [{"n": 1, "verdict": "keep", "severity": "minor"}]}), load(SAMPLE), fs[:1])
    assert kept[0].severity == "critical"
    kept, rejected = P.review(Reviewer(ans), load(SAMPLE), fs)
    assert fs[0] in kept and not rejected


def test_review_refuses_mass_drop():
    fs = [F(quote=f"q{i}") for i in range(10)]
    ans = {"items": [{"n": i, "verdict": "drop"} for i in range(1, 8)]}
    kept, rejected = P.review(Reviewer(ans), load(SAMPLE), fs)
    assert len(kept) == 10 and not rejected


def test_review_survives_model_failure():
    fs = [F(quote="a")]
    kept, rejected = P.review(Reviewer(LLMError("нет ответа")), load(SAMPLE), fs)
    assert kept == fs and not rejected


def test_review_skips_rule_findings():
    rule = F(scope="rule", quote="TBD")
    kept, _ = P.review(Reviewer({"items": [{"n": 1, "verdict": "drop"}]}), load(SAMPLE), [rule])
    assert kept == [rule]


def test_calibrate_ceilings():
    fs = [F("necessary", "critical"), F("ranked", "critical", scope="set"),
          F("consistent", "critical", scope="set"), F("necessary", "major", scope="set")]
    assert P.calibrate(fs) == 2
    assert [f.severity for f in fs] == ["minor", "major", "critical", "major"]


def test_consolidate_nests_extra_findings_of_one_requirement():
    a, b = F(sev="critical", quote="один два три четыре"), F(crit="complete", quote="пять шесть семь")
    per_req, per_set = P.consolidate([a, b], [])
    assert per_req == [a] and len(a.related) == 1 and not per_set


def test_rule_checks_find_placeholders(tmp_path):
    p = tmp_path / "d.md"
    p.write_text("# ТЗ\nОписание интеграции: TBD\nСистема должна работать быстро и удобно для клиента.\n",
                 encoding="utf-8")
    rules = P.rule_checks(load(p))
    assert any(f.criterion == "complete" and "TBD" in f.quote for f in rules)
    assert any(f.criterion == "unambiguous" and f.severity == "minor" for f in rules)


def test_verdict_blocks_on_critical():
    v = P.make_verdict([], [F(sev="critical")], [])
    assert not v.ready and v.top_fixes
    assert P.make_verdict([], [F(sev="minor")], []).ready
