"""Уровень недовольства клиента 0–3: правила по тексту + LLM + голосовые признаки (громкость, темп)."""
import re

from . import config, llm

LABELS = {0: "спокоен", 1: "недоволен", 2: "раздражён", 3: "в ярости / риск ухода"}
CHURN = re.compile(r"(растор|перейд|уйду|ухожу|отключ[иа]т[ье]с|отказ[ыа]ва|суд\b|в суд|роспотреб|прокурат|верните деньги)", re.I)
RUDE = re.compile(r"(бесит|достал|издева|сколько можно|безобраз|позор|кошмар|ужас|тупы|идиот|обман|разв[оеё]д|дурдом|задолбал|надоел)", re.I)
UNHAPPY = re.compile(r"(опять|снова|постоянно|третий раз|второй раз|невозможно|не работает|когда уже|почему)", re.I)
SCHEMA = {
    "type": "object",
    "properties": {
        "level": {"type": "integer", "enum": [0, 1, 2, 3]},
        "emotion": {"type": "string", "enum": ["calm", "confused", "disappointed", "irritated", "angry"]},
        "churn_risk": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["level", "emotion", "churn_risk", "reason"],
    "additionalProperties": False,
}
SYSTEM = ("Оцени эмоциональное состояние клиента интернет-провайдера по его сообщению. level: 0 — спокоен, 1 — недоволен, "
          "2 — раздражён (резкие формулировки, претензии), 3 — в ярости или угрожает уйти/судом/жалобой. reason — одна короткая фраза по-русски.")


def rules(text: str) -> dict:
    letters = [c for c in text if c.isalpha()]
    caps = sum(c.isupper() for c in letters) / max(len(letters), 1)
    churn, rude, unhappy = bool(CHURN.search(text)), len(RUDE.findall(text)), len(UNHAPPY.findall(text))
    bangs = text.count("!")
    lvl = 3 if churn else 2 if rude or (caps > 0.5 and len(letters) > 10) or bangs >= 3 else 1 if unhappy or bangs else 0
    return {"level": lvl, "churn_risk": churn, "signals": {"caps": round(caps, 2), "rude": rude, "unhappy": unhappy, "bangs": bangs}}


def voice_bump(voice: dict | None) -> int:
    if not voice:
        return 0
    return int(voice.get("loudness_db", -99) > -18) + int(voice.get("words_per_sec", 0) > 3.2)


def analyze(text: str, voice: dict | None = None, use_llm: bool | None = None) -> dict:
    r = rules(text)
    res = {"level": r["level"], "emotion": None, "churn_risk": r["churn_risk"], "reason": "по ключевым словам", "rules": r["signals"]}
    if config.EMOTION_LLM if use_llm is None else use_llm:
        try:
            j = llm.complete_json(SYSTEM, text, SCHEMA)
            res.update(emotion=j["emotion"], reason=j["reason"], llm_level=j["level"], churn_risk=j["churn_risk"] or r["churn_risk"])
            res["level"] = max(j["level"], r["level"] if r["level"] == 3 else 0)
        except Exception as e:
            res["llm_error"] = str(e)[:200]
    vb = voice_bump(voice)
    if vb and res["level"] >= 1:
        res["level"] = min(3, res["level"] + (1 if vb == 2 else 0))
        res["voice"] = {"bump": vb, **{k: voice[k] for k in ("loudness_db", "words_per_sec") if k in voice}}
    res["label"] = LABELS[res["level"]]
    return res
