#!/usr/bin/env python3
"""
بوت إشارات الدخول
يسحب شموع العملات من Binance، يطبّق الاستراتيجيات، ويرسل تنبيه تيليجرام عند كل دخول جديد.
لا يحتاج أي مكتبات خارجية (Python فقط).
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ───────────────────────── الإعدادات ─────────────────────────
INTERVAL = os.environ.get("INTERVAL", "15m")          # فريم الشموع
CANDLES = 1000                                        # عدد الشموع التي تُسحب لكل عملة
LOOKBACK_MIN = int(os.environ.get("LOOKBACK_MIN", "90"))  # يرسل الإشارات التي ظهرت خلال آخر X دقيقة
COINS_FILE = "coins.txt"
STATE_FILE = "state.json"                             # يحفظ الإشارات المُرسلة حتى لا تتكرر
STATE_KEEP_DAYS = 3
TZ = timezone(timedelta(hours=3))                     # توقيت إسطنبول للرسائل

BINANCE_HOSTS = [
    "https://data-api.binance.vision",   # رابط البيانات العام (يعمل من سيرفرات GitHub)
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]


# ───────────────────────── المؤشرات ─────────────────────────
def ema(values, length):
    """EMA مطابق لـ ta.ema في Pine: أول قيمة = SMA ثم معامل 2/(n+1)."""
    out = [None] * len(values)
    if len(values) < length:
        return out
    alpha = 2.0 / (length + 1)
    prev = sum(values[:length]) / length
    out[length - 1] = prev
    for i in range(length, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def gt(a, b):
    """مقارنة a > b بأسلوب Pine: أي قيمة na تعطي false."""
    return a is not None and b is not None and a > b


# ───────────────────────── الاستراتيجيات ─────────────────────────
def strategy_mervan(c):
    """
    استراتيجية MERVAN — منقولة من Pine كما هي.
    دخول: الترتيب EMA5 > EMA10 > EMA20 تحقق الآن ولم يكن متحققاً في الشمعة السابقة، ولا توجد صفقة مفتوحة.
    خروج (لمعرفة متى تكون الصفقة مفتوحة فقط): EMA5 يكسر EMA10 للأسفل.
    تُرجع أرقام الشموع التي حدث فيها دخول.
    """
    closes = c["close"]
    blue, red, yellow = ema(closes, 5), ema(closes, 10), ema(closes, 20)
    in_position = False
    entries = []
    for i in range(1, len(closes)):
        ordered_now = gt(blue[i], red[i]) and gt(red[i], yellow[i])
        ordered_before = gt(blue[i - 1], red[i - 1]) and gt(red[i - 1], yellow[i - 1])
        # ta.crossunder(blue, red): blue < red الآن و blue[1] >= red[1]
        cross_under = (
            None not in (blue[i], red[i], blue[i - 1], red[i - 1])
            and blue[i] < red[i]
            and blue[i - 1] >= red[i - 1]
        )
        buy = (not in_position) and ordered_now and not ordered_before
        sell = in_position and cross_under
        if buy:
            entries.append(i)
            in_position = True
        elif sell:
            in_position = False
    return entries


def rma(values, length):
    """ta.rma في Pine: أول قيمة = SMA ثم معامل 1/n."""
    out = [None] * len(values)
    start = next((i for i, v in enumerate(values) if v is not None), None)
    if start is None or len(values) - start < length:
        return out
    prev = sum(values[start:start + length]) / length
    out[start + length - 1] = prev
    alpha = 1.0 / length
    for i in range(start + length, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def atr(high, low, close, length):
    """ta.atr في Pine = rma(true range) — أول شمعة TR = high - low."""
    tr = [high[0] - low[0]]
    for i in range(1, len(close)):
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    return rma(tr, length)


def supertrend_trend(c, atr_period=10, mult=3.0):
    """يحسب trend للسوبرترند بنفس معادلة سكربت Pine حرفياً (changeATR = true)."""
    high, low, close = c["high"], c["low"], c["close"]
    n = len(close)
    a = atr(high, low, close, atr_period)
    trend = [1] * n
    up_prev = dn_prev = None          # up[1] و dn[1]
    for i in range(n):
        hl2 = (high[i] + low[i]) / 2
        up = hl2 - mult * a[i] if a[i] is not None else None
        dn = hl2 + mult * a[i] if a[i] is not None else None
        up1 = up_prev if up_prev is not None else up      # nz(up[1], up)
        dn1 = dn_prev if dn_prev is not None else dn      # nz(dn[1], dn)
        if i > 0 and gt(close[i - 1], up1):
            up = max(up, up1)
        if i > 0 and up1 is not None and dn1 is not None and close[i - 1] < dn1:
            dn = min(dn, dn1)
        t_prev = trend[i - 1] if i > 0 else 1
        if t_prev == -1 and gt(close[i], dn1):
            trend[i] = 1
        elif t_prev == 1 and up1 is not None and close[i] < up1:
            trend[i] = -1
        else:
            trend[i] = t_prev
        up_prev, dn_prev = up, dn
    return trend


def strategy_supertrend(c):
    """
    استراتيجية «مروان 5 — سوبرترند 15د» — الدخول فقط.
    دخول: الاتجاه انقلب من -1 إلى 1 (flipUp) ولا توجد صفقة مفتوحة.
    ملاحظة: في السكربت الأصلي كل انقلاب للأسفل يغلق الصفقة في نفس الشمعة (إغلاق تحت الخط الأخضر)،
    لذلك لا تكون هناك صفقة مفتوحة أبداً عند انقلاب جديد للأعلى → كل flipUp = دخول.
    """
    trend = supertrend_trend(c)
    return [i for i in range(1, len(trend)) if trend[i] == 1 and trend[i - 1] == -1]


# اسم الاستراتيجية ← الدالة
STRATEGIES = {
    "MERVAN (EMA 5/10/20)": strategy_mervan,
    "مروان 5 (سوبرترند)": strategy_supertrend,
}


# ───────────────────────── Binance ─────────────────────────
def http_json(url, data=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "signals-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_closed_candles(symbol):
    """يرجع الشموع المغلقة فقط (يحذف الشمعة الحالية التي لم تُغلق)."""
    query = urllib.parse.urlencode({"symbol": symbol, "interval": INTERVAL, "limit": CANDLES})
    last_err = None
    for host in BINANCE_HOSTS:
        try:
            rows = http_json(f"{host}/api/v3/klines?{query}")
            now_ms = int(time.time() * 1000)
            return [
                {"open_time": int(r[0]), "close_time": int(r[6]), "high": float(r[2]), "low": float(r[3]),
                 "close": float(r[4]), "close_str": r[4]}
                for r in rows
                if int(r[6]) < now_ms
            ]
        except urllib.error.HTTPError as e:
            if e.code == 400:  # رمز عملة غير صحيح — لا فائدة من تجربة سيرفر آخر
                raise ValueError(f"رمز غير صحيح على Binance: {symbol}")
            last_err = e
        except Exception as e:
            last_err = e
    raise ConnectionError(f"تعذر جلب {symbol}: {last_err}")


def columns(candles):
    return {k: [c[k] for c in candles] for k in ("high", "low", "close")}


# ───────────────────────── تيليجرام ─────────────────────────
def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[تحذير] TELEGRAM_TOKEN أو TELEGRAM_CHAT_ID غير موجود — الرسالة لم تُرسل:\n" + text)
        return False
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    for attempt in range(3):
        try:
            http_json(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
            time.sleep(1.1)  # حد تيليجرام: رسالة واحدة تقريباً كل ثانية
            return True
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="ignore")
            if e.code == 429:
                time.sleep(5)
                continue
            print(f"[خطأ تيليجرام] {e.code}: {body}")
            return False
        except Exception as e:
            print(f"[خطأ تيليجرام] {e}")
            time.sleep(2)
    return False


def fmt_price(s):
    return s.rstrip("0").rstrip(".") if "." in s else s


def fmt_time(ms):
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d %H:%M")


def signal_message(strategy, symbol, candle):
    chart = f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}"
    return (
        f"🟢 <b>دخول {symbol}</b>\n"
        f"الاستراتيجية: {strategy}\n"
        f"السعر: {fmt_price(candle['close_str'])}\n"
        f"إغلاق الشمعة: {fmt_time(candle['close_time'] + 1)} ({INTERVAL})\n"
        f'<a href="{chart}">فتح الشارت</a>'
    )


# ───────────────────────── الحالة ─────────────────────────
def load_coins():
    coins = []
    with open(COINS_FILE, encoding="utf-8") as f:
        for line in f:
            s = line.split("#")[0].strip().upper()
            if s:
                coins.append(s)
    return list(dict.fromkeys(coins))


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    cutoff = int((time.time() - STATE_KEEP_DAYS * 86400) * 1000)
    state = {k: v for k, v in state.items() if v >= cutoff}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=0, sort_keys=True)


# ───────────────────────── التشغيل ─────────────────────────
def run(lookback_min=None, exit_on_fail=True):
    coins = load_coins()
    state = load_state()
    now_ms = int(time.time() * 1000)
    window_start = now_ms - (lookback_min or LOOKBACK_MIN) * 60 * 1000
    errors, sent = [], 0

    for symbol, candles in fetch_all(coins):
        if isinstance(candles, Exception):
            errors.append(str(candles))
            print(f"[خطأ] {candles}")
            continue
        cols = columns(candles)
        for name, fn in STRATEGIES.items():
            for i in fn(cols):
                c = candles[i]
                if c["close_time"] < window_start:
                    continue
                key = f"{name}|{symbol}|{c['open_time']}"
                if key in state:
                    continue
                print(f"[إشارة] {name} {symbol} @ {fmt_time(c['close_time'] + 1)}")
                if send_telegram(signal_message(name, symbol, c)):
                    state[key] = c["open_time"]
                    sent += 1

    save_state(state)
    print(f"[{fmt_time(now_ms)}] تم فحص {len(coins)} عملة — أُرسلت {sent} إشارة — أخطاء: {len(errors)}", flush=True)
    if exit_on_fail and errors and len(errors) == len(coins):
        sys.exit(1)  # كل العملات فشلت → يظهر التشغيل كفاشل في GitHub
    return sent


def git_save_state():
    """يحفظ state.json في المستودع حتى يعرف التشغيل التالي ما أُرسل (يمنع التكرار)."""
    import subprocess

    def sh(*cmd):
        return subprocess.run(cmd, capture_output=True, text=True)

    if not sh("git", "status", "--porcelain", STATE_FILE).stdout.strip():
        return
    sh("git", "add", STATE_FILE)
    sh("git", "commit", "-m", "update state [skip ci]")
    for _ in range(3):
        sh("git", "pull", "--rebase", "-X", "theirs")
        if sh("git", "push").returncode == 0:
            return
        time.sleep(3)
    print("[تحذير] تعذر حفظ state.json في GitHub")


def loop():
    """
    يبقى شغالاً ويفحص لحظة إغلاق كل شمعة 15 دقيقة (+ بضع ثوانٍ).
    يتوقف بعد LOOP_MAX_MIN دقيقة مباشرة بعد فحص، فيبدأ التشغيل التالي (المنتظر في الطابور) ويكمل.
    """
    started = time.time()
    max_sec = int(os.environ.get("LOOP_MAX_MIN", "335")) * 60
    step = 15 * 60
    delay = 4  # ثوانٍ بعد الإغلاق حتى تثبت الشمعة عند Binance

    # فحص فوري عند البدء لالتقاط أي إشارة وقعت أثناء تسليم التشغيل
    boundary = (int(time.time()) // step) * step  # آخر إغلاق شمعة شمله هذا الفحص
    run(lookback_min=30, exit_on_fail=False)
    git_save_state()

    while True:
        # الإغلاق التالي بعد آخر إغلاق تم فحصه — إذا فات وقته (الفحص السابق طال) نفحص فوراً
        boundary += step
        if time.time() - boundary > step:      # تأخرنا أكثر من شمعة كاملة → نقفز للإغلاق الحالي
            boundary = (int(time.time()) // step) * step
        time.sleep(max(0, boundary + delay - time.time()))
        try:
            run(lookback_min=30, exit_on_fail=False)
            git_save_state()
        except Exception as e:
            print(f"[خطأ في الفحص] {e}", flush=True)
        if time.time() - started > max_sec:
            print("انتهت مدة هذا التشغيل — التشغيل التالي يكمل.")
            return


def fetch_all(coins):
    """يجلب كل العملات بالتوازي (أسرع)، ويرجعها بنفس ترتيب القائمة."""
    from concurrent.futures import ThreadPoolExecutor

    def one(sym):
        try:
            return fetch_closed_candles(sym)
        except Exception as e:
            return e

    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(zip(coins, ex.map(one, coins)))


def test():
    """يرسل رسالة تأكيد فيها آخر إشارة دخول لكل عملة — للمقارنة مع TradingView."""
    coins = load_coins()
    data = fetch_all(coins)
    bad = [str(c) for _, c in data if isinstance(c, Exception)]
    lines = [f"✅ <b>البوت يعمل</b> — فريم {INTERVAL}",
             f"عدد العملات: {len(coins)} (تعمل: {len(coins) - len(bad)})", ""]
    if bad:
        lines += ["⚠️ <b>عملات فيها مشكلة:</b>"] + [f"• {b}" for b in bad] + [""]
    for name, fn in STRATEGIES.items():
        lines.append(f"<b>آخر دخول — {name}:</b>")
        for symbol, candles in data:
            if isinstance(candles, Exception):
                continue
            entries = fn(columns(candles))
            if entries:
                c = candles[entries[-1]]
                lines.append(f"• {symbol}: {fmt_time(c['close_time'] + 1)} @ {fmt_price(c['close_str'])}")
            else:
                lines.append(f"• {symbol}: لا يوجد")
        lines.append("")
    text = "\n".join(lines)
    print(text)
    # تيليجرام يقبل 4096 حرفاً كحد أقصى للرسالة
    for i in range(0, len(text), 4000):
        send_telegram(text[i:i + 4000])


if __name__ == "__main__":
    mode = os.environ.get("MODE", "run").strip().lower()
    if mode == "test":
        test()
    elif mode == "loop":
        loop()
    else:
        run()
