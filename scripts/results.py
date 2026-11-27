"""Сводная таблица результатов проверки для защиты: собирает reports/*.json в docs/results.md.

Запуск после eval-прогонов: python -m scripts.results [папка_с_отчётами]
Для сравнения «с автопроверкой и без» нужны оба прогона:
  python -m scripts.eval_agent && python -m scripts.eval_agent --no-guard
  python -m scripts.eval_cases onboarding b2b && python -m scripts.eval_cases onboarding b2b --no-guard
"""
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(d: Path) -> dict:
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))}


def pct(p, t):
    return f"{p}/{t} ({round(100 * p / t)}%)" if t else "—"


def guard_rows(r: dict) -> list[str]:
    """Строки «с автопроверкой / без» по агенту поддержки и чат-кейсам подключения и B2B."""
    rows = []
    for name, rep in r.items():
        if name.endswith("_noguard"):
            continue
        base = r.get(f"{name}_noguard") or {}
        if name.startswith("agent_eval"):
            pairs = [("Поддержка (первая линия)", rep, base or None)]
        else:
            pairs = [(title, rep[k], base.get(k)) for k, title in (("onboarding", "Подключение"), ("b2b", "Бизнес-клиенты")) if k in rep]
        for title, x, y in pairs:
            if "passed" in x:
                rows.append(f"| {title} | {rep.get('model', '?')} | {pct(x['passed'], x['total'])} | "
                            f"{pct(y['passed'], y['total']) if y else 'не прогонялось'} | {x.get('guard_caught', '—')} / {x.get('guard_fixed', '—')} |")
    return rows


def main():
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "reports"
    r = load(d)
    out = [f"# Результаты проверки\n", f"_Сгенерировано `python -m scripts.results` {date.today():%d.%m.%Y}. Исходные отчёты — `reports/*.json`._\n"]

    s = r.get("sentinel_eval")
    if s:
        o = s["overall"]
        out += ["## Клиентские обращения: Сентинел сети",
                "С чем сравниваем: сейчас о неисправности узнают из первой жалобы клиента, Сентинел — из телеметрии. "
                f"Данные — симулятор сети (`app/network.py`): {s['seeds']} прогонов по {s['hours_per_seed']:g} ч, случайных неисправностей: {s['faults']}.\n",
                "| Тип неисправности | n | Обнаружено | Раньше первой жалобы | Выигрыш до жалобы, мин (медиана) |", "|---|---|---|---|---|",
                *[f"| {k} | {v['n']} | {v['recall']:.0%} | {v['detected_before_first_complaint']:.0%} | {v['median_lead_vs_complaint_min']:g} |" for k, v in s["by_kind"].items()],
                "", f"Итого: обнаружено {o['recall']:.0%}, ложных инцидентов {s['false_incidents']} ({s['false_per_day']:g} в сутки).\n"]

    rows = guard_rows(r)
    out += ["## Автопроверка «сказал = сделал» и цен: с ней и без неё",
            "С чем сравниваем: тот же агент на тех же сценариях, но ответ уходит клиенту без автопроверки (`--no-guard`).",
            "Последний столбец — сколько раз автопроверка поймала расхождение (бот сообщил о действии, которого не было, или назвал цену не из прайса) и сколько из них исправлено.\n"]
    if rows:
        out += ["| Направление | Модель | С автопроверкой | Без автопроверки | Поймано / исправлено |", "|---|---|---|---|---|", *rows, ""]
    else:
        out += ["_Прогонов с моделью ещё нет: нужен ключ API или Ollama, см. команды в начале `scripts/results.py`._\n"]

    for name, rep in r.items():
        q = rep.get("quality") if isinstance(rep, dict) else None
        if q and rep.get("guard", True):
            out += [f"## Качество обслуживания ({rep['model']})", "С чем сравниваем: оценки людей на размеченных диалогах (`data/quality/dialogs.json`).\n",
                    f"- точное совпадение балла: {q['exact']:.0%}", f"- расхождение не больше 1 балла: {q['within_1']:.0%}",
                    f"- диалогов: {q['n_dialogs']}", ""]
            break

    c = next((rep["comms"] for rep in r.values() if isinstance(rep, dict) and "comms" in rep), None)
    if c:
        out += ["## Реклама: фактчек текстов", "С чем сравниваем: ручная разметка ошибок в текстах (`data/comms/bad_texts.json`), включая корректные тексты (ложные срабатывания).\n",
                f"- размечено верно: {pct(c['passed'], c['total'])}", ""]

    out += ["## Ограничения проверки", "- Сценариев мало (поддержка 7, подключение 4, B2B 2, качество 10, реклама 7): это проверка работоспособности, а не статистика.",
            "- Сентинел проверен на симуляторе, который написан той же командой: на пилоте нужен прогон на исторической телеметрии SkyNet.",
            "- Тарифы, адреса и каталог B2B — демо-данные."]
    dst = ROOT / "docs" / "results.md"
    dst.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"→ {dst.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
