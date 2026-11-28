"""Личная норма речи клиента: средние громкость, темп и разброс высоты тона по его прошлым записям (Уэлфорд).
Возбуждение оценивается относительно этой нормы, а не общей: у кого-то громкая речь — обычное дело.
Хранится только числовая статистика голоса и счётчик манер — без записей и без выводов о человеке."""
import json
import math
from collections import Counter

from . import config

PATH = config.DATA / "runtime" / "voice_profiles.json"
FEATS = ("loudness_db", "words_per_sec", "pitch_std_st")
_p: dict = {}


def _load():
    global _p
    if not _p and PATH.exists():
        _p = json.loads(PATH.read_text(encoding="utf-8"))
    return _p


def _save():
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(_p, ensure_ascii=False, indent=1), encoding="utf-8")


def update(cid: str, voice: dict, manner: str, hostility: int):
    p = _load().setdefault(cid, {"n": 0, "stats": {}, "manner": {}, "hostility_max": 0})
    p["n"] += 1
    for k in FEATS:
        v = voice.get(k)
        if v is None:
            continue
        s = p["stats"].setdefault(k, {"n": 0, "mean": 0.0, "m2": 0.0})
        s["n"] += 1
        d = v - s["mean"]
        s["mean"] += d / s["n"]
        s["m2"] += d * (v - s["mean"])
    p["manner"] = dict(Counter(p["manner"]) + Counter({manner: 1}))
    p["hostility_max"] = max(p["hostility_max"], hostility)
    _save()


def baseline(cid: str):
    p = _load().get(cid)
    if not p:
        return None
    out = {"n": p["n"], "usual_manner": max(p["manner"], key=p["manner"].get) if p["manner"] else None}
    for k, s in p["stats"].items():
        out[k] = {"mean": s["mean"], "std": math.sqrt(s["m2"] / (s["n"] - 1)) if s["n"] > 1 else 0.0}
    return out


def reset():
    global _p
    _p = {}
    if PATH.exists():
        PATH.unlink()
