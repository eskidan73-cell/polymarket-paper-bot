"""Как зависит контртренд-сигнал от длины серии свечей (3..8).

Тянет историю 5-минутных свечей BTC-USD с Coinbase (публичный API, кэш в
candles_5m.csv), для каждой длины серии n считает: после n подряд закрытых
свечей в одну сторону ставим против серии на СЛЕДУЮЩЕЕ 5-мин окно;
выигрыш = окно закрылось в обратную сторону.

Важно: реальные рынки Polymarket резолвятся по Chainlink, а не по Coinbase, и
цена входа там не ровно 0.50 — это упрощение для оценки идеи на большой выборке.
Запуск: python streak_study.py [дней_истории]   (по умолчанию 90)
"""
import csv
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "candles_5m.csv")
STEP = 300
PAGE = 300  # макс. свечей за запрос


def fetch(days):
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    rows = {}
    if os.path.exists(CACHE):
        with open(CACHE, newline="") as f:
            for t, o, c in csv.reader(f):
                rows[int(t)] = (float(o), float(c))
    have_from = min(rows) if rows else None
    have_to = max(rows) if rows else None
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(seconds=STEP * PAGE), end)
        lo, hi = int(cur.timestamp()), int(nxt.timestamp())
        if have_from and have_to and lo >= have_from and hi <= have_to:
            cur = nxt
            continue
        for attempt in range(4):
            try:
                r = requests.get(URL, params={"granularity": STEP, "start": cur.isoformat(), "end": nxt.isoformat()}, timeout=60)
                r.raise_for_status()
                break
            except requests.RequestException as e:
                print(f"retry {attempt + 1} ({cur:%Y-%m-%d %H:%M}): {type(e).__name__}", flush=True)
                if attempt == 3:
                    raise
                time.sleep(3)
        for t, low, high, o, c, v in r.json():
            rows[int(t)] = (float(o), float(c))
        cur = nxt
        print(f"ok до {cur:%Y-%m-%d %H:%M}, свечей в кэше: {len(rows)}", flush=True)
        with open(CACHE, "w", newline="") as f:
            w = csv.writer(f)
            for t in sorted(rows):
                w.writerow([t, *rows[t]])
        time.sleep(0.15)
    with open(CACHE, "w", newline="") as f:
        w = csv.writer(f)
        for t in sorted(rows):
            w.writerow([t, *rows[t]])
    return [(t, *rows[t]) for t in sorted(rows) if t >= int(start.timestamp())]


def study(candles, n):
    # только подряд идущие свечи без дыр в данных
    wins = total = 0
    for i in range(n, len(candles)):
        window = candles[i - n:i + 1]
        if any(window[k + 1][0] - window[k][0] != STEP for k in range(n)):
            continue
        dirs = [1 if c > o else (-1 if c < o else 0) for _, o, c in window[:n]]
        if 0 in dirs or len(set(dirs)) != 1:
            continue
        nxt = window[n]
        nd = 1 if nxt[2] > nxt[1] else (-1 if nxt[2] < nxt[1] else 0)
        if nd == 0:
            continue
        total += 1
        wins += 1 if nd == -dirs[0] else 0
    return wins, total


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    candles = fetch(days)
    print(f"Свечей: {len(candles)}, период {datetime.fromtimestamp(candles[0][0], timezone.utc):%Y-%m-%d} .. "
          f"{datetime.fromtimestamp(candles[-1][0], timezone.utc):%Y-%m-%d}")
    ups = sum(1 for _, o, c in candles if c > o)
    print(f"Базовая доля 'Up' по всем свечам: {100*ups/len(candles):.1f}%\n")
    print(f"{'серия n':>8} {'сигналов':>9} {'против серии':>13} {'95% интервал':>16} {'ROI при цене 0.50':>19}")
    for n in range(2, 9):
        w, t = study(candles, n)
        if t == 0:
            continue
        p = w / t
        se = math.sqrt(p * (1 - p) / t)
        # ставка $1 по цене 0.50: выигрыш +1 (1/0.5-1), проигрыш -1  => ROI = 2p-1
        print(f"{n:>8} {t:>9} {100*p:>12.1f}% {100*(p-1.96*se):>7.1f}..{100*(p+1.96*se):<6.1f}% {100*(2*p-1):>+18.1f}%")
    print("\nЕсли интервал накрывает 50% — преимущества нет (комиссия и цена >0.50 только ухудшают).")


if __name__ == "__main__":
    main()
