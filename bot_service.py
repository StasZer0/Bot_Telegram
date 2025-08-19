# bot_service.py
import os, threading, tempfile, time
from datetime import datetime as dt
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# ===== importa funzioni/variabili dal tuo script esistente =====
from oil_report_telegram import (
    TZ, FETCH_DAYS, CHART_WINDOW_DAYS,
    fetch_prices, build_summary_block,
    make_chart_png, make_daily_line_chart_png, make_daily_table_png
)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# ---------- Mini HTTP server /health (senza dipendenze) ----------
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200); self.send_header("Content-Type","text/plain"); self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404); self.end_headers()

def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    httpd = HTTPServer(("0.0.0.0", port), _Health)
    httpd.serve_forever()

# ---------- Helper ----------
def build_text_summary(res_wti, res_brent):
    now = dt.now(TZ)
    header = f"🛢️ <b>Oil update</b> – {now:%Y-%m-%d %H:%M} (Asia/Bangkok)"
    wti_block, _   = build_summary_block("WTI",   res_wti)
    brnt_block, _  = build_summary_block("BRENT", res_brent)
    return (
        header + "\n\n" + wti_block + "\n\n" + brnt_block +
        "\n\n⚠️ Fonte primaria: Stooq; fallback: FRED (St. Louis Fed). Dati indicativi, non consulenza finanziaria."
    )

# ---------- Handlers ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "Ciao! Comandi:\n"
        "• /wti – ultimo WTI\n"
        "• /brent – ultimo Brent\n"
        "• /oil – riepilogo WTI+Brent\n"
        "• /report – riepilogo + immagini"
    )

async def oil_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    res_wti  = fetch_prices("WTI",   days=max(FETCH_DAYS, CHART_WINDOW_DAYS), diag=False)
    res_brnt = fetch_prices("BRENT", days=max(FETCH_DAYS, CHART_WINDOW_DAYS), diag=False)
    await update.message.reply_html(build_text_summary(res_wti, res_brent))

async def wti_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    res_wti = fetch_prices("WTI", days=max(FETCH_DAYS, CHART_WINDOW_DAYS), diag=False)
    block, _ = build_summary_block("WTI", res_wti)
    await update.message.reply_html(block)

async def brent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    res_b = fetch_prices("BRENT", days=max(FETCH_DAYS, CHART_WINDOW_DAYS), diag=False)
    block, _ = build_summary_block("BRENT", res_b)
    await update.message.reply_html(block)

async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    res_wti  = fetch_prices("WTI",   days=max(FETCH_DAYS, CHART_WINDOW_DAYS), diag=False)
    res_brnt = fetch_prices("BRENT", days=max(FETCH_DAYS, CHART_WINDOW_DAYS), diag=False)
    await update.message.reply_html(build_text_summary(res_wti, res_brnt))

    tmpfiles = []
    try:
        # WTI
        if res_wti["ohlc"] is not None and len(res_wti["ohlc"]) >= 2 and res_wti["df"] is not None:
            p1 = os.path.join(tempfile.gettempdir(), f"wti_candle_{int(time.time())}.png")
            make_chart_png("WTI (CL=F)", res_wti["ohlc"], p1); tmpfiles.append(("WTI — candele 2 settimane", p1))
            p2 = os.path.join(tempfile.gettempdir(), f"wti_daily_{int(time.time())}.png")
            make_daily_line_chart_png("WTI (CL=F)", res_wti["df"], p2, days=60); tmpfiles.append(("WTI — grafico giornaliero", p2))
            p3 = os.path.join(tempfile.gettempdir(), f"wti_table_{int(time.time())}.png")
            make_daily_table_png("WTI (CL=F)", res_wti["df"], p3, rows=14); tmpfiles.append(("WTI — tabella giornaliera", p3))

        # BRENT
        if res_brnt["ohlc"] is not None and len(res_brnt["ohlc"]) >= 2 and res_brnt["df"] is not None:
            p4 = os.path.join(tempfile.gettempdir(), f"brent_candle_{int(time.time())}.png")
            make_chart_png("Brent (BZ=F)", res_brnt["ohlc"], p4); tmpfiles.append(("Brent — candele 2 settimane", p4))
            p5 = os.path.join(tempfile.gettempdir(), f"brent_daily_{int(time.time())}.png")
            make_daily_line_chart_png("Brent (BZ=F)", res_brnt["df"], p5, days=60); tmpfiles.append(("Brent — grafico giornaliero", p5))
            p6 = os.path.join(tempfile.gettempdir(), f"brent_table_{int(time.time())}.png")
            make_daily_table_png("Brent (BZ=F)", res_brnt["df"], p6, rows=14); tmpfiles.append(("Brent — tabella giornaliera", p6))

        # invio (album max 10)
        media = []
        for i, (cap, path) in enumerate(tmpfiles[:10]):
            with open(path, "rb") as f:
                media.append(InputMediaPhoto(f.read(), caption=cap if i == 0 else None))
        if media:
            await context.bot.send_media_group(chat_id, media=media)
    finally:
        for _, p in tmpfiles:
            try: os.remove(p)
            except: pass

def main():
    # Avvia il server HTTP /health su PORT (Render lo richiede)
    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("oil", oil_cmd))
    app.add_handler(CommandHandler("wti", wti_cmd))
    app.add_handler(CommandHandler("brent", brent_cmd))
    app.add_handler(CommandHandler("report", report_cmd))

    print("Bot Telegram in polling... (/start, /oil, /report)")
    app.run_polling()

if __name__ == "__main__":
    main()
