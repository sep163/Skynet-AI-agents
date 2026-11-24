"""BM25 по markdown-базе знаний. Без внешних зависимостей; при желании заменяется на эмбеддинги."""
import math
import re
from collections import Counter
from pathlib import Path

from . import config


def tok(s: str) -> list[str]:
    return [w[:6] for w in re.findall(r"[a-zа-яё0-9]+", s.lower()) if len(w) > 2]


class KB:
    def __init__(self, folder: Path = config.DATA / "kb"):
        self.docs = []
        for p in sorted(folder.glob("*.md")):
            text = p.read_text(encoding="utf-8")
            title = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), p.stem)
            for part in re.split(r"\n(?=## )", text):
                if part.startswith("## "):
                    head, _, body = part.partition("\n")
                    self.docs.append({"source": p.name, "title": f"{title} / {head[3:].strip()}", "text": body.strip()})
        self.tf = [Counter(tok(d["title"] + " " + d["text"])) for d in self.docs]
        self.len = [sum(t.values()) for t in self.tf]
        self.avg = sum(self.len) / max(len(self.len), 1)
        df = Counter(w for t in self.tf for w in t)
        n = len(self.docs)
        self.idf = {w: math.log(1 + (n - f + 0.5) / (f + 0.5)) for w, f in df.items()}

    def search(self, query: str, k: int = 3) -> list[dict]:
        q, res = tok(query), []
        for i, t in enumerate(self.tf):
            s = sum(self.idf[w] * t[w] * 2.2 / (t[w] + 1.2 * (0.25 + 0.75 * self.len[i] / self.avg)) for w in q if w in t)
            if s > 0:
                res.append((s, i))
        return [{**self.docs[i], "score": round(s, 2)} for s, i in sorted(res, reverse=True)[:k]]


_kbs: dict[str, KB] = {}


def get_kb(folder: str = "kb") -> KB:
    if folder not in _kbs:
        _kbs[folder] = KB(config.DATA / folder)
    return _kbs[folder]
