"""Сравнение старой единой шкалы «тона» с новой оценкой по осям на размеченных репликах.
Запуск: python -m scripts.eval_tone [--llm]   (без --llm — только правила, детерминированно)"""
import json
import re
import sys

from app import config, emotion

VOICE = {"loud": {"loudness_db": -10, "words_per_sec": 3.8, "pitch_std_st": 4.5}, "quiet": {"loudness_db": -28, "words_per_sec": 2.2, "pitch_std_st": 1.0}}
OLD_CHURN = r"(растор|перейд|уйду|ухожу|отключ[иа]т[ье]с|отказ[ыа]ва|суд\b|в суд|роспотреб|прокурат|верните деньги)"
OLD_RUDE = r"(бесит|достал|издева|сколько можно|безобраз|позор|кошмар|ужас|тупы|идиот|обман|разв[оеё]д|дурдом|задолбал|надоел)"
OLD_UNHAPPY = r"(опять|снова|постоянно|третий раз|второй раз|невозможно|не работает|когда уже|почему)"


def old(text, voice):
    """Прежняя логика: одна шкала 0–3; ≥2 = «агрессивный», 3 = к оператору; громкий голос повышает уровень."""
    letters = [c for c in text if c.isalpha()]
    caps = sum(c.isupper() for c in letters) / max(len(letters), 1)
    bangs = text.count("!")
    lvl = 3 if re.search(OLD_CHURN, text, re.I) else 2 if re.search(OLD_RUDE, text, re.I) or (caps > 0.5 and len(letters) > 10) or bangs >= 3 else 1 if re.search(OLD_UNHAPPY, text, re.I) or bangs else 0
    if voice and lvl >= 1 and voice["loudness_db"] > -18 and voice["words_per_sec"] > 3.2:
        lvl = min(3, lvl + 1)
    return {"aggressive": lvl >= 2, "escalate": lvl == 3}


def main():
    use_llm = "--llm" in sys.argv
    cases = json.loads((config.DATA / "eval/tone_cases.json").read_text(encoding="utf-8"))["cases"]
    tot = {"old": {"agg": 0, "esc": 0, "fp": 0, "fn": 0}, "new": {"agg": 0, "esc": 0, "fp": 0, "fn": 0}}
    print(f"{'id':4} {'агр.':5} {'было':5} {'стало':6} {'к опер.':8} {'было':5} {'стало':6}  реплика")
    for c in cases:
        v = VOICE.get(c["voice"])
        o = old(c["text"], v)
        e = emotion.analyze(c["text"], v, use_llm=use_llm)
        n = {"aggressive": e["hostility"] >= 2, "escalate": e["escalate"]}
        for k, r in (("old", o), ("new", n)):
            tot[k]["agg"] += r["aggressive"] == c["aggressive"]
            tot[k]["esc"] += r["escalate"] == c["escalate"]
            tot[k]["fp"] += r["aggressive"] and not c["aggressive"]
            tot[k]["fn"] += c["aggressive"] and not r["aggressive"]
        m = lambda a, b: ("✅" if a == b else "❌") + ("да " if a else "нет")
        print(f"{c['id']:4} {'да ' if c['aggressive'] else 'нет':5} {m(o['aggressive'], c['aggressive']):5} {m(n['aggressive'], c['aggressive']):6} "
              f"{'да ' if c['escalate'] else 'нет':8} {m(o['escalate'], c['escalate']):5} {m(n['escalate'], c['escalate']):6}  {c['text'][:60]}")
    N = len(cases)
    for k, name in (("old", "Старая шкала"), ("new", "Новая оценка")):
        t = tot[k]
        print(f"{name}: «агрессивный» верно {t['agg']}/{N} (зря помечены {t['fp']}, пропущены {t['fn']}), эскалация верно {t['esc']}/{N}")
    open("reports/tone_eval.json", "w").write(json.dumps({"n": N, "llm": use_llm, **tot}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
