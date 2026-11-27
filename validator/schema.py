"""Контракт данных между частями агента. Поля менять только по договорённости команды."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

CRITERIA = (
    "necessary", "appropriate", "unambiguous", "complete", "singular", "feasible",
    "verifiable", "correct", "conforming", "consistent", "traceable", "comprehensible",
)
SEVERITIES = ("critical", "major", "minor")
SEVERITY_RU = {"critical": "критичный", "major": "существенный", "minor": "незначительный"}
CRITERION_RU = {
    "necessary": "Необходимость",
    "appropriate": "Уместность уровня (нет лишних проектных решений)",
    "unambiguous": "Однозначность",
    "complete": "Полнота",
    "singular": "Единичность",
    "feasible": "Выполнимость",
    "verifiable": "Проверяемость",
    "correct": "Корректность",
    "conforming": "Соответствие шаблону/стандарту",
    "consistent": "Непротиворечивость",
    "traceable": "Трассируемость",
    "comprehensible": "Понятность",
}


@dataclass
class Requirement:
    id: str                 # R01 — наш идентификатор в отчёте
    doc_id: str | None      # Ф-1 / None, если в документе номера нет
    section: str            # где в документе
    quote: str              # дословная цитата
    type: str = "other"
    location: str = ""      # «стр. 2» / «строка 57», заполняет код
    quote_verified: bool = False


@dataclass
class Finding:
    req_id: str             # R01, "R03,R07" или "SET"
    criterion: str
    severity: str
    quote: str
    explanation: str
    fix: str = ""
    location: str = ""
    quote_verified: bool = False
    scope: str = "requirement"  # requirement | set | rule
    related: list = field(default_factory=list)  # вложенные замечания к тому же месту: {criterion, severity, quote, explanation, fix}


@dataclass
class Passport:
    document: str
    date: str
    model: str
    commit: str
    agent_version: str


@dataclass
class Verdict:
    ready: bool
    summary: str
    top_fixes: list[str] = field(default_factory=list)


@dataclass
class Report:
    passport: Passport
    requirements: list[Requirement]
    findings: list[Finding]          # по отдельным требованиям
    set_findings: list[Finding]      # по набору целиком
    verdict: Verdict
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
