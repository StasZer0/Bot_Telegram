# app.py — Bot Telegram + Report (Stooq/FRED) in un solo file
# Requisiti: python-telegram-bot>=21, requests, numpy, pandas, matplotlib

import os, io, math, json, time, tempfile, threading
import datetime as dtmod
from datetime import datetime
from typing import List, Tuple
from http.server import HTTPServer, BaseHTTPRequestHandler

import numpy as np
import pandas as pd
pd.options.mode.copy_on_write = True

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import requests

from html import escape as html_escape

from telegram import Update, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ================== CONFIG / ENV ==================
# Token per il BOT (obbligatorio per la parte interattiva)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_TOKEN")
if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.startswith("INSERISCI"):
    raise RuntimeError("❌ TELEGRAM_BOT_TOKEN mancante. Impostalo nelle variabili d'ambiente Render.")

# Chat ID opzionale (serve solo per invii 'push' non interattivi)
TELEGRAM_CHATID = os.getenv("TG_CHATID") or os.getenv("TELEGRAM_CHATID")

# Fuso orario
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Bangkok"))
except Exception:
    from datetime import timezone, timedelta
    TZ = timezone(timedelta(hours=7))

UA = "Mozilla/5.0 (compatible; OilBot/1.0; +https://stooq.com)"

# Stooq (primaria)
STQ_SYMBOLS = {
    "WTI": "cl.f",     # Light Crude Oil continuous
    "BRENT": "brn.f",  # Brent continuous
}

# FRED (fallback: close giornaliero)
FRED_IDS = {
    "WTI": "DCOILWTICO",
    "BRENT": "DCOILBRENTEU",
}

SOURCE_ORDER = ["stooq", "fred"]

CHART_WINDOW_DAYS = 14
FETCH_DAYS = 60  # >= 45 per 30d; metto 60 per margine

# ================== HEALTH SERVER (Render) ==================
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404); self.end_headers()

def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    httpd = HTTPServer(("0.0.0.0", port), _Health)
    httpd.serve_forever()

# ================== HTTP helper ==================
from urllib.request import Request, urlopen
def http_get_bytes(url: str, timeout: int = 30, diag: bool = False) -> bytes:
    if diag:
        print(f"[GET] {url}")
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()

def robust_read_csv(data: bytes) -> pd.DataFrame:
    txt = data.decode("utf-8", errors="ignore")
    return pd.read_csv(io.StringIO(txt))

# ================== Indicatori ==================
def rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi

# ================== Fetchers ==================
def fetch_stooq(tkr: str, days: int, diag: bool = False) -> pd.DataFrame | None:
    sym = STQ_SYMBOLS.get(tkr.upper())
    if not sym:
        return None
    url = f"https://stooq.com/q/l/?s={sym}&c={max(days, FETCH_DAYS)}&f=sd2t2ohlcv&h&e=csv"
    try:
        b = http_get_bytes(url, diag=diag)
        df = robust_read_csv(b)
        cols = {c.lower(): c for c in df.columns}
        must = all(x in cols for x in ["date", "open", "high", "low", "close"])
        if not must:
            if diag: print("[STQ] formato CSV inatteso:", df.columns.tolist())
            return None
        df = df.rename(columns={
            cols["date"]: "Date",
            cols["open"]: "Open",
            cols["high"]: "High",
            cols["low"]: "Low",
            cols["close"]: "Close"
        })
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.dropna(subset=["Date"])
        for c in ["Open", "High", "Low", "Close"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["Close"]).sort_values("Date").reset_index(drop=True)
        return df
    except Exception as e:
        if diag: print("[STQ] errore:", repr(e))
        return None

def fetch_fred_close_series(series_id: str, diag: bool = False) -> pd.DataFrame | None:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        b = http_get_bytes(url, diag=diag)
        df = robust_read_csv(b)
        lower = {c.lower(): c for c in df.columns}
        date_col = lower.get("date") or lower.get("observation_date")
        val_col = lower.get("value") or lower.get(series_id.lower())
        if not date_col or not val_col:
            if diag: print("[FRED] header inatteso:", df.columns.tolist())
            return None
        df = df.rename(columns={date_col: "Date", val_col: "Close"})
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
        df = df.dropna(subset=["Date", "Close"]).sort_values("Date").reset_index(drop=True)
        # pseudo-OHLC
        df["Open"] = df["Close"].shift(1).fillna(df["Close"])
        df["High"] = df[["Open", "Close"]].max(axis=1)
        df["Low"]  = df[["Open", "Close"]].min(axis=1)
        return df[["Date","Open","High","Low","Close"]]
    except Exception as e:
        if diag: print("[FRED] errore:", repr(e))
        return None

def fetch_prices(tkr: str, days: int, diag: bool = False) -> dict:
    if "stooq" in SOURCE_ORDER:
        stq = fetch_stooq(tkr, days=max(days, FETCH_DAYS), diag=diag)
        if stq is not None and len(stq) > 0:
            return {"source": "stooq", "df": stq, "ohlc": stq.tail(days).copy()}
    if "fred" in SOURCE_ORDER:
        fred_id = FRED_IDS.get(tkr.upper())
        fred = fetch_fred_close_series(fred_id, diag=diag) if fred_id else None
        if fred is not None and len(fred) > 0:
            return {"source": "fred", "df": fred, "ohlc": fred.tail(days).copy()}
    return {"source": None, "df": None, "ohlc": None}

# ================== Utils di formattazione ==================
def fmt_pct(x: float | None) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "n/a"
    return f"{x:+.2f}%"

def fmt_price(x: float | None) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "n/a"
    return f"{x:.2f} USD"

# ================== Grafici ==================
def draw_candles(ax, dates, opens, highs, lows, closes, width=0.6):
    for d, o, h, l, c in zip(dates, opens, highs, lows, closes):
        color = "tab:green" if c >= o else "tab:red"
        ax.vlines(d, l, h, linewidth=1, color=color)
        top = max(o, c); bottom = min(o, c)
        ax.add_patch(plt.Rectangle((d - width/2, bottom),
                                   width, max(top - bottom, 0.001),
                                   facecolor=color, edgecolor=color, linewidth=1, alpha=0.8))

def make_chart_png(title: str, ohlc: pd.DataFrame, outpath: str):
    period = 14
    o = ohlc.copy().sort_values("Date")
    d_show = o.tail(CHART_WINDOW_DAYS).dropna(subset=["Open","High","Low","Close"])
    if d_show.empty:
        fig = plt.figure(figsize=(7.5, 6), dpi=150, constrained_layout=True)
        ax = fig.add_subplot(2,1,1); ax.set_title(f"{title} — ultime 2 settimane (nessun dato)")
        fig.savefig(outpath, bbox_inches="tight"); plt.close(fig); return

    d_buf = o.tail(CHART_WINDOW_DAYS + period).dropna(subset=["Close"])
    rsi_full = rsi_series(d_buf["Close"], period=period)
    rsi_to_plot = rsi_full.tail(len(d_show)).reset_index(drop=True)

    x = mdates.date2num(d_show["Date"].to_numpy(dtype="datetime64[ns]"))

    fig = plt.figure(figsize=(7.5, 6), dpi=150, constrained_layout=True)
    gs = fig.add_gridspec(2, 1, height_ratios=[3, 1])
    ax = fig.add_subplot(gs[0]); ax_rsi = fig.add_subplot(gs[1], sharex=ax)
    ax.xaxis_date(); ax_rsi.xaxis_date()

    draw_candles(ax, x, d_show["Open"].values, d_show["High"].values, d_show["Low"].values, d_show["Close"].values)
    ax.set_ylabel("USD/barile"); ax.set_title(f"{title} — ultime 2 settimane", fontsize=10)

    if rsi_to_plot.notna().any():
        ax_rsi.plot(d_show["Date"], rsi_to_plot, linewidth=1.2)
    else:
        ax_rsi.text(0.5, 0.5, "RSI(14) non disponibile", ha="center", va="center", transform=ax_rsi.transAxes)

    ax_rsi.axhline(70, linestyle="--", linewidth=0.8, color="gray")
    ax_rsi.axhline(30, linestyle="--", linewidth=0.8, color="gray")
    ax_rsi.set_ylabel("RSI(14)"); ax_rsi.set_ylim(0, 100)

    fmt = mdates.DateFormatter("%m-%d")
    ax.xaxis.set_major_formatter(fmt); ax_rsi.xaxis.set_major_formatter(fmt)
    ax.grid(True, linewidth=0.3, alpha=0.5); ax_rsi.grid(True, linewidth=0.3, alpha=0.5)

    fig.savefig(outpath, bbox_inches="tight"); plt.close(fig)

def make_daily_line_chart_png(title: str, df: pd.DataFrame, outpath: str, days: int = 60):
    d = df.copy().sort_values("Date").dropna(subset=["Close"]).tail(days)
    if d.empty:
        fig = plt.figure(figsize=(7.5, 4.2), dpi=150, constrained_layout=True)
        ax = fig.add_subplot(1,1,1); ax.set_title(f"{title} — grafico giornaliero (nessun dato)")
        fig.savefig(outpath, bbox_inches="tight"); plt.close(fig); return
    sma20 = d["Close"].rolling(20).mean(); sma50 = d["Close"].rolling(50).mean()
    fig = plt.figure(figsize=(7.5, 4.2), dpi=150, constrained_layout=True); ax = fig.add_subplot(1,1,1)
    ax.plot(d["Date"], d["Close"], linewidth=1.5, label="Close")
    ax.plot(d["Date"], sma20, linewidth=1.1, label="SMA 20")
    ax.plot(d["Date"], sma50, linewidth=1.1, label="SMA 50")
    ax.set_title(f"{title} — grafico giornaliero (ultimi {len(d)}g)"); ax.set_ylabel("USD/barile")
    ax.grid(True, linewidth=0.3, alpha=0.5); ax.legend(loc="best", fontsize=8)
    fmt = mdates.DateFormatter("%m-%d"); ax.xaxis.set_major_formatter(fmt)
    fig.savefig(outpath, bbox_inches="tight"); plt.close(fig)

def make_daily_table_png(title: str, df: pd.DataFrame, outpath: str, rows: int = 14):
    d = df.copy().sort_values("Date").dropna(subset=["Open","High","Low","Close"]).tail(rows)
    fig = plt.figure(figsize=(7.5, 3.6), dpi=150, constrained_layout=True); ax = fig.add_subplot(1,1,1); ax.axis("off")
    if d.empty:
        ax.set_title(f"{title} — tabella giornaliera (nessun dato)")
        fig.savefig(outpath, bbox_inches="tight"); plt.close(fig); return
    tab = [[r["Date"].strftime("%Y-%m-%d"),
            f"{r['Open']:.2f}", f"{r['High']:.2f}", f"{r['Low']:.2f}", f"{r['Close']:.2f}"] for _, r in d.iterrows()]
    ax.set_title(f"{title} — ultimi {len(tab)} giorni (OHLC)", pad=10)
    the_table = ax.table(cellText=tab, colLabels=["Date","Open","High","Low","Close"], loc="center")
    the_table.auto_set_font_size(False); the_table.set_fontsize(8); the_table.scale(1, 1.25)
    fig.savefig(outpath, bbox_inches="tight"); plt.close(fig)

# ================== Blocchi testuali ==================
def fmt_pct_val(close: pd.Series, n: int) -> float | None:
    if len(close) > n:
        return float((close.iloc[-1] / close.iloc[-n-1] - 1.0) * 100.0)
    return None

def build_summary_block(name: str, res: dict) -> tuple[str, str]:
    df = res.get("df"); src = res.get("source"); name_upper = name.upper()
    name_link_html = {"WTI": '<a href="https://stooq.com/q/?s=cl.f">WTI</a>',
                      "BRENT": '<a href="https://stooq.com/q/?s=brn.f">Brent</a>'}
    title_html = name_link_html.get(name_upper, html_escape(name))
    if df is None or len(df) == 0:
        html = (f"<b>{title_html} ({'CL-F' if name_upper=='WTI' else 'BZ-F'} proxy {src or 'N/A'})</b>\n"
                f"⚠️ Dati non disponibili.\n")
        return html, (src or "n/a")
    df = df.copy().sort_values("Date").reset_index(drop=True)
    close = df["Close"]

    # se lo storico è corto, integra FRED per indicatori
    if len(close) < 40:
        fred_id = FRED_IDS.get(name_upper)
        fred_df = fetch_fred_close_series(fred_id) if fred_id else None
        if fred_df is not None and len(fred_df) > 0:
            extra = fred_df[["Date", "Close"]].rename(columns={"Close": "Close_fred"})
            df = df.merge(extra, on="Date", how="outer").sort_values("Date")
            df["Close"] = df["Close"].fillna(df["Close_fred"])
            df = df.drop(columns=["Close_fred"]).reset_index(drop=True)
            close = df["Close"]

    d1 = fmt_pct_val(close, 1); d7 = fmt_pct_val(close, 7); d30 = fmt_pct_val(close, 30)
    sma20  = close.rolling(20).mean().iloc[-1] if len(close) >= 20 else np.nan
    sma50  = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else np.nan
    sma200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else np.nan
    rsi_last = rsi_series(close, 14).iloc[-1] if len(close) >= 15 else np.nan

    price = float(close.iloc[-1])
    sma_line = f"SMA 20: {fmt_price(sma20)} | 50: {fmt_price(sma50)} | 200: {fmt_price(sma200)}"

    html = (
        f"<b>{title_html} ({'CL-F' if name_upper=='WTI' else 'BZ-F'} proxy {src})</b>\n"
        f"💵 Prezzo: {fmt_price(price)}\n"
        f"📉 1d: {fmt_pct(d1)}  •  7d: {fmt_pct(d7)}  •  30d: {fmt_pct(d30)}\n"
        f"📊 {html_escape(sma_line)}\n"
        f"📈 RSI(14): {('%.2f' % rsi_last) if not pd.isna(rsi_last) else 'n/a'}"
    )
    return html, (src or "n/a")

def build_text_summary(res_wti: dict, res_brent: dict) -> str:
    now = datetime.now(TZ)
    header = f"🛢️ <b>Oil update</b> – {now:%Y-%m-%d %H:%M} ({TZ.key if hasattr(TZ,'key') else 'local'})"
    wti_block, _   = build_summary_block("WTI", res_wti)
    brnt_block, _  = build_summary_block("BRENT", res_brent)
    body = (header + "\n\n" + wti_block + "\n\n" + brnt_block +
            "\n\n⚠️ Fonte primaria: Stooq; fallback: FRED (St. Louis Fed). Dati indicativi, non consulenza finanziaria.")
    return body

# ================== INVIO TELEGRAM PUSH (opzionale) ==================
def tg_call(method: str, data=None, files=None, timeout=20, retries=3, backoff=1.5, diag=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    last_err = None
    for i in range(retries):
        try:
            r = requests.post(url, data=data, files=files, timeout=timeout)
            if r.ok:
                js = r.json()
                if js.get("ok"): return js
                last_err = js
            else:
                last_err = r.text
        except Exception as e:
            last_err = str(e)
        if diag: print(f"[TG] retry {i+1}/{retries} errore: {last_err}")
        time.sleep(backoff**i)
    raise RuntimeError(f"Telegram {method} failed: {last_err}")

def send_telegram_html(html: str, disable_preview: bool = True, diag: bool = False):
    if not TELEGRAM_CHATID:
        raise RuntimeError("TG_CHATID mancante: impostalo se vuoi invii push non interattivi.")
    data = {"chat_id": TELEGRAM_CHATID, "text": html, "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview}
    return tg_call("sendMessage", data=data, diag=diag)

def send_telegram_photo(path: str, caption_html: str = None, diag: bool = False):
    if not TELEGRAM_CHATID:
        raise RuntimeError("TG_CHATID mancante: impostalo se vuoi invii push non interattivi.")
    with open(path, "rb") as f:
        files = {"photo": f}
        data = {"chat_id": TELEGRAM_CHATID}
        if caption_html:
            data["caption"] = caption_html; data["parse_mode"] = "HTML"
        return tg_call("sendPhoto", data=data, files=files, diag=diag)

def send_telegram_album(items, diag=False):
    if not TELEGRAM_CHATID:
        raise RuntimeError("TG_CHATID mancante: impostalo se vuoi invii push non interattivi.")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup"
    files = {}; media = []
    for i, (path, caption) in enumerate(items[:10]):  # Telegram max 10
        key = f"photo{i}"
        files[key] = (os.path.basename(path), open(path, "rb"), "image/png")
        media.append({"type": "photo", "media": f"attach://{key}",
                      "caption": caption or "", "parse_mode": "HTML"})
    r = requests.post(url, data={"chat_id": TELEGRAM_CHATID, "media": json.dumps(media)}, files=files, timeout=30)
    for f in files.values():
        try: f[1].close()
        except Exception: pass
    r.raise_for_status();  return r.json()

# ================== HANDLERS BOT ==================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "Ciao! Comandi disponibili:\n"
        "• /wti – ultimo WTI\n"
        "• /brent – ultimo Brent\n"
        "• /oil – riepilogo WTI+Brent\n"
        "• /report – riepilogo + immagini (ultimi 60g)\n"
        "Puoi anche scrivere: wti, brent, oil."
    )

async def wti_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    res_wti = fetch_prices("WTI", days=max(FETCH_DAYS, CHART_WINDOW_DAYS), diag=False)
    block, _ = build_summary_block("WTI", res_wti)
    await update.message.reply_html(block)

async def brent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    res_b = fetch_prices("BRENT", days=max(FETCH_DAYS, CHART_WINDOW_DAYS), diag=False)
    block, _ = build_summary_block("BRENT", res_b)
    await update.message.reply_html(block)

async def oil_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    res_wti  = fetch_prices("WTI",   days=max(FETCH_DAYS, CHART_WINDOW_DAYS), diag=False)
    res_brnt = fetch_prices("BRENT", days=max(FETCH_DAYS, CHART_WINDOW_DAYS), diag=False)
    await update.message.reply_html(build_text_summary(res_wti, res_brnt))

async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    res_wti  = fetch_prices("WTI",   days=max(FETCH_DAYS, CHART_WINDOW_DAYS), diag=False)
    res_brnt = fetch_prices("BRENT", days=max(FETCH_DAYS, CHART_WINDOW_DAYS), diag=False)
    await update.message.reply_html(build_text_summary(res_wti, res_brnt))
    tmpfiles = []
    try:
        if res_wti["ohlc"] is not None and len(res_wti["ohlc"]) >= 2 and res_wti["df"] is not None:
            p1 = os.path.join(tempfile.gettempdir(), f"wti_candle_{int(time.time())}.png")
            make_chart_png("WTI (CL=F)", res_wti["ohlc"], p1); tmpfiles.append(("WTI — candele 2 settimane", p1))
            p2 = os.path.join(tempfile.gettempdir(), f"wti_daily_{int(time.time())}.png")
            make_daily_line_chart_png("WTI (CL=F)", res_wti["df"], p2, days=60); tmpfiles.append(("WTI — grafico giornaliero", p2))
            p3 = os.path.join(tempfile.gettempdir(), f"wti_table_{int(time.time())}.png")
            make_daily_table_png("WTI (CL=F)", res_wti["df"], p3, rows=14); tmpfiles.append(("WTI — tabella giornaliera", p3))
        if res_brnt["ohlc"] is not None and len(res_brnt["ohlc"]) >= 2 and res_brnt["df"] is not None:
            p4 = os.path.join(tempfile.gettempdir(), f"brent_candle_{int(time.time())}.png")
            make_chart_png("Brent (BZ=F)", res_brnt["ohlc"], p4); tmpfiles.append(("Brent — candele 2 settimane", p4))
            p5 = os.path.join(tempfile.gettempdir(), f"brent_daily_{int(time.time())}.png")
            make_daily_line_chart_png("Brent (BZ=F)", res_brnt["df"], p5, days=60); tmpfiles.append(("Brent — grafico giornaliero", p5))
            p6 = os.path.join(tempfile.gettempdir(), f"brent_table_{int(time.time())}.png")
            make_daily_table_png("Brent (BZ=F)", res_brnt["df"], p6, rows=14); tmpfiles.append(("Brent — tabella giornaliera", p6))
        media = []
        for i, (cap, path) in enumerate(tmpfiles[:10]):
            with open(path, "rb") as f:
                media.append(InputMediaPhoto(f.read(), caption=cap if i == 0 else None))
        if media:
            await context.bot.send_media_group(chat_id, media=media)
    finally:
        for _, p in tmpfiles:
            try: os.remove(p)
            except Exception: pass

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip().lower()
    if t == "wti":   return await wti_cmd(update, context)
    if t == "brent": return await brent_cmd(update, context)
    if t in {"oil", "petrolio"}: return await oil_cmd(update, context)
    return await start_cmd(update, context)

# ================== BOOTSTRAP ==================
async def _post_init(app: Application):
    # Importantissimo per evitare conflitti: rimuove webhook e scarta eventuali update pendenti
    await app.bot.delete_webhook(drop_pending_updates=True)

def main():
    # Avvia health server (Render vuole una porta aperta)
    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help",  start_cmd))
    app.add_handler(CommandHandler("wti",   wti_cmd))
    app.add_handler(CommandHandler("brent", brent_cmd))
    app.add_handler(CommandHandler("oil",   oil_cmd))
    app.add_handler(CommandHandler("report",report_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("Bot Telegram in polling... (/start, /oil, /wti, /brent, /report)")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
