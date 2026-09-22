import requests, time, json, os
from datetime import datetime, timezone

CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CLOB_URL = "https://clob.polymarket.com"

SIGNAL_N = 4
STAKE = 10.0
RESOLVE_BUFFER = 60  # seconds after window close before trusting the resolved price
LOG_FILE = os.environ.get("LOG_FILE", "paper_trades.jsonl")

_PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
PROXIES = {"http": _PROXY, "https": _PROXY} if _PROXY else None


def load_records():
    records = []
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return records


def save_records(records):
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def get_candles(limit=20):
    r = requests.get(CANDLES_URL, params={"granularity": 300}, proxies=PROXIES, timeout=15)
    try:
        data = r.json()
    except ValueError:
        raise ValueError(f"non-JSON from coinbase (status {r.status_code}): {r.text[:300]!r}")
    if not data:
        raise ValueError(f"no candle data: {data}")
    # each row: [time, low, high, open, close, volume], most recent first
    rows = sorted(data, key=lambda x: int(x[0]))[-limit:]
    return [(int(x[0]), float(x[3]), float(x[4])) for x in rows]


def compute_signal(candles, n=SIGNAL_N):
    closed = candles[:-1]
    if len(closed) < n:
        return None
    last_n = closed[-n:]
    dirs = [1 if c > o else (-1 if c < o else 0) for _, o, c in last_n]
    if 0 in dirs or len(set(dirs)) != 1:
        return None
    return -dirs[0]


def window_ts(ts=None):
    ts = ts if ts is not None else time.time()
    return int(ts - (ts % 300))


def get_market(win_ts):
    slug = f"btc-updown-5m-{win_ts}"
    r = requests.get(GAMMA_URL, params={"slug": slug}, proxies=PROXIES, timeout=15)
    try:
        data = r.json()
    except ValueError:
        raise ValueError(f"non-JSON from polymarket gamma (status {r.status_code}): {r.text[:300]!r}")
    return data[0] if data else None


def get_token_price(token_id):
    r = requests.get(f"{CLOB_URL}/price", params={"token_id": token_id, "side": "buy"}, proxies=PROXIES, timeout=15)
    try:
        return float(r.json().get("price", 0.5))
    except ValueError:
        raise ValueError(f"non-JSON from polymarket clob (status {r.status_code}): {r.text[:300]!r}")


def enter_trade(records):
    now_win = window_ts()
    if any(r["window_ts"] == now_win for r in records):
        print(f"window {now_win} already recorded, skip entry")
        return

    candles = get_candles(limit=SIGNAL_N + 5)
    signal = compute_signal(candles)
    if signal is None:
        print(f"{datetime.now(timezone.utc)}: no signal for window {now_win}")
        return

    direction = "Up" if signal == 1 else "Down"
    market = get_market(now_win)
    if not market:
        print(f"no market for window {now_win}, skipping")
        return

    tokens = json.loads(market["clobTokenIds"])
    outcomes = json.loads(market["outcomes"])
    idx = outcomes.index(direction)
    token_id = tokens[idx]
    entry_price = get_token_price(token_id)

    trade = {
        "window_ts": now_win,
        "signal_n": SIGNAL_N,
        "direction": direction,
        "entry_price": entry_price,
        "slug": market.get("slug"),
        "condition_id": market.get("conditionId"),
        "token_id": token_id,
        "outcome_idx": idx,
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "won": None,
    }
    records.append(trade)
    print(f"entered {direction} @ {entry_price:.3f} (window {now_win})")


def resolve_pending(records):
    now = time.time()
    for r in records:
        if r.get("won") is not None:
            continue
        if now < r["window_ts"] + 300 + RESOLVE_BUFFER:
            continue
        try:
            resolved = get_market(r["window_ts"])
            outcome_prices = [float(x) for x in json.loads(resolved.get("outcomePrices", '["0.5","0.5"]'))]
        except Exception as e:
            print(f"resolve failed for window {r['window_ts']}: {e}")
            continue
        idx = r["outcome_idx"]
        won = outcome_prices[idx] > 0.5
        payout = STAKE / r["entry_price"] if won else 0.0
        pnl = round(payout - STAKE, 4) if won else -STAKE
        r.update({
            "won": won,
            "outcome_prices": outcome_prices,
            "pnl": pnl,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        })
        print(f"resolved window {r['window_ts']}: {'WIN' if won else 'LOSS'}, pnl ${pnl:.2f}")


def main():
    records = load_records()
    resolve_pending(records)
    try:
        enter_trade(records)
    except Exception as e:
        print("entry error:", e)
    save_records(records)


if __name__ == "__main__":
    main()
