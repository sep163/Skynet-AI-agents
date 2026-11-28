"""Разобрать все звонки продаж моделью из .env и сохранить результат в data/sales/analysis_cache.json
(дашборд РОП работает по этому кэшу без обращения к модели). Запуск: python -m scripts.analyze_sales [S01 S02 ...]"""
import sys
import time

from app.cases import sales

ids = sys.argv[1:] or [c["id"] for c in sales.S.calls]
for cid in ids:
    t0 = time.time()
    r = sales.analyze_call(cid)
    print(f"{cid} {r['total']:>3}% риск: {', '.join(r['risk_zones']) or '—'} · {time.time() - t0:.0f} с · {r['feedback']['fix'][:1]}")
d = sales.dashboard()
print("\n" + (d["main_problem"] or ""))
for w in d["weak_links"][:6]:
    print(f"  слабое звено: {w['dimension']} {w['who']} — {w['stage']} {w['score']}%")
