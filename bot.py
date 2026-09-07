"""
Ikdienas refleksijas Telegram bots (daudzvalodu: LV / EN / RU).

Katru dienu paša izvēlētā laikā (var būt rīts, vakars — jebkurš brīdis)
bots uzdod lietotājam 5 "maģiskos jautājumus",
lietotājs atbild ar tekstu vai balsi (balss tiek pārvērsta tekstā ar
OpenAI Whisper API), un visas atbildes tiek saglabātas SQLite datubāzē
kā ikdienas dienasgrāmatas ieraksts. Katrs lietotājs var izvēlēties
savu saskarnes valodu neatkarīgi no citiem.
"""

import os
import re
import sqlite3
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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
DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ("lv", "en", "ru")


def detect_language(telegram_language_code):
    """Nosaka valodu pēc Telegram lietotāja iestatījuma; ja nesaskan, noklusē uz angļu."""
    code = (telegram_language_code or "").lower()
    for lang in SUPPORTED_LANGUAGES:
        if code.startswith(lang):
            return lang
    return DEFAULT_LANGUAGE

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ---------- Valodas un tulkojumi ----------

LANGUAGES = {
    "lv": "🇱🇻 Latviešu",
    "en": "🇬🇧 English",
    "ru": "🇷🇺 Русский",
}

CHOOSE_LANGUAGE_TEXT = (
    "🇱🇻 Izvēlies valodu:\n🇬🇧 Choose a language:\n🇷🇺 Выберите язык:"
)

# 5 jautājumi katrā valodā, adaptēti no "Maģiskie jautājumi partnerim" uz solo refleksiju
QUESTIONS = {
    "lv": [
        "Par ko tu šobrīd esi visvairāk pateicīgs?",
        "Kāda uzvara vai panākums tev bija pēdējās 24 stundās?",
        "Kādu konkrētu problēmu tu redzi šobrīd?",
        "Kādi konkrēti nodomi tev ir šodienai?",
        "Kādu jautājumu tu šobrīd vēlies uzdot Visumam (vai sev)?",
    ],
    "en": [
        "What are you most grateful for right now?",
        "What win or success have you had in the last 24 hours?",
        "What specific problem do you see right now?",
        "What specific intentions do you have for today?",
        "What question do you want to ask the Universe (or yourself) right now?",
    ],
    "ru": [
        "За что ты сейчас больше всего благодарен?",
        "Какая победа или успех были у тебя за последние 24 часа?",
        "Какую конкретную проблему ты видишь прямо сейчас?",
        "Какие конкретные намерения у тебя на сегодня?",
        "Какой вопрос ты хочешь задать Вселенной (или себе) прямо сейчас?",
    ],
}

TEXTS = {
    "lv": {
        "greeting_named": "Sveiks, {name}! 👋",
        "greeting_plain": "Sveiks! 👋",
        "language_hint": "Ja vēlies citu valodu, spied 🌐 Valoda.",
        "reset_done": "Dati dzēsti. Nosūti /start, lai sāktu no jauna kā pilnīgi jauns lietotājs.",
        "btn_start_now": "▶️ Sākt tagad",
        "btn_today": "📓 Šodien",
        "btn_history": "📅 Vēsture",
        "btn_time": "⏰ Mainīt laiku",
        "btn_help": "❓ Palīdzība",
        "btn_language": "🌐 Valoda",
        "morning_greeting": "Ir laiks refleksijai! Atbildi ar tekstu vai balss ziņu.",
        "no_active_question": "Šobrīd nav aktīva jautājuma. Nospied \"▶️ Sākt tagad\", lai sāktu šodienas refleksiju.",
        "choose_time": "Izvēlies laiku, kad katru dienu saņemt jautājumus:",
        "time_set": "Laiks iestatīts uz {time}.",
        "time_usage": "Lieto formātā: /laiks 08:00",
        "custom_time_label": "✏️ Ievadīt pats",
        "custom_time_prompt": "Ieraksti vēlamo laiku formātā HH:MM (piemēram, 19:30).",
        "invalid_time_format": "Nederīgs formāts. Ieraksti, piemēram, 19:30.",
        "transcribing": "Transkribēju balss ziņu...",
        "recognized_text": "Atpazīts teksts: {text}",
        "transcription_error": "Neizdevās transkribēt: {error}",
        "voice_not_configured": "Balss transkripcija nav konfigurēta.",
        "thanks_saved": "Paldies! Šodienas ieraksts saglabāts. 🌅",
        "no_entries": "Vēl nav neviena ieraksta.",
        "no_entries_for_date": "Nav ierakstu par {date}.",
        "entry_header": "📓 Ieraksts — {date}\n",
        "intro": (
            "Reizi dienā, plkst. {time}, es tev uzdošu 5 refleksijas jautājumus — "
            "par pateicību, uzvarām, izaicinājumiem un nodomiem. Kad būsi atbildējis "
            "uz visiem, tie apkoposies dienas ierakstā. Šos apkopojumus vari jebkurā "
            "laikā palasīt ar 📓 Šodien vai 📅 Vēsture."
        ),
        "help": (
            "Lieto pogas apakšā:\n"
            "▶️ Sākt tagad — sākt šodienas jautājumus\n"
            "📓 Šodien — šodienas ieraksts\n"
            "📅 Vēsture — pēdējo dienu ieraksti\n"
            "⏰ Mainīt laiku — mainīt rīta laiku\n"
            "🌐 Valoda — mainīt valodu"
        ),
    },
    "en": {
        "greeting_named": "Hi, {name}! 👋",
        "greeting_plain": "Hi! 👋",
        "language_hint": "If you'd like a different language, tap 🌐 Language.",
        "reset_done": "Data cleared. Send /start to begin again as a brand-new user.",
        "btn_start_now": "▶️ Start now",
        "btn_today": "📓 Today",
        "btn_history": "📅 History",
        "btn_time": "⏰ Change time",
        "btn_help": "❓ Help",
        "btn_language": "🌐 Language",
        "morning_greeting": "Time for reflection! Reply with text or a voice message.",
        "no_active_question": "There's no active question right now. Tap \"▶️ Start now\" to begin today's reflection.",
        "choose_time": "Choose the time you'd like your daily questions:",
        "time_set": "Time set to {time}.",
        "time_usage": "Use the format: /laiks 08:00",
        "custom_time_label": "✏️ Enter manually",
        "custom_time_prompt": "Type the time you'd like in HH:MM format (e.g. 19:30).",
        "invalid_time_format": "Invalid format. Please type e.g. 19:30.",
        "transcribing": "Transcribing your voice message...",
        "recognized_text": "Recognized text: {text}",
        "transcription_error": "Transcription failed: {error}",
        "voice_not_configured": "Voice transcription isn't configured.",
        "thanks_saved": "Thanks! Today's entry is saved. 🌅",
        "no_entries": "No entries yet.",
        "no_entries_for_date": "No entries for {date}.",
        "entry_header": "📓 Entry — {date}\n",
        "intro": (
            "Once a day, at {time}, I'll ask you 5 reflection questions — "
            "about gratitude, wins, challenges, and intentions. Once you've "
            "answered them all, they're compiled into a daily entry. You can "
            "read these summaries anytime with 📓 Today or 📅 History."
        ),
        "help": (
            "Use the buttons below:\n"
            "▶️ Start now — begin today's questions\n"
            "📓 Today — today's entry\n"
            "📅 History — past entries\n"
            "⏰ Change time — change your morning time\n"
            "🌐 Language — change language"
        ),
    },
    "ru": {
        "greeting_named": "Привет, {name}! 👋",
        "greeting_plain": "Привет! 👋",
        "language_hint": "Если хочешь другой язык, нажми 🌐 Язык.",
        "reset_done": "Данные удалены. Отправь /start, чтобы начать заново как новый пользователь.",
        "btn_start_now": "▶️ Начать сейчас",
        "btn_today": "📓 Сегодня",
        "btn_history": "📅 История",
        "btn_time": "⏰ Изменить время",
        "btn_help": "❓ Помощь",
        "btn_language": "🌐 Язык",
        "morning_greeting": "Время для рефлексии! Ответь текстом или голосовым сообщением.",
        "no_active_question": "Сейчас нет активного вопроса. Нажми «▶️ Начать сейчас», чтобы начать сегодняшнюю рефлексию.",
        "choose_time": "Выбери время, когда каждый день получать вопросы:",
        "time_set": "Время установлено на {time}.",
        "time_usage": "Используй формат: /laiks 08:00",
        "custom_time_label": "✏️ Ввести вручную",
        "custom_time_prompt": "Введи нужное время в формате ЧЧ:ММ (например, 19:30).",
        "invalid_time_format": "Неверный формат. Введи, например, 19:30.",
        "transcribing": "Расшифровываю голосовое сообщение...",
        "recognized_text": "Распознанный текст: {text}",
        "transcription_error": "Не удалось расшифровать: {error}",
        "voice_not_configured": "Расшифровка голоса не настроена.",
        "thanks_saved": "Спасибо! Сегодняшняя запись сохранена. 🌅",
        "no_entries": "Пока нет записей.",
        "no_entries_for_date": "Нет записей за {date}.",
        "entry_header": "📓 Запись — {date}\n",
        "intro": (
            "Раз в день, в {time}, я буду задавать тебе 5 вопросов для "
            "рефлексии — о благодарности, победах, проблемах и намерениях. "
            "Когда ответишь на все, они соберутся в запись за день. Эти сводки "
            "можно читать в любое время с помощью 📓 Сегодня или 📅 История."
        ),
        "help": (
            "Используй кнопки внизу:\n"
            "▶️ Начать сейчас — начать сегодняшние вопросы\n"
            "📓 Сегодня — запись за сегодня\n"
            "📅 История — прошлые записи\n"
            "⏰ Изменить время — изменить утреннее время\n"
            "🌐 Язык — сменить язык"
        ),
    },
}


def t(lang, key, **kwargs):
    tx = TEXTS.get(lang, TEXTS[DEFAULT_LANGUAGE])
    template = tx.get(key, TEXTS[DEFAULT_LANGUAGE][key])
    return template.format(**kwargs) if kwargs else template


def main_menu_keyboard(lang):
    tx = TEXTS.get(lang, TEXTS[DEFAULT_LANGUAGE])
    return ReplyKeyboardMarkup(
        [
            [tx["btn_start_now"], tx["btn_today"]],
            [tx["btn_history"], tx["btn_time"]],
            [tx["btn_help"], tx["btn_language"]],
        ],
        resize_keyboard=True,
    )


def language_keyboard():
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"lang:{code}")]
        for code, label in LANGUAGES.items()
    ]
    return InlineKeyboardMarkup(buttons)


TIME_PRESETS = ["06:30", "07:00", "07:30", "08:00", "08:30", "09:00"]


def time_menu_keyboard(lang):
    tx = TEXTS.get(lang, TEXTS[DEFAULT_LANGUAGE])
    buttons = [InlineKeyboardButton(tm, callback_data=f"settime:{tm}") for tm in TIME_PRESETS]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton(tx["custom_time_label"], callback_data="settime:custom")])
    return InlineKeyboardMarkup(rows)


def _build_button_actions():
    mapping = {}
    for tx in TEXTS.values():
        mapping[tx["btn_start_now"]] = "start_now"
        mapping[tx["btn_today"]] = "today"
        mapping[tx["btn_history"]] = "history"
        mapping[tx["btn_time"]] = "time"
        mapping[tx["btn_help"]] = "help"
        mapping[tx["btn_language"]] = "language"
    return mapping


BUTTON_ACTIONS = _build_button_actions()


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
            language TEXT NOT NULL DEFAULT 'lv',
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
    # migrācija esošām datubāzēm, kas izveidotas pirms valodas atbalsta
    try:
        conn.execute("ALTER TABLE users ADD COLUMN language TEXT NOT NULL DEFAULT 'lv'")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def today_str():
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def now_hhmm():
    return datetime.now(TIMEZONE).strftime("%H:%M")


def get_user_language(chat_id):
    conn = db()
    row = conn.execute("SELECT language FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    if row and row[0] in TEXTS:
        return row[0]
    return DEFAULT_LANGUAGE


def set_user_language(chat_id, lang):
    conn = db()
    conn.execute(
        "INSERT INTO users (chat_id, morning_time, language, active) VALUES (?, ?, ?, 1) "
        "ON CONFLICT(chat_id) DO UPDATE SET language=excluded.language",
        (chat_id, DEFAULT_MORNING_TIME, lang),
    )
    conn.commit()
    conn.close()


def get_user_morning_time(chat_id):
    conn = db()
    row = conn.execute("SELECT morning_time FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return row[0] if row else DEFAULT_MORNING_TIME


# ---------- Palīgfunkcijas ----------

async def send_question(chat_id, idx, lang, context: ContextTypes.DEFAULT_TYPE):
    questions = QUESTIONS.get(lang, QUESTIONS[DEFAULT_LANGUAGE])
    total = len(questions)
    text = f"({idx + 1}/{total}) {questions[idx]}"
    await context.bot.send_message(chat_id=chat_id, text=text)


async def start_daily_flow(chat_id, context: ContextTypes.DEFAULT_TYPE, date=None):
    if date is None:
        date = today_str()
    conn = db()
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
    lang = get_user_language(chat_id)
    await context.bot.send_message(chat_id=chat_id, text=t(lang, "morning_greeting"))
    await send_question(chat_id, 0, lang, context)


async def check_and_trigger(context: ContextTypes.DEFAULT_TYPE):
    """Palaižas ik minūti, pārbauda vai kādam lietotājam ir pienācis rīta laiks."""
    hhmm = now_hhmm()
    conn = db()
    rows = conn.execute(
        "SELECT chat_id FROM users WHERE active=1 AND morning_time=?", (hhmm,)
    ).fetchall()
    conn.close()
    for (chat_id,) in rows:
        await start_daily_flow(chat_id, context)


async def transcribe_voice(file_path_ogg: str, lang: str) -> str:
    mp3_path = file_path_ogg.replace(".oga", ".mp3")
    AudioSegment.from_file(file_path_ogg).export(mp3_path, format="mp3")
    with open(mp3_path, "rb") as f:
        result = openai_client.audio.transcriptions.create(
            model="whisper-1", file=f, language=lang
        )
    os.remove(file_path_ogg)
    os.remove(mp3_path)
    return result.text.strip()


def format_entry(chat_id, date, lang) -> str:
    conn = db()
    rows = conn.execute(
        "SELECT question_text, answer_text FROM answers "
        "WHERE chat_id=? AND date=? ORDER BY question_index",
        (chat_id, date),
    ).fetchall()
    conn.close()
    if not rows:
        return t(lang, "no_entries_for_date", date=date)
    lines = [t(lang, "entry_header", date=date)]
    for q, a in rows:
        lines.append(f"❓ {q}\n💬 {a}\n")
    return "\n".join(lines)


async def send_today_entry(chat_id, context: ContextTypes.DEFAULT_TYPE):
    lang = get_user_language(chat_id)
    await context.bot.send_message(chat_id=chat_id, text=format_entry(chat_id, today_str(), lang))


async def send_history_entries(chat_id, n, context: ContextTypes.DEFAULT_TYPE):
    lang = get_user_language(chat_id)
    conn = db()
    dates = conn.execute(
        "SELECT DISTINCT date FROM answers WHERE chat_id=? ORDER BY date DESC LIMIT ?",
        (chat_id, n),
    ).fetchall()
    conn.close()
    if not dates:
        await context.bot.send_message(chat_id=chat_id, text=t(lang, "no_entries"))
        return
    for (date,) in dates:
        await context.bot.send_message(chat_id=chat_id, text=format_entry(chat_id, date, lang))


async def prompt_language_change(chat_id, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=chat_id, text=CHOOSE_LANGUAGE_TEXT, reply_markup=language_keyboard()
    )


# ---------- Komandas ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    telegram_lang_code = user.language_code if user else None
    detected_lang = detect_language(telegram_lang_code)

    conn = db()
    # INSERT OR IGNORE: ja lietotājs jau pastāv (atgriezies), viņa iepriekš
    # izvēlētā valoda saglabājas — auto-noteikšana attiecas tikai uz jauniem lietotājiem.
    conn.execute(
        "INSERT OR IGNORE INTO users (chat_id, morning_time, language, active) VALUES (?, ?, ?, 1)",
        (chat_id, DEFAULT_MORNING_TIME, detected_lang),
    )
    conn.commit()
    conn.close()

    lang = get_user_language(chat_id)
    morning_time = get_user_morning_time(chat_id)
    name = user.first_name if user else None
    greeting = t(lang, "greeting_named", name=name) if name else t(lang, "greeting_plain")
    message = f"{greeting}\n\n{t(lang, 'intro', time=morning_time)}\n\n{t(lang, 'language_hint')}"
    await update.message.reply_text(message, reply_markup=main_menu_keyboard(lang))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang = get_user_language(chat_id)
    await update.message.reply_text(t(lang, "help"), reply_markup=main_menu_keyboard(lang))


async def cmd_laiks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang = get_user_language(chat_id)
    if not context.args or not re.match(r"^\d{1,2}:\d{2}$", context.args[0]):
        await update.message.reply_text(t(lang, "time_usage"))
        return
    new_time = context.args[0]
    h, m = map(int, new_time.split(":"))
    if not (0 <= h < 24 and 0 <= m < 60):
        await update.message.reply_text(t(lang, "time_usage"))
        return
    new_time = f"{h:02d}:{m:02d}"
    conn = db()
    conn.execute(
        "INSERT INTO users (chat_id, morning_time, language, active) VALUES (?, ?, ?, 1) "
        "ON CONFLICT(chat_id) DO UPDATE SET morning_time=excluded.morning_time",
        (chat_id, new_time, lang),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(t(lang, "time_set", time=new_time))


async def cmd_tagad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    detected_lang = detect_language(update.effective_user.language_code if update.effective_user else None)
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO users (chat_id, morning_time, language, active) VALUES (?, ?, ?, 1)",
        (chat_id, DEFAULT_MORNING_TIME, detected_lang),
    )
    conn.execute(
        "DELETE FROM sessions WHERE chat_id=? AND date=? AND done=0",
        (chat_id, today_str()),
    )
    conn.commit()
    conn.close()
    await start_daily_flow(chat_id, context)


async def cmd_sodien(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_today_entry(update.effective_chat.id, context)


async def cmd_vesture(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = 7
    if context.args and context.args[0].isdigit():
        n = int(context.args[0])
    await send_history_entries(update.effective_chat.id, n, context)


async def cmd_valoda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await prompt_language_change(update.effective_chat.id, context)


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Testēšanai — parāda, ko Telegram reāli atsūta, ko bots no tā noteica,
    un kas ir saglabāts datubāzē, lai varētu pārbaudīt valodas noteikšanu."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    tg_code = user.language_code if user else None
    detected = detect_language(tg_code)
    stored = get_user_language(chat_id)
    morning_time = get_user_morning_time(chat_id)
    text = (
        "🔧 Debug info\n\n"
        f"Telegram language_code: {tg_code!r}\n"
        f"Auto-noteiktā valoda (šobrīd): {detected}\n"
        f"Saglabātā valoda (datubāzē): {stored}\n"
        f"Saglabātais laiks: {morning_time}\n"
        f"chat_id: {chat_id}"
    )
    await update.message.reply_text(text)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Testēšanai — izdzēš lietotāja paša datus (valoda, laiks, ieraksti),
    lai varētu no jauna izmēģināt /start plūsmu kā pilnīgi jaunam lietotājam."""
    chat_id = update.effective_chat.id
    lang = get_user_language(chat_id)
    conn = db()
    conn.execute("DELETE FROM answers WHERE chat_id=?", (chat_id,))
    conn.execute("DELETE FROM sessions WHERE chat_id=?", (chat_id,))
    conn.execute("DELETE FROM users WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()
    context.user_data.clear()
    await update.message.reply_text(t(lang, "reset_done"), reply_markup=ReplyKeyboardRemove())


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Testēšanai — nekavējoties pārceļ uz nākamo simulēto dienu un sāk tās
    jautājumus, negaidot reālu diennakts maiņu vai plānoto laiku."""
    chat_id = update.effective_chat.id
    conn = db()
    row = conn.execute(
        "SELECT date FROM sessions WHERE chat_id=? ORDER BY date DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    conn.close()
    if row:
        try:
            last_date = datetime.strptime(row[0], "%Y-%m-%d").date()
        except ValueError:
            last_date = datetime.now(TIMEZONE).date()
    else:
        last_date = datetime.now(TIMEZONE).date()
    next_date = (last_date + timedelta(days=1)).strftime("%Y-%m-%d")
    await start_daily_flow(chat_id, context, date=next_date)


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    lang = query.data.split(":", 1)[1]
    if lang not in TEXTS:
        lang = DEFAULT_LANGUAGE
    set_user_language(chat_id, lang)
    await query.answer()
    await query.edit_message_text(LANGUAGES[lang])
    morning_time = get_user_morning_time(chat_id)
    await context.bot.send_message(
        chat_id=chat_id,
        text=t(lang, "intro", time=morning_time),
        reply_markup=main_menu_keyboard(lang),
    )


async def settime_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    lang = get_user_language(chat_id)
    value = query.data.split(":", 1)[1]
    await query.answer()

    if value == "custom":
        context.user_data["awaiting_time"] = True
        await query.edit_message_text(t(lang, "custom_time_prompt"))
        return

    new_time = value
    conn = db()
    conn.execute(
        "INSERT INTO users (chat_id, morning_time, language, active) VALUES (?, ?, ?, 1) "
        "ON CONFLICT(chat_id) DO UPDATE SET morning_time=excluded.morning_time",
        (chat_id, new_time, lang),
    )
    conn.commit()
    conn.close()
    await query.edit_message_text(t(lang, "time_set", time=new_time))


# ---------- Atbilžu apstrāde ----------

async def _save_answer_and_advance(chat_id, date, idx, lang, answer_text, is_voice, context):
    questions = QUESTIONS.get(lang, QUESTIONS[DEFAULT_LANGUAGE])
    conn = db()
    conn.execute(
        "INSERT INTO answers (chat_id, date, question_index, question_text, "
        "answer_text, is_voice, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            chat_id,
            date,
            idx,
            questions[idx],
            answer_text,
            1 if is_voice else 0,
            datetime.now(TIMEZONE).isoformat(),
        ),
    )
    next_idx = idx + 1
    if next_idx >= len(questions):
        conn.execute(
            "UPDATE sessions SET current_index=?, done=1 WHERE chat_id=? AND date=?",
            (next_idx, chat_id, date),
        )
        conn.commit()
        conn.close()
        await context.bot.send_message(chat_id=chat_id, text=t(lang, "thanks_saved"))
        await context.bot.send_message(chat_id=chat_id, text=format_entry(chat_id, date, lang))
    else:
        conn.execute(
            "UPDATE sessions SET current_index=? WHERE chat_id=? AND date=?",
            (next_idx, chat_id, date),
        )
        conn.commit()
        conn.close()
        await send_question(chat_id, next_idx, lang, context)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text
    lang = get_user_language(chat_id)

    action = BUTTON_ACTIONS.get(text)
    if action:
        context.user_data["awaiting_time"] = False
    if action == "start_now":
        await cmd_tagad(update, context)
        return
    if action == "today":
        await send_today_entry(chat_id, context)
        return
    if action == "history":
        await send_history_entries(chat_id, 7, context)
        return
    if action == "time":
        await update.message.reply_text(t(lang, "choose_time"), reply_markup=time_menu_keyboard(lang))
        return
    if action == "help":
        await cmd_help(update, context)
        return
    if action == "language":
        await prompt_language_change(chat_id, context)
        return

    if context.user_data.get("awaiting_time"):
        if re.match(r"^\d{1,2}:\d{2}$", text):
            h, m = map(int, text.split(":"))
            if 0 <= h < 24 and 0 <= m < 60:
                new_time = f"{h:02d}:{m:02d}"
                conn = db()
                conn.execute(
                    "INSERT INTO users (chat_id, morning_time, language, active) VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(chat_id) DO UPDATE SET morning_time=excluded.morning_time",
                    (chat_id, new_time, lang),
                )
                conn.commit()
                conn.close()
                context.user_data["awaiting_time"] = False
                await update.message.reply_text(t(lang, "time_set", time=new_time))
                return
        await update.message.reply_text(t(lang, "invalid_time_format"))
        return

    conn = db()
    row = conn.execute(
        "SELECT date, current_index FROM sessions WHERE chat_id=? AND done=0 "
        "ORDER BY date DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    conn.close()
    if not row:
        await update.message.reply_text(t(lang, "no_active_question"))
        return
    date, idx = row
    await _save_answer_and_advance(chat_id, date, idx, lang, text, False, context)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang = get_user_language(chat_id)
    conn = db()
    row = conn.execute(
        "SELECT date, current_index FROM sessions WHERE chat_id=? AND done=0 "
        "ORDER BY date DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    conn.close()
    if not row:
        await update.message.reply_text(t(lang, "no_active_question"))
        return
    if not openai_client:
        await update.message.reply_text(t(lang, "voice_not_configured"))
        return
    date, idx = row
    tg_file = await context.bot.get_file(update.message.voice.file_id)
    os.makedirs("tmp", exist_ok=True)
    ogg_path = f"tmp/{chat_id}_{idx}.oga"
    await tg_file.download_to_drive(ogg_path)
    await update.message.reply_text(t(lang, "transcribing"))
    try:
        text = await transcribe_voice(ogg_path, lang)
    except Exception as e:
        logger.exception("Transkripcijas kļūda")
        await update.message.reply_text(t(lang, "transcription_error", error=e))
        return
    await update.message.reply_text(t(lang, "recognized_text", text=text))
    await _save_answer_and_advance(chat_id, date, idx, lang, text, True, context)


# ---------- Palaišana ----------

# Komandas, kas redzamas Telegram "/" ieteikumu sarakstā (pēc lietotāja valodas).
# /reset un /next apzināti NAV šeit iekļautas — tās joprojām strādā, ja tās
# uzraksta ar roku, bet nav publiski redzamas.
PUBLIC_COMMANDS = [
    ("start", {"lv": "Sākt / restartēt botu", "en": "Start / restart the bot", "ru": "Запустить / перезапустить бота"}),
    ("tagad", {"lv": "Sākt šodienas jautājumus", "en": "Start today's questions", "ru": "Начать сегодняшние вопросы"}),
    ("sodien", {"lv": "Šodienas ieraksts", "en": "Today's entry", "ru": "Запись за сегодня"}),
    ("vesture", {"lv": "Pēdējo dienu ieraksti", "en": "Recent entries", "ru": "Последние записи"}),
    ("laiks", {"lv": "Mainīt jautājumu laiku", "en": "Change question time", "ru": "Изменить время вопросов"}),
    ("valoda", {"lv": "Mainīt valodu", "en": "Change language", "ru": "Сменить язык"}),
    ("palidziba", {"lv": "Palīdzība", "en": "Help", "ru": "Помощь"}),
]


async def setup_commands(application):
    for lang_code in ("lv", "en", "ru"):
        commands = [BotCommand(cmd, desc[lang_code]) for cmd, desc in PUBLIC_COMMANDS]
        await application.bot.set_my_commands(commands, language_code=lang_code)
    default_commands = [BotCommand(cmd, desc["en"]) for cmd, desc in PUBLIC_COMMANDS]
    await application.bot.set_my_commands(default_commands)


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(setup_commands).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("palidziba", cmd_help))
    app.add_handler(CommandHandler("laiks", cmd_laiks))
    app.add_handler(CommandHandler("tagad", cmd_tagad))
    app.add_handler(CommandHandler("sodien", cmd_sodien))
    app.add_handler(CommandHandler("vesture", cmd_vesture))
    app.add_handler(CommandHandler("valoda", cmd_valoda))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CallbackQueryHandler(language_callback, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(settime_callback, pattern=r"^settime:"))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.job_queue.run_repeating(check_and_trigger, interval=60, first=5)

    logger.info("Bots startē...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
