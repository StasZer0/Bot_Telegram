# -*- coding: utf-8 -*-
"""
oil_report_telegram.py
Fonte primaria: Stooq (CL.F / BRN.F). Fallback: FRED (DCOILWTICO / DCOILBRENTEU).
Invio su Telegram con grafico (ultime 2 settimane) + indicatori.
"""

import os, sys, io, math, json, time, tempfile, datetime as dt
from urllib.request import Request, urlopen
from urllib.parse import urlencode
import numpy as np
import pandas as pd
pd.options.mode.copy_on_write = True

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import requests


from html import escape as html_escape

# =============== CONFIG TELEGRAM ===============

# Token e Chat ID (già i tuoi valori reali)
TELEGRAM_TOKEN = os.getenv("TG_TOKEN") or "8174807681:AAGVgWeqZgQMYXNAnqQucKMoIQ4TLSuz6XU"
TELEGRAM_CHATID = os.getenv("TG_CHATID") or "636137539"

# Controllo presenza token e chat_id (per evitare 404 silenziosi)
if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.startswith("INSERISCI"):
    raise RuntimeError("❌ TG_TOKEN mancante o non valido. Inserisci un token corretto.")
if not TELEGRAM_CHATID or TELEGRAM_CHATID.startswith("INSERISCI"):
    raise RuntimeError("❌ TG_CHATID mancante o non valido. Inserisci un ID chat corretto.")


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

# Ordine fonti
SOURCE_ORDER = ["stooq", "fred"]

# Finestra grafico (ultime 2 settimane visibili)
CHART_WINDOW_DAYS = 14
# Giorni da scaricare per indicatori/delta
FETCH_DAYS = 60  # >= 45 per 30d, metto 60 per margine

# Fuso per intestazione
from zoneinfo import ZoneInfo
TZ = ZoneInfo("Asia/Bangkok")

UA = "Mozilla/5.0 (compatible; OilBot/1.0; +https://stooq.com)"

# =============== HTTP helpers ===============
def http_get_bytes(url: str, timeout: int = 30, diag: bool = False) -> bytes:
    if diag:
        print(f"[GET] {url}")
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()

def robust_read_csv(data: bytes) -> pd.DataFrame:
    txt = data.decode("utf-8", errors="ignore")
    return pd.read_csv(io.StringIO(txt))

# =============== Indicatori ===============
def rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    """
    RSI(14) classico con medie semplici; NaN fino a 'period'.
    """
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi

# =============== Fetchers ===============
def fetch_stooq(tkr: str, days: int, diag: bool = False) -> pd.DataFrame | None:
    sym = STQ_SYMBOLS.get(tkr.upper())
    if not sym:
        return None
    # c= numero punti (giorni di calendario/mercato gestiti da stooq)
    url = f"https://stooq.com/q/l/?s={sym}&c={max(days, FETCH_DAYS)}&f=sd2t2ohlcv&h&e=csv"
    try:
        b = http_get_bytes(url, diag=diag)
        df = robust_read_csv(b)
        # Colonne attese: Date,Open,High,Low,Close,Volume
        cols = {c.lower(): c for c in df.columns}
        must = all(x in cols for x in ["date", "open", "high", "low", "close"])
        if not must:
            if diag:
                print("[STQ] formato CSV inatteso:", df.columns.tolist())
            return None
        # Normalizzazione
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
        df = df.dropna(subset=["Close"])
        df = df.sort_values("Date").reset_index(drop=True)
        return df
    except Exception as e:
        if diag:
            print("[STQ] errore:", repr(e))
        return None

def fetch_fred_close_series(series_id: str, diag: bool = False) -> pd.DataFrame | None:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        b = http_get_bytes(url, diag=diag)
        df = robust_read_csv(b)
        # Gestione sia 'DATE' sia 'observation_date'
        lower = {c.lower(): c for c in df.columns}
        date_col = lower.get("date") or lower.get("observation_date")
        # Valore: 'value' OPPURE nome della serie
        val_col = lower.get("value") or lower.get(series_id.lower())
        if not date_col or not val_col:
            if diag:
                print("[FRED] header inatteso:", df.columns.tolist())
            return None

        df = df.rename(columns={date_col: "Date", val_col: "Close"})
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
        df = df.dropna(subset=["Date", "Close"]).sort_values("Date").reset_index(drop=True)

        # Ricostruzione pseudo-OHLC per il grafico
        df["Open"] = df["Close"].shift(1)
        df["Open"] = df["Open"].fillna(df["Close"])
        df["High"] = df[["Open", "Close"]].max(axis=1)
        df["Low"]  = df[["Open", "Close"]].min(axis=1)
        return df[["Date","Open","High","Low","Close"]]
    except Exception as e:
        if diag:
            print("[FRED] errore:", repr(e))
        return None

def fetch_prices(tkr: str, days: int, diag: bool = False) -> dict:
    """
    Prova Stooq poi FRED. Ritorna dict con:
    {
      "source": "stooq"|"fred"|None,
      "df":     DataFrame completo (ordinato),
      "ohlc":   DataFrame ultimi N giorni per grafico (Date,Open,High,Low,Close)
    }
    """
    # 1) STQ
    if "stooq" in SOURCE_ORDER:
        stq = fetch_stooq(tkr, days=max(days, FETCH_DAYS), diag=diag)
        if stq is not None and len(stq) > 0:
            ohlc = stq.tail(days).copy()
            return {"source": "stooq", "df": stq, "ohlc": ohlc}

    # 2) FRED
    if "fred" in SOURCE_ORDER:
        fred_id = FRED_IDS.get(tkr.upper())
        fred = fetch_fred_close_series(fred_id, diag=diag) if fred_id else None
        if fred is not None and len(fred) > 0:
            # Di FRED mi tengo molti giorni per delta/RSI
            return {"source": "fred", "df": fred, "ohlc": fred.tail(days).copy()}

    return {"source": None, "df": None, "ohlc": None}

# =============== Formattazione numeri/sicura ===============
def fmt_pct(x: float | None) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "n/a"
    return f"{x:+.2f}%"

def fmt_price(x: float | None) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "n/a"
    return f"{x:.2f} USD"

# =============== Candlestick base (senza librerie extra) ===============
def draw_candles(ax, dates, opens, highs, lows, closes, width=0.6):
    # dates: matplotlib date numbers
    for d, o, h, l, c in zip(dates, opens, highs, lows, closes):
        color = "tab:green" if c >= o else "tab:red"
        # Wick
        ax.vlines(d, l, h, linewidth=1, color=color)
        # Body
        top = max(o, c)
        bottom = min(o, c)
        ax.add_patch(plt.Rectangle((d - width/2, bottom),
                                   width, top - bottom if top > bottom else 0.001,
                                   facecolor=color, edgecolor=color, linewidth=1, alpha=0.8))

# =============== Grafico ===============
def make_chart_png(title: str, ohlc: pd.DataFrame, outpath: str):
    """
    Grafico candele + RSI(14). Calcola l'RSI su una finestra più ampia
    per avere valori anche se mostriamo solo 14 giorni.
    """
    period = 14
    # dati ordinati
    o = ohlc.copy().sort_values("Date")

    # Finestra da mostrare
    d_show = o.tail(CHART_WINDOW_DAYS).dropna(subset=["Open", "High", "Low", "Close"])
    if d_show.empty:
        fig = plt.figure(figsize=(7.5, 6), dpi=150, constrained_layout=True)
        ax = fig.add_subplot(2, 1, 1)
        ax.set_title(f"{title} — ultime 2 settimane (nessun dato valido)")
        fig.savefig(outpath, bbox_inches="tight")
        plt.close(fig)
        return

    # Buffer per RSI: prendiamo CHART_WINDOW_DAYS + period
    d_buf = o.tail(CHART_WINDOW_DAYS + period).dropna(subset=["Close"])

    # Calcola RSI sul buffer e poi taglia per allinearlo a d_show
    rsi_full = rsi_series(d_buf["Close"], period=period)
    # allinea l'RSI alle date da mostrare
    rsi_to_plot = rsi_full.tail(len(d_show)).reset_index(drop=True)

    # Date come numeri matplotlib
    x = mdates.date2num(d_show["Date"].to_numpy(dtype="datetime64[ns]"))

    # Figura
    fig = plt.figure(figsize=(7.5, 6), dpi=150, constrained_layout=True)
    gs = fig.add_gridspec(2, 1, height_ratios=[3, 1])
    ax = fig.add_subplot(gs[0])
    ax_rsi = fig.add_subplot(gs[1], sharex=ax)

    ax.xaxis_date(); ax_rsi.xaxis_date()

    # Candele
    draw_candles(ax, x,
                 d_show["Open"].values, d_show["High"].values,
                 d_show["Low"].values,  d_show["Close"].values)
    ax.set_ylabel("USD/barile")
    ax.set_title(f"{title} — ultime 2 settimane", fontsize=10)

    # RSI(14) – se ancora tutto NaN, mostro messaggio
    if rsi_to_plot.notna().any():
        ax_rsi.plot(d_show["Date"], rsi_to_plot, linewidth=1.2)
    else:
        ax_rsi.text(0.5, 0.5, "RSI(14) non disponibile",
                    ha="center", va="center", transform=ax_rsi.transAxes)

    ax_rsi.axhline(70, linestyle="--", linewidth=0.8, color="gray")
    ax_rsi.axhline(30, linestyle="--", linewidth=0.8, color="gray")
    ax_rsi.set_ylabel("RSI(14)")
    ax_rsi.set_ylim(0, 100)

    # Asse X
    fmt = mdates.DateFormatter("%m-%d")
    ax.xaxis.set_major_formatter(fmt)
    ax_rsi.xaxis.set_major_formatter(fmt)

    ax.grid(True, linewidth=0.3, alpha=0.5)
    ax_rsi.grid(True, linewidth=0.3, alpha=0.5)

    ax.relim(); ax.autoscale_view()
    ax_rsi.relim(); ax_rsi.autoscale_view()

    fig.savefig(outpath, bbox_inches="tight")
    plt.close(fig)

def make_daily_line_chart_png(title: str, df: pd.DataFrame, outpath: str, days: int = 60):
    """
    Grafico giornaliero a linea del 'Close' con SMA20/SMA50.
    Usa gli ultimi 'days' (default 60).
    """
    d = df.copy().sort_values("Date").dropna(subset=["Close"]).tail(days)
    if d.empty:
        fig = plt.figure(figsize=(7.5, 4.2), dpi=150, constrained_layout=True)
        ax = fig.add_subplot(1,1,1)
        ax.set_title(f"{title} — grafico giornaliero (nessun dato)")
        fig.savefig(outpath, bbox_inches="tight"); plt.close(fig); return

    sma20 = d["Close"].rolling(20).mean()
    sma50 = d["Close"].rolling(50).mean()

    fig = plt.figure(figsize=(7.5, 4.2), dpi=150, constrained_layout=True)
    ax = fig.add_subplot(1,1,1)
    ax.plot(d["Date"], d["Close"], linewidth=1.5, label="Close")
    ax.plot(d["Date"], sma20, linewidth=1.1, label="SMA 20")
    ax.plot(d["Date"], sma50, linewidth=1.1, label="SMA 50")
    ax.set_title(f"{title} — grafico giornaliero (ultimi {len(d)}g)")
    ax.set_ylabel("USD/barile")
    ax.grid(True, linewidth=0.3, alpha=0.5)

    fmt = mdates.DateFormatter("%m-%d")
    ax.xaxis.set_major_formatter(fmt)
    ax.legend(loc="best", fontsize=8)
    fig.savefig(outpath, bbox_inches="tight")
    plt.close(fig)

def make_daily_table_png(title: str, df: pd.DataFrame, outpath: str, rows: int = 14):
    """
    Tabella con gli ultimi 'rows' giorni: Date, Open, High, Low, Close.
    """
    d = df.copy().sort_values("Date").dropna(subset=["Open","High","Low","Close"]).tail(rows)
    if d.empty:
        fig = plt.figure(figsize=(7.5, 3.6), dpi=150, constrained_layout=True)
        ax = fig.add_subplot(1,1,1)
        ax.set_title(f"{title} — tabella giornaliera (nessun dato)")
        ax.axis("off")
        fig.savefig(outpath, bbox_inches="tight"); plt.close(fig); return

    # prepara dati formattati
    tab = []
    for _, r in d.iterrows():
        tab.append([
            r["Date"].strftime("%Y-%m-%d"),
            f"{r['Open']:.2f}",
            f"{r['High']:.2f}",
            f"{r['Low']:.2f}",
            f"{r['Close']:.2f}",
        ])

    fig = plt.figure(figsize=(7.5, 3.6), dpi=150, constrained_layout=True)
    ax = fig.add_subplot(1,1,1)
    ax.axis("off")
    ax.set_title(f"{title} — ultimi {len(tab)} giorni (OHLC)", pad=10)

    col_labels = ["Date", "Open", "High", "Low", "Close"]
    the_table = ax.table(cellText=tab, colLabels=col_labels, loc="center")
    the_table.auto_set_font_size(False)
    the_table.set_fontsize(8)
    the_table.scale(1, 1.25)

    fig.savefig(outpath, bbox_inches="tight")
    plt.close(fig)

# =============== Telegram ===============
def tg_call(method: str, data=None, files=None, timeout=20, retries=3, backoff=1.5, diag=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    last_err = None
    for i in range(retries):
        try:
            if files:
                r = requests.post(url, data=data, files=files, timeout=timeout)
            else:
                r = requests.post(url, data=data, timeout=timeout)
            if r.ok and r.json().get("ok"):
                return r.json()
            last_err = r.text
        except Exception as e:
            last_err = str(e)
        if diag:
            print(f"[TG] retry {i+1}/{retries} errore: {last_err}")
        time.sleep(backoff**i)
    raise RuntimeError(f"Telegram {method} failed: {last_err}")

def send_telegram_html(html: str, disable_preview: bool = True, diag: bool = False):
    data = {
        "chat_id": TELEGRAM_CHATID,
        "text": html,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    return tg_call("sendMessage", data=data, diag=diag)

def send_telegram_photo(path: str, caption_html: str = None, diag: bool = False):
    with open(path, "rb") as f:
        files = {"photo": f}
        data = {"chat_id": TELEGRAM_CHATID}
        if caption_html:
            data["caption"] = caption_html
            data["parse_mode"] = "HTML"
        return tg_call("sendPhoto", data=data, files=files, diag=diag)

def send_telegram_album(items, diag=False):
    """
    Invia più foto in un unico messaggio (album).
    items: lista di tuple (path_png, caption_html)
           max 10 elementi secondo i limiti Telegram.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMediaGroup"

    files = {}
    media = []
    for i, (path, caption) in enumerate(items):
        key = f"photo{i}"
        files[key] = (os.path.basename(path), open(path, "rb"), "image/png")
        media.append({
            "type": "photo",
            "media": f"attach://{key}",
            "caption": caption or "",
            "parse_mode": "HTML"
        })

    data = {"chat_id": TELEGRAM_CHATID, "media": json.dumps(media)}
    r = requests.post(url, data=data, files=files, timeout=30)
    # chiudi i file aperti
    for f in files.values():
        try:
            f[1].close()
        except Exception:
            pass

    if diag:
        try:
            print("[TG] sendMediaGroup:", r.status_code, r.text[:200])
        except Exception:
            print("[TG] sendMediaGroup:", r.status_code)
    r.raise_for_status()
    return r.json()


# =============== Analisi / Messaggi ===============
def build_summary_block(name: str, res: dict) -> tuple[str, str]:
    """
    Costruisce il blocco HTML per WTI/Brent e restituisce (html_block, source_label).
    Se la serie primaria (Stooq) ha poca storia, integra lo storico da FRED
    solo per il calcolo degli indicatori (prezzo ultimo resta quello primario).
    """
    df = res.get("df")
    src = res.get("source")
    name_upper = name.upper()

    # Link cliccabili
    name_link_html = {
        "WTI":   '<a href="https://stooq.com/q/?s=cl.f">WTI</a>',
        "BRENT": '<a href="https://stooq.com/q/?s=brn.f">Brent</a>',
    }
    title_html = name_link_html.get(name_upper, html_escape(name))

    # Nessun dato
    if df is None or len(df) == 0:
        html = (f"<b>{title_html} ({'CL-F' if name_upper=='WTI' else 'BZ-F'} proxy {src or 'N/A'})</b>\n"
                f"⚠️ Dati non disponibili.\n")
        return html, (src or "n/a")

    # Normalizza e ordina
    df = df.copy().sort_values("Date").reset_index(drop=True)

    # Se lo storico è corto, prova a fondere con FRED per gli indicatori
    close = df["Close"]
    if len(close) < 40:
        fred_id: str | None = FRED_IDS.get(name_upper)  # <- INIZIALIZZATO SEMPRE
        fred_df = None
        if fred_id:
            fred_df = fetch_fred_close_series(fred_id)
        if fred_df is not None and len(fred_df) > 0:
            extra = fred_df[["Date", "Close"]].rename(columns={"Close": "Close_fred"})
            df = df.merge(extra, on="Date", how="outer").sort_values("Date")
            # mantieni il prezzo primario dove c'è; riempi buchi con FRED
            df["Close"] = df["Close"].fillna(df["Close_fred"])
            df = df.drop(columns=["Close_fred"]).reset_index(drop=True)
            close = df["Close"]

    # --- Indicatori ---
    def pct(n: int) -> float | None:
        if len(close) > n:
            return float((close.iloc[-1] / close.iloc[-n-1] - 1.0) * 100.0)
        return None

    d1  = pct(1)
    d7  = pct(7)
    d30 = pct(30)

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

def main():
    diag = ("--diag" in sys.argv)

    now = dt.datetime.now(TZ)
    header = f"🛢️ <b>Oil update</b> – {now:%Y-%m-%d %H:%M} (Asia/Bangkok)"

    if diag:
        print("== DIAGNOSTICA ==")

    # WTI
    res_wti  = fetch_prices("WTI", days=max(FETCH_DAYS, CHART_WINDOW_DAYS), diag=diag)
    # BRENT
    res_brnt = fetch_prices("BRENT", days=max(FETCH_DAYS, CHART_WINDOW_DAYS), diag=diag)

    # Corpo messaggio (testo)
    wti_block, wti_src   = build_summary_block("WTI",   res_wti)
    brnt_block, brnt_src = build_summary_block("BRENT", res_brnt)

    body = header + "\n\n" + wti_block + "\n\n" + brnt_block + "\n\n" + \
           "⚠️ Fonte primaria: Stooq; fallback: FRED (St. Louis Fed). Dati indicativi, non consulenza finanziaria."

    # Invia testo
    send_telegram_html(body, diag=diag)

            # Grafici (se disponibili)
    tmpfiles = []
    try:
        # --- WTI ---
        if res_wti["ohlc"] is not None and len(res_wti["ohlc"]) >= 2 and res_wti["df"] is not None:
            p_candle = os.path.join(tempfile.gettempdir(), f"wti_candle_{int(time.time())}.png")
            make_chart_png("WTI (CL=F)", res_wti["ohlc"], p_candle)
            tmpfiles.append(("WTI — candele 2 settimane", p_candle))

            p_daily = os.path.join(tempfile.gettempdir(), f"wti_daily_{int(time.time())}.png")
            make_daily_line_chart_png("WTI (CL=F)", res_wti["df"], p_daily, days=60)
            tmpfiles.append(("WTI — grafico giornaliero", p_daily))

            p_table = os.path.join(tempfile.gettempdir(), f"wti_table_{int(time.time())}.png")
            make_daily_table_png("WTI (CL=F)", res_wti["df"], p_table, rows=14)
            tmpfiles.append(("WTI — tabella giornaliera", p_table))

        # --- BRENT ---
        if res_brnt["ohlc"] is not None and len(res_brnt["ohlc"]) >= 2 and res_brnt["df"] is not None:
            p_candle = os.path.join(tempfile.gettempdir(), f"brent_candle_{int(time.time())}.png")
            make_chart_png("Brent (BZ=F)", res_brnt["ohlc"], p_candle)
            tmpfiles.append(("Brent — candele 2 settimane", p_candle))

            p_daily = os.path.join(tempfile.gettempdir(), f"brent_daily_{int(time.time())}.png")
            make_daily_line_chart_png("Brent (BZ=F)", res_brnt["df"], p_daily, days=60)
            tmpfiles.append(("Brent — grafico giornaliero", p_daily))

            p_table = os.path.join(tempfile.gettempdir(), f"brent_table_{int(time.time())}.png")
            make_daily_table_png("Brent (BZ=F)", res_brnt["df"], p_table, rows=14)
            tmpfiles.append(("Brent — tabella giornaliera", p_table))

        # Invia come album (se >1 immagine), sennò singola
        if len(tmpfiles) > 1:
            album = [(path, cap) for cap, path in tmpfiles]  # (path, caption)
            send_telegram_album(album, diag=diag)
        elif len(tmpfiles) == 1:
            cap, path = tmpfiles[0]
            send_telegram_photo(path, caption_html=cap, diag=diag)

    finally:
        for _, path in tmpfiles:
            try:
                os.remove(path)
            except Exception:
                pass


    if diag:
        print("✔ Report + grafici inviati su Telegram.")

if __name__ == "__main__":
    main()
