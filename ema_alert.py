"""XAUUSD EMA 50 / EMA 200 cross alert on the 5-minute chart -> Telegram.

Runs on GitHub Actions every 5 minutes during trading hours.
Standard library only. Secrets come from environment variables, never from this file.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SYMBOL = "XAU/USD"
INTERVAL = "5min"
STEP = timedelta(minutes=5)
FAST, SLOW = 50, 200
LOOKBACK = 60            # also check the last 6 closed candles (30 min), so a late or skipped run still catches a cross
STALE_AFTER = timedelta(minutes=20)   # newest closed candle older than this = market closed, stay quiet
BKK = timezone(timedelta(hours=7))
STATE_FILE = "state.json"
FORCE_TEST = os.environ.get("FORCE_TEST", "").lower() == "true"


class Persistent(Exception):
    """An error that will not fix itself (bad key, bad request): fail the run so GitHub emails you."""


def http_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "xauusd-ema-alert"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_candles():
    key = os.environ["TWELVEDATA_API_KEY"]
    url = "https://api.twelvedata.com/time_series?" + urllib.parse.urlencode({
        "symbol": SYMBOL, "interval": INTERVAL, "outputsize": 1000,
        "timezone": "UTC", "apikey": key})
    try:
        data = http_json(url)
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403, 404):
            raise Persistent(f"Twelve Data HTTP {e.code}")
        raise
    if data.get("status") == "error":
        code = data.get("code")
        if code in (400, 401, 403, 404):
            raise Persistent(f"Twelve Data error {code}: {data.get('message', '')[:150]}")
        raise RuntimeError(f"Twelve Data error {code}")
    rows = []
    for v in data.get("values", []):
        t = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        rows.append((t, float(v["close"])))
    rows.sort(key=lambda r: r[0])          # oldest first
    return rows


def ema(values, n):
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    out = [None] * (n - 1) + [e]
    for v in values[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def crosses(times, closes, f, s, start):
    found = []
    for i in range(max(start, 1), len(closes)):
        if f[i - 1] is None or s[i - 1] is None:
            continue
        prev, cur = f[i - 1] - s[i - 1], f[i] - s[i]
        if prev <= 0 < cur:
            found.append((times[i], "up", closes[i], f[i], s[i]))
        elif prev >= 0 > cur:
            found.append((times[i], "down", closes[i], f[i], s[i]))
    return found


def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=1)


def send_telegram(text):
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30).read()


def fmt_bkk(t):
    return t.astimezone(BKK).strftime("%a %d %b, %H:%M") + " Bangkok"


def main():
    now = datetime.now(timezone.utc)
    rows = fetch_candles()
    rows = [r for r in rows if r[0] + STEP <= now]          # keep only fully closed candles
    if len(rows) < SLOW + 50:
        print(f"Only {len(rows)} closed candles; need more. Skipping.")
        return
    times, closes = [r[0] for r in rows], [r[1] for r in rows]
    newest_close = times[-1] + STEP
    if now - newest_close > STALE_AFTER and not FORCE_TEST:
        print(f"Newest closed candle ended {fmt_bkk(newest_close)}; market looks closed. Quiet.")
        return

    f, s = ema(closes, FAST), ema(closes, SLOW)

    if FORCE_TEST:
        last = crosses(times, closes, f, s, 1)
        rel = "ABOVE" if f[-1] > s[-1] else "BELOW"
        lastx = (f"{last[-1][1].upper()} at {fmt_bkk(last[-1][0] + STEP)}" if last else "none in loaded history")
        send_telegram(
            "TEST - XAUUSD 5m EMA alert is working\n"
            f"Last closed candle: {fmt_bkk(newest_close)}\n"
            f"Close: {closes[-1]:.2f}\nEMA 50: {f[-1]:.2f}  |  EMA 200: {s[-1]:.2f}\n"
            f"EMA 50 is currently {rel} EMA 200\nMost recent cross in loaded data: {lastx}\n"
            f"Candles used: {len(closes)}")
        print("Test message sent.")
        return

    state = load_state()
    last_done = state.get("last_cross_utc")
    new = [e for e in crosses(times, closes, f, s, len(closes) - LOOKBACK)
           if last_done is None or e[0].isoformat() > last_done]
    if not new:
        print(f"No new cross. EMA50 {f[-1]:.2f} / EMA200 {s[-1]:.2f}. Close {closes[-1]:.2f}.")
        return

    for t, direction, price, e50, e200 in new:
        close_time = t + STEP
        late = now - close_time
        word = "ABOVE" if direction == "up" else "BELOW"
        msg = (f"XAUUSD 5m: EMA 50 crossed {word} EMA 200\n"
               f"Candle closed: {fmt_bkk(close_time)}\n"
               f"Close price: {price:.2f}\n"
               f"EMA 50: {e50:.2f}  |  EMA 200: {e200:.2f}\n")
        if late > timedelta(minutes=10):
            msg += f"(Late alert: the cross was {int(late.total_seconds() // 60)} min ago)\n"
        msg += "Data: Twelve Data, may differ slightly from your chart. Not financial advice."
        send_telegram(msg)
        state["last_cross_utc"] = t.isoformat()           # saved after each successful send
        save_state(state)
        print("Alert sent:", direction, t.isoformat())


if __name__ == "__main__":
    try:
        main()
    except Persistent as e:
        print("FAILED (will not fix itself):", e, file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print("Temporary problem, will try again next run:", e, file=sys.stderr)
        sys.exit(0 if "Twelve Data" in str(e) or isinstance(e, (urllib.error.URLError, TimeoutError)) else 1)
