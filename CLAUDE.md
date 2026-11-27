# Агент проверки требований (хакатон BCC-HUB, дедлайн 27.11, 23:59 по Астане)

Команда: Дамир (движок: validator/*.py, финальный прогон) и Омар (prompts/, templates/, eval/golden_defects.json, README).

## Правила
- Контракт данных — `validator/schema.py` (Requirement, Finding, Report). Поля не менять без согласия напарника.
- Чужие файлы не править: Дамир — `validator/`, Омар — `prompts/`, `templates/`, `eval/golden_defects.json`, `README.md`.
- Промпты: плейсхолдеры `{document}`, `{criteria}`, `{requirements_json}` подставляются через str.replace (фигурные скобки JSON в тексте промпта допустимы). Ответ модели — строго JSON-объект `{"items":[...]}`.
- Каждая цитата (quote) должна дословно встречаться в документе: код отбрасывает непроверенные.
- Отчёт только на русском. Без общих фраз: каждое замечание привязано к месту документа.
- Секреты только в `.env` (в git не попадает).

## Команды
- Прогон: `python -m validator input/requirements.md reports/report.md`
- Оценка: `python eval/score.py reports/report.json`
- Без кэша: добавить `--no-cache`
