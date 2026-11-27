"""Eval решений по направлениям. Запуск: python -m scripts.eval_cases [comms] [quality] [onboarding] [b2b]
comms — без LLM (детерминированный проверяльщик); остальные — на модели из .env.
--no-guard — onboarding и b2b без автопроверок «сказал = сделал» и цен: база для сравнения."""
import json
import re
import sys
import time

from app import config
from app.cases import b2b, comms, common, onboarding, quality

OUT = {}


def run_comms():
    rows = json.loads((config.DATA / "comms/bad_texts.json").read_text(encoding="utf-8"))
    ok = 0
    for b in rows:
        got = {i["type"] for i in comms.check(b["text"], b["channel"], b["segment"])["issues"]}
        hit = set(b["expect"]) <= got and (bool(b["expect"]) or not got)
        ok += hit
        print(f"{'✅' if hit else '❌'} {b['id']} ожидалось {b['expect']} найдено {sorted(got)}")
    OUT["comms"] = {"passed": ok, "total": len(rows)}
    print(f"comms: {ok}/{len(rows)} текстов размечены верно\n")


def run_quality():
    t0 = time.time()
    for d in quality.S.dialogs:
        quality.S.results[d["id"]] = quality.score(d["dialog"])
        print(f"{d['id']}: ИИ {quality.S.results[d['id']]['total']}%  люди {round(100 * sum(d['labels'].values()) / 14)}%")
    a = quality.agreement(quality.S.results)
    OUT["quality"] = a
    print(f"quality: точное совпадение {a['exact']:.0%}, ±1 балл {a['within_1']:.0%}, {time.time() - t0:.0f} с\n")


def dialog(fn, turns):
    h, res, guards = [], None, []
    for t in turns:
        h.append({"role": "user", "content": t})
        res = fn(h)
        guards.append(res["guard"])
        h.append({"role": "assistant", "content": res["answer"]})
    return res, h, guards


ONB = [
    ("unknown_address", ["Хочу интернет на Невском проспекте, 1"], lambda r, h: not onboarding.S.applications and re.search(r"уточн|не наш|нет в баз|лист ожид|не найден", r["answer"], re.I)),
    ("private_house", ["Частный дом в Парголово, ул. Николая Рубцова, 9 — можно подключить?"], lambda r, h: re.search(r"инженер|выезд|оценк", r["answer"], re.I)),
    ("construction", ["Улица Оптиков, 4 — есть у вас интернет?"], lambda r, h: re.search(r"20|декабр|лист ожид|подключа", r["answer"], re.I)),
    ("full_booking", ["Хочу подключить интернет, Савушкина 12 кв 40", "Нас трое, муж работает из дома, дети смотрят ютуб, ТВ не нужно, роутера нет",
                      "Давайте Дом 500, роутер в аренду. Анна, +7 911 123-45-67, самый ранний слот"],
     lambda r, h: len(onboarding.S.applications) == 1 and r["trust"]["ok"]),
]
B2B = [
    ("no_prices_to_client", ["Сколько стоит интернет для офиса на 20 человек? Назовите цену."], lambda r, h: not re.search(r"\d\s*(₽|руб)", r["answer"])),
    ("brief_to_manager", ["Сеть из 3 кофеен, в каждой касса с эквайрингом, гости просят Wi-Fi, хотим видеть зал. Интернет падал на полдня, не могли принимать карты.",
                          "Сейчас домашний интернет 100 Мбит у другого провайдера, сотрудников 12, по 2 камеры на точку. Срочно. Ирина, +7 900 123-45-67, ООО «Зерно»",
                          "Да, всё верно"], lambda r, h: len(b2b.S.leads) == 1 and not re.search(r"\d\s*(₽|руб)", " ".join(m["content"] for m in h if m["role"] == "assistant"))),
]


def run_chat(name, mod, cases):
    ok = caught = fixed = 0
    rows = []
    for cid, turns, check in cases:
        mod.reset()
        t0 = time.time()
        r, h, guards = dialog(mod.answer, turns)
        hit = bool(check(r, h))
        ok += hit
        c = sum(bool(g["violations"]) for g in guards)
        caught += c
        fixed += sum(bool(g["violations"]) and g["ok"] for g in guards)
        rows.append({"id": cid, "pass": hit, "guard_caught": c, "answer": r["answer"]})
        print(f"{'✅' if hit else '❌'} {cid:22} {time.time() - t0:5.1f}s guard={r['guard'].get('violations') or 'ok'} → {r['answer'][:160]!r}")
    OUT[name] = {"passed": ok, "total": len(cases), "guard_caught": caught, "guard_fixed": fixed, "cases": rows}
    print(f"{name}: {ok}/{len(cases)}" + ("" if not common.GUARD else f"; автопроверка поймала {caught}, исправлено {fixed}") + "\n")


if __name__ == "__main__":
    common.GUARD = "--no-guard" not in sys.argv
    want = [a for a in sys.argv[1:] if not a.startswith("--")] or ["comms", "quality", "onboarding", "b2b"]
    if "comms" in want:
        run_comms()
    if "quality" in want:
        run_quality()
    if "onboarding" in want:
        run_chat("onboarding", onboarding, ONB)
    if "b2b" in want:
        run_chat("b2b", b2b, B2B)
    model = config.CLAUDE_MODEL if config.PROVIDER == "claude" else config.OLLAMA_MODEL
    suffix = "" if common.GUARD else "_noguard"
    path = f"reports/cases_eval_{model.replace(':', '_')}{suffix}.json"
    try:
        prev = json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError):
        prev = {}
    open(path, "w", encoding="utf-8").write(json.dumps(prev | {"model": model, "guard": common.GUARD, **OUT}, ensure_ascii=False, indent=1))
    print(json.dumps({k: {x: y for x, y in v.items() if x != "cases"} for k, v in OUT.items()}, ensure_ascii=False))
