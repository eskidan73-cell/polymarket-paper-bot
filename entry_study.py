"""Честный P&L контртренд-сигнала на реальных ценах входа Polymarket, с комиссией.

Для каждого окна, где по свечам Coinbase (candles_5m.csv из streak_study.py)
сработал сигнал серии n=3..7 (ставим против серии), берём:
  - рынок btc-updown-5m-<ts> из Gamma (токены, итоговые outcomePrices);
  - цену токена нужного исхода на старте окна из CLOB prices-history.
Исход = как резолвнул сам Polymarket (не Coinbase). Комиссия по feeSchedule рынка:
fee = rate * (p*(1-p))**exponent  (доля от суммы ставки; у этих рынков rate=0.07,
exponent=1, takerOnly). Кэш: pm_entry_cache.json (докачивается при перезапуске).
Ставка условно $10, держим до резолва.
Запуск: python entry_study.py
"""
import csv
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CANDLES = os.path.join(HERE, "candles_5m.csv")
CACHE = os.path.join(HERE, "pm_entry_cache.json")
GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB = "https://clob.polymarket.com/prices-history"
STEP = 300
STAKE = 10.0
RATE, EXPONENT = 0.07, 1  # feeSchedule рынков btc-updown-5m
LENGTHS = (3, 4, 5, 6, 7)


def load_candles():
    with open(CANDLES, newline="") as f:
        return [(int(t), float(o), float(c)) for t, o, c in csv.reader(f)]


def find_signals(candles):
    """{n: [(window_ts, direction_to_bet)]}, direction: 'Up'/'Down' (против серии)."""
    out = {n: [] for n in LENGTHS}
    for n in LENGTHS:
        for i in range(n, len(candles)):
            w = candles[i - n:i + 1]
            if any(w[k + 1][0] - w[k][0] != STEP for k in range(n)):
                continue
            dirs = [1 if c > o else (-1 if c < o else 0) for _, o, c in w[:n]]
            if 0 in dirs or len(set(dirs)) != 1:
                continue
            out[n].append((w[n][0], "Up" if -dirs[0] == 1 else "Down"))
    return out


def get_json(url, params, tries=4):
    for a in range(tries):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(2 + a * 2)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            if a == tries - 1:
                return None
            time.sleep(1.5)
    return None


def fetch_window(ts, direction):
    m = get_json(GAMMA, {"slug": f"btc-updown-5m-{ts}", "closed": "true"})
    if not m:
        m = get_json(GAMMA, {"slug": f"btc-updown-5m-{ts}"})
    if not m:
        return None
    m = m[0]
    outcomes = json.loads(m["outcomes"])
    tokens = json.loads(m["clobTokenIds"])
    prices = [float(x) for x in json.loads(m.get("outcomePrices", "[]"))]
    if len(prices) != 2 or max(prices) < 0.99:  # рынок ещё не резолвлен окончательно
        return None
    idx = outcomes.index(direction)
    h = get_json(CLOB, {"market": tokens[idx], "startTs": ts - 120, "endTs": ts + 120, "fidelity": 1})
    pts = (h or {}).get("history") or []
    before = [p for p in pts if p["t"] <= ts + 20]
    entry = before[-1]["p"] if before else (pts[0]["p"] if pts and pts[0]["t"] <= ts + 60 else None)
    return {"direction": direction, "won": prices[idx] > 0.5, "entry": entry}


def load_cache():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def fee_frac(p):
    return RATE * (p * (1 - p)) ** EXPONENT


def pnl(entry, won, slip=0.0):
    p = min(entry + slip, 0.99)
    shares = STAKE / p
    gross = shares - STAKE if won else -STAKE
    return gross - STAKE * fee_frac(p)


def main():
    signals = find_signals(load_candles())
    todo = {}
    for n in LENGTHS:
        for ts, d in signals[n]:
            todo[ts] = d
    cache = load_cache()
    pending = [(ts, d) for ts, d in todo.items() if str(ts) not in cache]
    print(f"окон с сигналом (n=3..7): {len(todo)}, уже в кэше: {len(todo) - len(pending)}, качаем: {len(pending)}", flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_window, ts, d): ts for ts, d in pending}
        for f, ts in futs.items():
            res = f.result()
            cache[str(ts)] = res if res else {"skip": True}
            done += 1
            if done % 100 == 0:
                with open(CACHE, "w", encoding="utf-8") as fh:
                    json.dump(cache, fh)
                print(f"  {done}/{len(pending)}", flush=True)
    with open(CACHE, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)

    for slip in (0.0, 0.01):
        print(f"\n=== вход по mid-цене + {slip:.2f} (проскальзывание), комиссия {RATE}*(p(1-p))^{EXPONENT}, ставка ${STAKE:.0f} ===")
        print(f"{'n':>2} {'сделок':>7} {'выигр.':>7} {'ср.вход':>8} {'безубыт.':>9} {'P&L $':>9} {'ROI':>7} {'95% ROI':>16}")
        for n in LENGTHS:
            rows = []
            for ts, d in signals[n]:
                c = cache.get(str(ts))
                if c and not c.get("skip") and c.get("entry") and 0.02 < c["entry"] < 0.98:
                    rows.append(c)
            if not rows:
                continue
            pl = [pnl(r["entry"], r["won"], slip) for r in rows]
            tot = sum(pl)
            mean = tot / len(pl)
            sd = math.sqrt(sum((x - mean) ** 2 for x in pl) / max(len(pl) - 1, 1))
            se = sd / math.sqrt(len(pl))
            wins = sum(r["won"] for r in rows)
            avg_e = sum(r["entry"] for r in rows) / len(rows)
            be = avg_e + slip + fee_frac(avg_e + slip) * (avg_e + slip)
            print(f"{n:>2} {len(rows):>7} {100*wins/len(rows):>6.1f}% {avg_e:>8.3f} {100*be:>8.1f}% {tot:>+9.1f} {100*mean/STAKE:>+6.1f}% "
                  f"{100*(mean-1.96*se)/STAKE:>+7.1f}..{100*(mean+1.96*se)/STAKE:>+6.1f}%")
    skipped = sum(1 for v in cache.values() if v.get("skip"))
    print(f"\nОкон без данных (нет рынка/цены/резолва): {skipped} из {len(cache)}")


if __name__ == "__main__":
    main()
