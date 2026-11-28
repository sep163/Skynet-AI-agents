"""Разобрать звонки продаж моделью из .env и сохранить в data/sales/analysis_cache.json (дашборды работают по кэшу без модели).
Запуск: python -m scripts.analyze_sales            — только ещё не разобранные
        python -m scripts.analyze_sales --all      — все заново
        python -m scripts.analyze_sales S01 S02    — выбранные"""
import sys
import time

from app.cases import sales

args = [a for a in sys.argv[1:] if not a.startswith("--")]
ids = args or [c["id"] for c in sales.S.calls if "--all" in sys.argv or c["id"] not in sales.S.results]
fails = []
for cid in ids:
    for attempt in (1, 2):
        t0 = time.time()
        try:
            r = sales.analyze_call(cid)
            print(f"{cid} {r['total']:>3}% риск: {', '.join(r['risk_zones']) or '—'} · {r['style_verdict']} · {time.time() - t0:.0f} с", flush=True)
            break
        except Exception as e:
            print(f"{cid} попытка {attempt}: {type(e).__name__} {str(e)[:120]}", flush=True)
    else:
        fails.append(cid)
d = sales.department()
print("\nСлабые места отдела:")
for it in d["items"][:6]:
    print(f"  {it['title']}: {it['value']} {it['unit']} · слабее всех: {it['managers'][0]['manager'] if it['managers'] else '—'}")
if fails:
    print("Не разобраны:", ", ".join(fails))
