@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo === Прогон агента по PDF (5-8 минут) ===
python -m validator input/requirements.pdf reports/report_pdf.md --no-cache
echo.
echo === Оценка по эталону ===
python eval/score.py reports/report_pdf.json > reports\score_pdf.txt 2>&1
type reports\score_pdf.txt
echo.
pause
