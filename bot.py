"""
Rīta refleksijas Telegram bots.

Katru dienu noteiktā laikā bots uzdod lietotājam 5 "maģiskos jautājumus",
lietotājs atbild ar tekstu vai balsi (balss tiek pārvērsta tekstā ar
OpenAI Whisper API), un visas atbildes tiek saglabātas SQLite datubāzē
kā ikdienas dienasgrāmatas ieraksts.
"""

import os
import re
import sqlite3
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from openai import OpenAI
from pydub import AudioSegment

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Riga"))
DB_PATH = os.environ.get("DB_PATH", "data/bot.db")
DEFAULT_MORNING_TIME = os.environ.get("DEFAULT_MORNING_TIME", "08:00")

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# 5 jautājumi, adaptēti no "Maģiskie jautājumi partnerim" uz solo rīta refleksiju
QUESTIONS = [
    "Par ko tu šobrīd esi visvairāk pateicīgs?",
    "Kāda uzvara vai panākums tev bija pēdējās 24 stundās?",
    "Kādu konkrētu problēmu tu redzi šobrīd?",
    "Kādi konkrēti nodomi tev ir šodienai?",
    "Kādu jautājumu tu šobrīd vēlies uzdot Visumam (vai sev)?",
]


# ---------- Datubāze ----------

def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            morning_time TEXT NOT NULL DEFAULT '08:00',
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS sessions (
            chat_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            current_index INTEGER NOT NULL DEFAULT 0,
            done INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, date)
        );
        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            question_index INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            answer_text TEXT NOT NULL,
            is_voice INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def today_str():
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def now_hhmm():
    return datetime.now(TIMEZONE).strftime("%H:%M")


# ---------- Palīgfunkcijas ----------

async def send_question(chat_id, idx, context: ContextTypes.DEFAULT_TYPE):
    total = len(QUESTIONS)
    text = f"({idx + 1}/{total}) {QUESTIONS[idx]}"
    await context.bot.send_message(chat_id=chat_id, text=text)


async def start_daily_flow(chat_id, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    date = today_str()
    existing = conn.execute(
        "SELECT 1 FROM sessions WHERE chat_id=? AND date=?", (chat_id, date)
    ).fetchone()
    if existing:
        conn.close()
        return
    conn.execute(
        "INSERT INTO sessions (chat_id, date, current_index, done) VALUES (?, ?, 0, 0)",
        (chat_id, date),
    )
    conn.commit()
    conn.close()
    await context.bot.send_message(
        chat_id=chat_id,
        text="Labrīt! Laiks rīta refleksijai. Atbildi ar tekstu vai balss ziņu.",
    )
    await send_question(chat_id, 0, context)


async def check_and_trigger(context: ContextTypes.DEFAULT_TYPE):
    """Palaižas ik minūti, pārbauda vai kādam lietotājam ir pienācis rīta laiks."""
    hhmm = now_hhmm()
    date = today_str()
    conn = db()
    rows = conn.execute(
        "SELECT chat_id FROM users WHERE active=1 AND morning_time=?", (hhmm,)
    ).fetchall()
    conn.close()
    for (chat_id,) in rows:
        await start_daily_flow(chat_id, context)


async def transcribe_voice(file_path_ogg: str) -> str:
    mp3_path = file_path_ogg.replace(".oga", ".mp3")
    AudioSegment.from_file(file_path_ogg).export(mp3_path, format="mp3")
    with open(mp3_path, "rb") as f:
        result = openai_client.audio.transcriptions.create(
            model="whisper-1", file=f, language="lv"
        )
    os.remove(file_path_ogg)
    os.remove(mp3_path)
    return result.text.strip()


def format_entry(chat_id, date) -> str:
    conn = db()
    rows = conn.execute(
        "SELECT question_text, answer_text FROM answers "
        "WHERE chat_id=? AND date=? ORDER BY question_index",
        (chat_id, date),
    ).fetchall()
    conn.close()
    if not rows:
        return f"Nav ierakstu par {date}."
    lines = [f"📓 Ieraksts — {date}\n"]
    for q, a in rows:
        lines.append(f"❓ {q}\n💬 {a}\n")
    return "\n".join(lines)


# ---------- Komandas ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO users (chat_id, morning_time, active) VALUES (?, ?, 1)",
        (chat_id, DEFAULT_MORNING_TIME),
    )
    conn.commit()
    row = conn.execute(
        "SELECT morning_time FROM users WHERE chat_id=?", (chat_id,)
    ).fetchone()
    conn.close()
    await update.message.reply_text(
        "Sveiks! Es katru dienu plkst. "
        f"{row[0]} uzdošu tev 5 rīta refleksijas jautājumus.\n\n"
        "Komandas:\n"
        "/laiks HH:MM — mainīt rīta laiku\n"
        "/tagad — sākt šodienas jautājumus uzreiz\n"
        "/sodien — parādīt šodienas ierakstu\n"
        "/vesture N — parādīt pēdējo N dienu ierakstus (noklusēti 7)\n"
        "/palidziba — palīdzība"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_laiks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args or not re.match(r"^\d{1,2}:\d{2}$", context.args[0]):
        await update.message.reply_text("Lieto formātā: /laiks 08:00")
        return
    new_time = context.args[0]
    h, m = map(int, new_time.split(":"))
    if not (0 <= h < 24 and 0 <= m < 60):
        await update.message.reply_text("Nederīgs laiks. Piemērs: /laiks 07:30")
        return
    new_time = f"{h:02d}:{m:02d}"
    conn = db()
    conn.execute(
        "INSERT INTO users (chat_id, morning_time, active) VALUES (?, ?, 1) "
        "ON CONFLICT(chat_id) DO UPDATE SET morning_time=excluded.morning_time",
        (chat_id, new_time),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Rīta laiks iestatīts uz {new_time}.")


async def cmd_tagad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO users (chat_id, morning_time, active) VALUES (?, ?, 1)",
        (chat_id, DEFAULT_MORNING_TIME),
    )
    # ļauj pārsākt, ja šodien vēl nav pabeigts vai vēl nav sākts
    conn.execute(
        "DELETE FROM sessions WHERE chat_id=? AND date=? AND done=0",
        (chat_id, today_str()),
    )
    conn.commit()
    conn.close()
    await start_daily_flow(chat_id, context)


async def cmd_sodien(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(format_entry(chat_id, today_str()))


async def cmd_vesture(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    n = 7
    if context.args and context.args[0].isdigit():
        n = int(context.args[0])
    conn = db()
    dates = conn.execute(
        "SELECT DISTINCT date FROM answers WHERE chat_id=? ORDER BY date DESC LIMIT ?",
        (chat_id, n),
    ).fetchall()
    conn.close()
    if not dates:
        await update.message.reply_text("Vēl nav neviena ieraksta.")
        return
    for (date,) in dates:
        await update.message.reply_text(format_entry(chat_id, date))


# ---------- Atbilžu apstrāde ----------

async def _save_answer_and_advance(chat_id, date, idx, answer_text, is_voice, context):
    conn = db()
    conn.execute(
        "INSERT INTO answers (chat_id, date, question_index, question_text, "
        "answer_text, is_voice, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            chat_id,
            date,
            idx,
            QUESTIONS[idx],
            answer_text,
            1 if is_voice else 0,
            datetime.now(TIMEZONE).isoformat(),
        ),
    )
    next_idx = idx + 1
    if next_idx >= len(QUESTIONS):
        conn.execute(
            "UPDATE sessions SET current_index=?, done=1 WHERE chat_id=? AND date=?",
            (next_idx, chat_id, date),
        )
        conn.commit()
        conn.close()
        await context.bot.send_message(chat_id=chat_id, text="Paldies! Šodienas ieraksts saglabāts. 🌅")
        await context.bot.send_message(chat_id=chat_id, text=format_entry(chat_id, date))
    else:
        conn.execute(
            "UPDATE sessions SET current_index=? WHERE chat_id=? AND date=?",
            (next_idx, chat_id, date),
        )
        conn.commit()
        conn.close()
        await send_question(chat_id, next_idx, context)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    date = today_str()
    conn = db()
    row = conn.execute(
        "SELECT current_index FROM sessions WHERE chat_id=? AND date=? AND done=0",
        (chat_id, date),
    ).fetchone()
    conn.close()
    if not row:
        await update.message.reply_text(
            "Šobrīd nav aktīva jautājuma. Lieto /tagad, lai sāktu šodienas refleksiju."
        )
        return
    idx = row[0]
    await _save_answer_and_advance(chat_id, date, idx, update.message.text, False, context)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    date = today_str()
    conn = db()
    row = conn.execute(
        "SELECT current_index FROM sessions WHERE chat_id=? AND date=? AND done=0",
        (chat_id, date),
    ).fetchone()
    conn.close()
    if not row:
        await update.message.reply_text(
            "Šobrīd nav aktīva jautājuma. Lieto /tagad, lai sāktu šodienas refleksiju."
        )
        return
    if not openai_client:
        await update.message.reply_text(
            "Balss transkripcija nav konfigurēta (trūkst OPENAI_API_KEY)."
        )
        return
    idx = row[0]
    tg_file = await context.bot.get_file(update.message.voice.file_id)
    os.makedirs("tmp", exist_ok=True)
    ogg_path = f"tmp/{chat_id}_{idx}.oga"
    await tg_file.download_to_drive(ogg_path)
    await update.message.reply_text("Transkribēju balss ziņu...")
    try:
        text = await transcribe_voice(ogg_path)
    except Exception as e:
        logger.exception("Transkripcijas kļūda")
        await update.message.reply_text(f"Neizdevās transkribēt: {e}")
        return
    await update.message.reply_text(f"Atpazīts teksts: {text}")
    await _save_answer_and_advance(chat_id, date, idx, text, True, context)


# ---------- Palaišana ----------

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("palidziba", cmd_help))
    app.add_handler(CommandHandler("laiks", cmd_laiks))
    app.add_handler(CommandHandler("tagad", cmd_tagad))
    app.add_handler(CommandHandler("sodien", cmd_sodien))
    app.add_handler(CommandHandler("vesture", cmd_vesture))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.job_queue.run_repeating(check_and_trigger, interval=60, first=5)

    logger.info("Bots startē...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
