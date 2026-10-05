"""
Ikdienas refleksijas Telegram bots (daudzvalodu: LV / EN / RU).

Katru dienu divas sesijas: rīta jautājumi (pateicība, kas ES esmu, ieviešamais,
galvenais uzdevums, prioritātes) un vakara jautājumi (rituāli, kas jauns,
pateicība, palīdzība citiem, iemācītais, atziņas). Katrai sesijai savs
atgādinājuma laiks, bet "Sākt tagad" pēc pulksteņa izvēlas, kuru sesiju palaist.
Lietotājs atbild ar tekstu vai balsi (balss tiek pārvērsta tekstā ar OpenAI
Whisper API), un visas atbildes tiek saglabātas SQLite datubāzē kā ikdienas
dienasgrāmatas ieraksts. Katrs lietotājs var izvēlēties savu saskarnes valodu
neatkarīgi no citiem.
"""

import os
import re
import json
import random
import sqlite3
import logging
from datetime import datetime, timedelta
from html import escape as html_escape
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
DEFAULT_EVENING_TIME = os.environ.get("DEFAULT_EVENING_TIME", "21:00")
# "Sākt tagad": pirms šīs pulksteņa stundas tiek piedāvāts rīts, no tās — vakars
SESSION_SPLIT_HOUR = 16
# Plānotais atgādinājums vēl tiek sūtīts, ja plānotais laiks pagājis ne vairāk par
# tik minūtēm (lai bota pārstartēšana vai izlaista minūte neizraisītu zaudējumu)
REMINDER_WINDOW_MIN = 10
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

# ---------- Jautājumi: rīts un vakars ----------
# Katram periodam ("morning" / "evening") savi jautājumi katrā valodā.
# Jautājumi ir pirmajā personā ("ES") — kā saruna ar sevi; paskaidrojumi
# ("Pārdomai") ir uz "tu".

PERIODS = ("morning", "evening")

QUESTIONS = {
    "morning": {
        "lv": [
            "Par ko ES varu būt pateicīgs šorīt?",
            "Kas ES esmu šodien?",
            "Ko ES šodien savā dzīvē ieviesīšu vai vēlos ieviest (un kas manu šodienu padarīs izdevušos)?",
            "Kāds ir mans šīs dienas pats galvenais uzdevums?",
            "5 prioritātes un vienkārši uzdevumi, kuri man šodienas laikā ir jāpaveic.",
        ],
        "en": [
            "What can I be grateful for this morning?",
            "Who am I today?",
            "What will I bring into my life today, or want to bring (and what will make my day a success)?",
            "What is my single most important task of this day?",
            "5 priorities and simple tasks I need to get done today.",
        ],
        "ru": [
            "За что Я могу поблагодарить жизнь этим утром?",
            "Кто Я сегодня?",
            "Что Я сегодня внесу в свою жизнь или хочу внести (и что сделает мой день удавшимся)?",
            "Какая моя самая главная задача на сегодня?",
            "5 приоритетов и простых задач, которые мне нужно выполнить в течение дня.",
        ],
    },
    "evening": {
        "lv": [
            "Kādus rituālus un ikdienas darbus ES šodien paveicu?",
            "Kas jauns šodien noticis manā dzīvē?",
            "Kam ES šodien varēju būt pateicīgs? Par ko varu pateikties?",
            "Kam ES palīdzēju, pateicu labus vārdus vai veltīju nedalītu uzmanību?",
            "Ko ES šodien iemācījos? Ko ES gribētu iemācīties vai attīstīt sevī papildus?",
            "Ko ES apzinājos vai sapratu, atbildot uz šiem dienas jautājumiem?",
        ],
        "en": [
            "Which rituals and everyday tasks did I get done today?",
            "What's new that happened in my life today?",
            "What could I have been grateful for today? Who can I say thank you to?",
            "Who did I help, say kind words to, or give my undivided attention?",
            "What did I learn today? What would I like to learn or develop in myself?",
            "What did I realize or understand while answering these questions of the day?",
        ],
        "ru": [
            "Какие ритуалы и повседневные дела у меня сегодня получились?",
            "Что нового произошло сегодня в моей жизни?",
            "Кому и за что Я могу сегодня сказать спасибо?",
            "Кому сегодня достались моя помощь, добрые слова или нераздельное внимание?",
            "Чему меня научил этот день? Чему хочу научиться или что развить в себе дополнительно?",
            "Какой ответ или какое понимание пришло ко мне, пока Я отвечаю на эти вопросы дня?",
        ],
    },
}

# Īsi "iesildīšanas" teksti katram jautājumam — palīdz cilvēkam apstāties un
# padomāt dziļāk, pirms atbild. Rādās zem paša jautājuma. Secība sakrīt ar QUESTIONS.
REFLECTION_PROMPTS = {
    "morning": {
        "lv": [
            "Sāc ar mazām, konkrētām lietām — tējas krūze, silta gulta, kāds ziņojums, "
            "kas tevi iepriecināja. Konkrētais strādā labāk par vispārīgo: nevis «par "
            "visu», bet par to, ko tu šorīt tiešām vari ieraudzīt vai sajust.",
            "Pajautā sev, kur tu šorīt atrodies — kā jūties, kāds gribi būt šodien un "
            "ko ienes pasaulē. Vari atbildēt ar vienu vārdu, lomu vai sajūtu — "
            "piemēram, miers, drosme, uzmanība, atbalsts citiem.",
            "Padomā par vienu jaunu ieradumu, domu vai rīcību, ko šodien vari ienest "
            "savā dzīvē — pat sīku. Un pajautā: kas mainīsies, ja es to izdarīšu? Kas "
            "liks šai dienai justies izdevušai?",
            "No visa, kas gaida, izvēlies vienu lietu, kuru paveicot, diena jau būs "
            "vērtīga. Ja šodien izdarītu tikai to, vai diena justos izdevusies? Tas "
            "arī ir tavs galvenais uzdevums.",
            "Uzraksti vienā ziņā līdz piecām konkrētām, vienkāršām darbībām, kas tev "
            "šodien jāpaveic — katru savā rindā vai atdalot ar komatu. Labi uzdevumi "
            "ir tādi, ko vakarā var vienkārši atzīmēt kā izdarītus vai neizdarītus.",
        ],
        "en": [
            "Start small and concrete — a warm cup of tea, a comfortable bed, a message "
            "that made you smile. Specific things work better than general ones: not "
            "«everything», but what you can actually see or feel this morning.",
            "Ask yourself where you are this morning — how you feel, who you want to be "
            "today, what you bring into the world. You can answer with a single word, "
            "role or feeling — calm, focus, courage, support for others.",
            "Think of one new habit, thought or action you can bring into your life "
            "today — even a tiny one. Then ask: what changes if I do it? What would "
            "make this day feel successful?",
            "Out of everything waiting for you, pick the one thing that, once done, "
            "makes the day worthwhile. If that were the only thing you finished today, "
            "would the day still feel successful? That's your main task.",
            "In one message, write up to five concrete, simple actions you need to "
            "complete today — one per line or separated by commas. Good tasks are ones "
            "you can simply tick off tonight as done or not done.",
        ],
        "ru": [
            "Начни с маленького и конкретного — чашка чая, тёплая постель, сообщение, "
            "которое порадовало. Конкретное работает лучше общего: не «за всё», а за "
            "то, что ты действительно можешь увидеть или почувствовать этим утром.",
            "Спроси себя, где ты этим утром находишься — как себя чувствуешь, каким "
            "хочешь быть сегодня, что приносишь в мир. Можно ответить одним словом, "
            "ролью или ощущением — спокойствие, смелость, внимательность, поддержка "
            "для других.",
            "Подумай об одной новой привычке, мысли или действии, которое можно "
            "привнести в жизнь сегодня — пусть даже крошечном. И спроси себя: что "
            "изменится, если я это сделаю? Что сделает этот день удавшимся?",
            "Из всего, что ждёт, выбери одно дело, после которого день уже не будет "
            "напрасным. Если бы сегодня удалось сделать только это, день всё равно "
            "казался бы удавшимся? Это и есть главная задача.",
            "Одним сообщением запиши до пяти конкретных простых действий, которые "
            "нужно выполнить сегодня — каждое с новой строки или через запятую. "
            "Хорошая задача — та, которую вечером можно просто отметить как "
            "сделанную или нет.",
        ],
    },
    "evening": {
        "lv": [
            "Atskaties uz dienas ierastajām lietām — rīta un vakara rituāliem, mājas "
            "darbiem, ēdienreizēm, kustībām, kārtības uzturēšanu. Kas no tā šodien "
            "izdevās, kas ne, un kā tas gāja — viegli, ar pūlēm vai gandrīz nemanot?",
            "Kas šodien bija citādāk nekā parasti? Pārmaiņa, negaidīta tikšanās, labs "
            "notikums, jauna pieredze, piedzīvojums — pat sīks pavērsiens dienas gaitā. "
            "Pietiek ar to, kas tavā dzīvē šodien parādījās pirmo reizi.",
            "Skaties uz dienu kā uz stāstu — kas tajā bija tāds, par ko vērts pateikt "
            "paldies? Tas var būt cilvēks, notikums vai sīkums. Padomā arī, kam "
            "konkrēti to varētu pateikt skaļi.",
            "Atceries brīžus, kad tu kādam biji blakus — palīdzēji, pateici labu vārdu "
            "vai patiešām biji klāt bez steigas un telefona. Tas var būt tuvs cilvēks "
            "vai nejaušs garāmgājējs. Ja šodien tādu brīdi neatrodi, tā ir atbilde "
            "rītdienai.",
            "Pajautā, ko šī diena tev iemācīja — par pasauli, citiem vai sevi. Un "
            "otrādi: ko tu gribētu iemācīties vai attīstīt sevī papildus — nav "
            "obligāti kaut kas liels, pietiek ar vienu nelielu virzienu.",
            "Atskaties uz to, ko šodien pierakstīji. Vai, atbildot uz šiem jautājumiem, "
            "kaut kas kļuva skaidrāks — atbilde, jauna doma, atziņa, ko iepriekš "
            "nepamanīji? Ieraksti to, pat ja tā ir tikai viena frāze.",
        ],
        "en": [
            "Look back at the everyday things — morning and evening rituals, chores, "
            "meals, movement, keeping things in order. What worked today, what didn't, "
            "and how did it feel — easy, effortful, or almost unnoticed?",
            "What was different today from the usual? A change, an unexpected meeting, "
            "something good that happened, a new experience, an adventure — even a "
            "small turn in the course of the day. Whatever showed up in your life for "
            "the first time today is enough.",
            "Look at the day like a story — what in it was worth saying thank you for? "
            "It could be a person, an event or a small thing. Also think of who exactly "
            "you could say it to out loud.",
            "Recall the moments when you were there for someone — you helped, said a "
            "kind word, or truly were present without rushing or your phone. It could "
            "be someone close or a stranger. If you can't find such a moment today, "
            "that is also an answer for tomorrow.",
            "Ask what this day taught you — about the world, others or yourself. And "
            "the other side: what would you like to learn or develop in yourself — it "
            "doesn't have to be big, one small direction is enough.",
            "Look back at what you wrote today. Did anything become clearer while "
            "answering these questions — an answer, a new thought, an insight you "
            "hadn't noticed before? Write it down, even if it's just one sentence.",
        ],
        "ru": [
            "Оглянись на повседневные дела — утренние и вечерние ритуалы, домашние "
            "заботы, еду, движение, порядок вокруг. Что сегодня получилось, что нет, "
            "и как это далось — легко, с усилием или почти незаметно?",
            "Что сегодня было не так, как обычно? Перемена, неожиданная встреча, "
            "что-то хорошее, новый опыт, приключение — даже небольшой поворот в "
            "течение дня. Достаточно того, что сегодня появилось в твоей жизни "
            "впервые.",
            "Посмотри на день как на историю — что в нём было достойно слов «спасибо»? "
            "Это может быть человек, событие или мелочь. Подумай и о том, кому именно "
            "это можно сказать вслух.",
            "Вспомни моменты, в которых нашлось место для другого человека: помощь, "
            "доброе слово, полное внимание без спешки и телефона. Это может быть "
            "близкий человек или случайный прохожий. Если сегодня такого момента не "
            "нашлось — это тоже ответ для завтрашнего дня.",
            "Спроси себя, чему тебя научил этот день — о мире, о других или о себе. И "
            "с другой стороны: чему хочется научиться или что развить в себе "
            "дополнительно — не обязательно что-то большое, достаточно одного "
            "небольшого направления.",
            "Оглянись на написанное сегодня. Стало ли что-то яснее, пока шли ответы "
            "на эти вопросы — ответ, новая мысль, понимание, которого раньше не "
            "замечалось? Запиши это, даже если получится одна фраза.",
        ],
    },
}

# Īsi tematiskie apzīmējumi katram jautājumam (emocijzīme, teksts) — lieto
# šodienas/vēstures ieraksta kompaktajā attēlošanā. Secība sakrīt ar QUESTIONS.
TOPIC_LABELS = {
    "morning": {
        "lv": [("🙏", "Pateicība"), ("🧭", "Kas ES esmu"), ("🌱", "Ko ieviesīšu"), ("🎯", "Galvenais uzdevums"), ("✅", "Prioritātes")],
        "en": [("🙏", "Gratitude"), ("🧭", "Who I am"), ("🌱", "What I'll bring"), ("🎯", "Main task"), ("✅", "Priorities")],
        "ru": [("🙏", "Благодарность"), ("🧭", "Кто Я"), ("🌱", "Что внесу"), ("🎯", "Главная задача"), ("✅", "Приоритеты")],
    },
    "evening": {
        "lv": [("🏠", "Rituāli un ikdiena"), ("✨", "Kas jauns"), ("🙏", "Pateicība"), ("🤝", "Palīdzība citiem"), ("📚", "Iemācījos"), ("💡", "Sapratu")],
        "en": [("🏠", "Rituals and routine"), ("✨", "What's new"), ("🙏", "Gratitude"), ("🤝", "Helping others"), ("📚", "Learned"), ("💡", "Realized")],
        "ru": [("🏠", "Ритуалы и быт"), ("✨", "Что нового"), ("🙏", "Благодарность"), ("🤝", "Помощь другим"), ("📚", "Уроки дня"), ("💡", "Осознание")],
    },
}

# Vecie (pirms rīta/vakara sadalījuma) jautājumu apzīmējumi — tikai vecu ierakstu
# attēlošanai vēsturē. Jaunie ieraksti tos vairs neizmanto.
LEGACY_TOPIC_LABELS = {
    "lv": [("🙏", "Pateicība"), ("🏆", "Uzvara"), ("⚠️", "Problēma"), ("🎯", "Nodoms"), ("🌌", "Jautājums Visumam")],
    "en": [("🙏", "Gratitude"), ("🏆", "Win"), ("⚠️", "Challenge"), ("🎯", "Intention"), ("🌌", "Question to the Universe")],
    "ru": [("🙏", "Благодарность"), ("🏆", "Победа"), ("⚠️", "Проблема"), ("🎯", "Намерение"), ("🌌", "Вопрос Вселенной")],
}

# Animēto (Premium) emocijzīmju ID — lieto HTML <tg-emoji> tagā. Ja kāda emocijzīme
# šeit nav uzskaitīta, tg_emoji() vienkārši atgriež parasto (statisko) versiju.
CUSTOM_EMOJI_IDS = {
    "📓": "5197269100878907942",
    "🙏": "5382319231410904354",
    "🏆": "5188344996356448758",
    "⚠️": "5447644880824181073",
    "🎯": "5310278924616356636",
    "🌌": "5217818964612108191",
    "✨": "5451636889717062286",
    "💡": "5422439311196834318",
    "🌙": "5208554136039073738",
    "🏠": "5416041192905265756",
    "🤝": "5395732581780040886",
    "📚": "5258046117932711905",
    "🧭": "5213107179329953547",
    "🌱": "5474417568053745249",
    "✅": "5332533929020761310",
    "🌅": "5402477260982731644",
    "❓": "5382187118216879236",
}


def tg_emoji(char):
    """Ietin emocijzīmi <tg-emoji> tagā, ja tai zināms ID (animēta versija HTML
    ziņās) — citādi atgriež to pašu parasto emocijzīmi bez izmaiņām."""
    emoji_id = CUSTOM_EMOJI_IDS.get(char)
    if not emoji_id:
        return char
    return f'<tg-emoji emoji-id="{emoji_id}">{char}</tg-emoji>'

# Pilni valodu nosaukumi angliski — lieto AI uzmundrinājuma prompta instrukcijā
LANGUAGE_NAMES_FOR_PROMPT = {"lv": "Latvian", "en": "English", "ru": "Russian"}

# Sākotnējās "padoma" frāzes — izmanto 💡 Padoms pogai. Tiek ielādētas
# datubāzes "advice" tabulā vienreiz (pirmajā palaišanas reizē).
ADVICE_SEED = {
    "lv": [
        "Nedari šodien to, ko varēsi nožēlot rīt no rīta.",
        "Mazi soļi katru dienu uzveic lielus lēcienus reizi gadā.",
        "Tava nākamā versija tevi vēro. Rādi labu piemēru.",
        "Atpūta nav slinkums — tā ir daļa no plāna.",
        "Ja kaut kas neizdodas trīs reizes, tas nav zīme apstāties — tā ir zīme mainīt pieeju.",
        "Nesalīdzini savu 1. nodaļu ar kāda cita 20. nodaļu.",
        "Disciplīna ir mīlestība pret sevi nākotnē.",
        "Neviens neatceras, cik ātri tu sāki. Visi atceras, vai tu pabeidzi.",
        "Skaidrība nāk no darbības, ne no domāšanas.",
        "Tava komforta zona ir skaista vieta, kur nekas neizaug.",
        "Vienalga, cik lēni tu ej — tu joprojām apsteidz visus, kas sēž.",
        "Ideāls plāns, kas nekad nesākas, ir sliktāks par vidēju plānu, kas sākas šodien.",
        "Neprasi sev, vai tu vari. Prasi sev, vai tu esi gatavs mēģināt.",
        "Katra diena, kurā tu iemācies kaut ko jaunu par sevi, nav velti pavadīta diena.",
        "Vājākā versija no tevis grib atlikt. Stiprākā versija jau ir sākusi.",
    ],
    "en": [
        "Don't do today what you'll regret tomorrow morning.",
        "Small steps daily beat big leaps once a year.",
        "Your future self is watching. Set a good example.",
        "Rest isn't laziness — it's part of the plan.",
        "If something fails three times, that's not a sign to stop — it's a sign to change your approach.",
        "Don't compare your chapter 1 to someone else's chapter 20.",
        "Discipline is love for your future self.",
        "No one remembers how fast you started. Everyone remembers if you finished.",
        "Clarity comes from action, not from thinking.",
        "Your comfort zone is a beautiful place where nothing grows.",
        "No matter how slow you're going, you're still lapping everyone on the couch.",
        "A perfect plan that never starts is worse than an average plan that starts today.",
        "Don't ask yourself if you can. Ask yourself if you're willing to try.",
        "Any day you learn something new about yourself isn't a wasted day.",
        "The weakest version of you wants to postpone. The strongest version has already started.",
    ],
    "ru": [
        "Не делай сегодня того, о чём пожалеешь завтра утром.",
        "Маленькие шаги каждый день побеждают большие скачки раз в год.",
        "Твоя будущая версия наблюдает за тобой. Подай хороший пример.",
        "Отдых — не лень, а часть плана.",
        "Если что-то не получается три раза — это не знак остановиться, а знак изменить подход.",
        "Не сравнивай свою главу 1 с чужой главой 20.",
        "Дисциплина — это любовь к своему будущему себе.",
        "Никто не помнит, как быстро ты начал. Все помнят, довёл ли ты до конца.",
        "Ясность приходит от действия, а не от размышлений.",
        "Зона комфорта — красивое место, где ничего не растёт.",
        "Как бы медленно ты ни шёл, ты всё равно обгоняешь тех, кто сидит на диване.",
        "Идеальный план, который никогда не начинается, хуже среднего плана, который начинается сегодня.",
        "Не спрашивай себя, можешь ли ты. Спроси, готов ли попробовать.",
        "Любой день, когда ты узнаёшь о себе что-то новое, не прожит зря.",
        "Самая слабая версия тебя хочет отложить. Самая сильная — уже начала.",
    ],
}

# Nejaušas ziņas, kad padoms pieprasīts pirms 4h intervāla beigām — bez
# precīza pulksteņa laika, dažādas katru reizi.
ADVICE_COOLDOWN_MESSAGES = {
    "lv": [
        "Nesen jau prasīji padomu. Rīkojies — atgriezies vēlāk.",
        "Padoms jau tev rokā. Izmanto to, pirms prasi nākamo.",
        "Pacietība. Labs padoms strādā tikai tad, ja tam dod laiku.",
        "Vēl ne. Vispirms izmēģini to, ko jau dabūji.",
        "Vēl par agru jaunam padomam. Šis vēl strādā.",
    ],
    "en": [
        "You already asked recently. Go act on it — come back later.",
        "You've already got one. Use it before asking for another.",
        "Patience. Advice only works if you give it time.",
        "Not yet. Try the one you already have first.",
        "Too soon for a new one. This one's still working.",
    ],
    "ru": [
        "Ты уже недавно спрашивал. Иди действуй — вернись позже.",
        "У тебя уже есть совет. Используй его, прежде чем просить следующий.",
        "Терпение. Совет работает, только если дать ему время.",
        "Ещё нет. Сначала попробуй то, что уже получил.",
        "Ещё рано для нового. Этот пока работает.",
    ],
}

# Lokalizēti nedēļas dienu un mēnešu nosaukumi (indekss 0 = pirmdiena / janvāris),
# lieto datuma formatēšanai bez gada, piem. "Otrdiena, 8. septembris".
WEEKDAYS = {
    "lv": ["Pirmdiena", "Otrdiena", "Trešdiena", "Ceturtdiena", "Piektdiena", "Sestdiena", "Svētdiena"],
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "ru": ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"],
}

MONTHS = {
    "lv": ["janvāris", "februāris", "marts", "aprīlis", "maijs", "jūnijs", "jūlijs",
           "augusts", "septembris", "oktobris", "novembris", "decembris"],
    "en": ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"],
    "ru": ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря"],
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
        "btn_advice": "💡 Padoms",
        "greeting_morning": "🌅 Labrīt! Laiks rīta jautājumiem. Atbildi ar tekstu vai balss ziņu.",
        "greeting_evening": "🌙 Labvakar! Laiks vakara jautājumiem. Atbildi ar tekstu vai balss ziņu.",
        "resume_note": "Turpinām!",
        "day_word": "diena",
        "period_morning": "🌅 Rīts",
        "period_evening": "🌙 Vakars",
        "reflection_label": "Pārdomai",
        "no_active_question": "Šobrīd nav aktīva jautājuma. Nospied \"▶️ Sākt tagad\", lai sāktu šodienas refleksiju.",
        "times_overview": "Pašreizējie laiki:\n🌅 Rīts — {morning}\n🌙 Vakars — {evening}\n\nKuru laiku mainīt?",
        "choose_time": "{period}: pašlaik {current}.\n\nIzvēlies jaunu laiku:",
        "time_set": "{period}: laiks iestatīts uz {time}.",
        "custom_time_label": "✏️ Ievadīt pats",
        "custom_time_prompt": "{period}: ieraksti vēlamo laiku formātā HH:MM (piemēram, 19:30).",
        "invalid_time_format": "Nederīgs formāts. Ieraksti, piemēram, 19:30.",
        "transcribing": "Transkribēju balss ziņu...",
        "recognized_text": "Atpazīts teksts: {text}",
        "transcription_error": "Neizdevās transkribēt: {error}",
        "voice_not_configured": "Balss transkripcija nav konfigurēta.",
        "thanks_morning": "Paldies! Rīta ieraksts saglabāts. Lai tev laba diena! 🌅",
        "thanks_evening": "Paldies! Vakara ieraksts saglabāts. Mierīgu vakaru un labu nakti! 🌙",
        "already_done_morning": "Rīta jautājumi šodien jau pabeigti! Lūk, ko šodien pierakstīji:",
        "already_done_evening": "Vakara jautājumi šodien jau pabeigti! Lūk, ko šodien pierakstīji:",
        "no_entries": "Vēl nav neviena ieraksta.",
        "no_entries_for_date": "Nav ierakstu par {date}.",
        "today_label": "Šodien",
        "yesterday_label": "Vakar",
        "intro": (
            "Divreiz dienā — rītā plkst. {morning} un vakarā plkst. {evening} — es tev "
            "uzdošu jautājumus sev: rītā par pateicību, šodienas galveno uzdevumu un "
            "prioritātēm, vakarā par paveikto, jauno un iemācīto. Atbildi ar tekstu "
            "vai balss ziņu. Viss apkopojas dienas ierakstā, ko vari jebkurā laikā "
            "palasīt ar 📓 Šodien vai 📅 Vēsture."
        ),
        "help": (
            "Lieto pogas apakšā:\n"
            "▶️ Sākt tagad — sākt rīta vai vakara jautājumus (pēc pulksteņa: līdz {split} rīta, vēlāk vakara)\n"
            "📓 Šodien — šodienas ieraksts\n"
            "📅 Vēsture — pēdējo dienu ieraksti\n"
            "⏰ Mainīt laiku — mainīt rīta un vakara laiku\n"
            "💡 Padoms — īss padoms (reizi 4 stundās)\n"
            "🌐 Valoda — mainīt valodu\n\n"
            "Dienas skaitītājs, piemēram, 12/9, nozīmē: 12. diena kopš sākuma, "
            "un 9 no šīm dienām ir ieraksti."
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
        "btn_advice": "💡 Advice",
        "greeting_morning": "🌅 Good morning! Time for your morning questions. Reply with text or a voice message.",
        "greeting_evening": "🌙 Good evening! Time for your evening questions. Reply with text or a voice message.",
        "resume_note": "Let's continue!",
        "day_word": "day",
        "period_morning": "🌅 Morning",
        "period_evening": "🌙 Evening",
        "reflection_label": "Something to consider",
        "no_active_question": "There's no active question right now. Tap \"▶️ Start now\" to begin today's reflection.",
        "times_overview": "Current times:\n🌅 Morning — {morning}\n🌙 Evening — {evening}\n\nWhich one do you want to change?",
        "choose_time": "{period}: currently {current}.\n\nChoose a new time:",
        "time_set": "{period}: time set to {time}.",
        "custom_time_label": "✏️ Enter manually",
        "custom_time_prompt": "{period}: type the time you'd like in HH:MM format (e.g. 19:30).",
        "invalid_time_format": "Invalid format. Please type e.g. 19:30.",
        "transcribing": "Transcribing your voice message...",
        "recognized_text": "Recognized text: {text}",
        "transcription_error": "Transcription failed: {error}",
        "voice_not_configured": "Voice transcription isn't configured.",
        "thanks_morning": "Thanks! Your morning entry is saved. Have a great day! 🌅",
        "thanks_evening": "Thanks! Your evening entry is saved. Have a calm evening and a good night! 🌙",
        "already_done_morning": "Today's morning questions are already done! Here's what you wrote today:",
        "already_done_evening": "Today's evening questions are already done! Here's what you wrote today:",
        "no_entries": "No entries yet.",
        "no_entries_for_date": "No entries for {date}.",
        "today_label": "Today",
        "yesterday_label": "Yesterday",
        "intro": (
            "Twice a day — in the morning at {morning} and in the evening at {evening} — "
            "I'll ask you questions to ask yourself: in the morning about gratitude, "
            "your main task and priorities; in the evening about what you did, what's "
            "new and what you learned. Reply with text or a voice message. Everything "
            "compiles into a daily entry you can read back anytime with 📓 Today or "
            "📅 History."
        ),
        "help": (
            "Use the buttons below:\n"
            "▶️ Start now — begin the morning or evening questions (by the clock: morning before {split}, evening after)\n"
            "📓 Today — today's entry\n"
            "📅 History — past entries\n"
            "⏰ Change time — change your morning and evening times\n"
            "💡 Advice — a short piece of advice (once every 4 hours)\n"
            "🌐 Language — change language\n\n"
            "The day counter, e.g. 12/9, means: day 12 since you started, "
            "and 9 of those days have entries."
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
        "btn_advice": "💡 Совет",
        "greeting_morning": "🌅 Доброе утро! Время утренних вопросов. Ответь текстом или голосовым сообщением.",
        "greeting_evening": "🌙 Добрый вечер! Время вечерних вопросов. Ответь текстом или голосовым сообщением.",
        "resume_note": "Продолжаем!",
        "day_word": "день",
        "period_morning": "🌅 Утро",
        "period_evening": "🌙 Вечер",
        "reflection_label": "Для размышления",
        "no_active_question": "Сейчас нет активного вопроса. Нажми «▶️ Начать сейчас», чтобы начать сегодняшнюю рефлексию.",
        "times_overview": "Текущее время:\n🌅 Утро — {morning}\n🌙 Вечер — {evening}\n\nКакое время изменить?",
        "choose_time": "{period}: сейчас {current}.\n\nВыбери новое время:",
        "time_set": "{period}: время установлено на {time}.",
        "custom_time_label": "✏️ Ввести вручную",
        "custom_time_prompt": "{period}: введи нужное время в формате ЧЧ:ММ (например, 19:30).",
        "invalid_time_format": "Неверный формат. Введи, например, 19:30.",
        "transcribing": "Расшифровываю голосовое сообщение...",
        "recognized_text": "Распознанный текст: {text}",
        "transcription_error": "Не удалось расшифровать: {error}",
        "voice_not_configured": "Расшифровка голоса не настроена.",
        "thanks_morning": "Спасибо! Утренняя запись сохранена. Хорошего дня! 🌅",
        "thanks_evening": "Спасибо! Вечерняя запись сохранена. Спокойного вечера и доброй ночи! 🌙",
        "already_done_morning": "Утренние вопросы на сегодня уже пройдены! Вот твои записи за сегодня:",
        "already_done_evening": "Вечерние вопросы на сегодня уже пройдены! Вот твои записи за сегодня:",
        "no_entries": "Пока нет записей.",
        "no_entries_for_date": "Нет записей за {date}.",
        "today_label": "Сегодня",
        "yesterday_label": "Вчера",
        "intro": (
            "Дважды в день — утром в {morning} и вечером в {evening} — я буду "
            "задавать тебе вопросы к самому себе: утром о благодарности, главной "
            "задаче и приоритетах; вечером о том, что сделано, что нового и чему "
            "научил день. Отвечай текстом или голосовым сообщением. Всё собирается "
            "в запись за день, которую можно читать в любое время с помощью "
            "📓 Сегодня или 📅 История."
        ),
        "help": (
            "Используй кнопки внизу:\n"
            "▶️ Начать сейчас — начать утренние или вечерние вопросы (по времени: до {split} утренние, позже вечерние)\n"
            "📓 Сегодня — запись за сегодня\n"
            "📅 История — прошлые записи\n"
            "⏰ Изменить время — изменить время утра и вечера\n"
            "💡 Совет — короткий совет (раз в 4 часа)\n"
            "🌐 Язык — сменить язык\n\n"
            "Счётчик дней, например 12/9, означает: 12-й день с начала, "
            "и в 9 из этих дней есть записи."
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
            [tx["btn_advice"]],
        ],
        resize_keyboard=True,
    )


def language_keyboard():
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"lang:{code}")]
        for code, label in LANGUAGES.items()
    ]
    return InlineKeyboardMarkup(buttons)


TIME_PRESETS = {
    "morning": ["06:30", "07:00", "07:30", "08:00", "08:30", "09:00"],
    "evening": ["19:30", "20:00", "20:30", "21:00", "21:30", "22:00"],
}


def period_picker_keyboard(lang):
    tx = TEXTS.get(lang, TEXTS[DEFAULT_LANGUAGE])
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(tx["period_morning"], callback_data="pickperiod:morning"),
                InlineKeyboardButton(tx["period_evening"], callback_data="pickperiod:evening"),
            ]
        ]
    )


def time_menu_keyboard(lang, period):
    tx = TEXTS.get(lang, TEXTS[DEFAULT_LANGUAGE])
    buttons = [
        InlineKeyboardButton(tm, callback_data=f"settime:{period}:{tm}")
        for tm in TIME_PRESETS[period]
    ]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append(
        [InlineKeyboardButton(tx["custom_time_label"], callback_data=f"settime:{period}:custom")]
    )
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
        mapping[tx["btn_advice"]] = "advice"
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
            evening_time TEXT NOT NULL DEFAULT '21:00',
            language TEXT NOT NULL DEFAULT 'lv',
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS sessions (
            chat_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            period TEXT NOT NULL DEFAULT 'legacy',
            current_index INTEGER NOT NULL DEFAULT 0,
            done INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, date, period)
        );
        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            period TEXT NOT NULL DEFAULT 'legacy',
            question_index INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            answer_text TEXT NOT NULL,
            is_voice INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS advice (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lang TEXT NOT NULL,
            phrase TEXT NOT NULL
        );
        """
    )
    # migrācijas esošām datubāzēm (ALTER met kļūdu, ja kolonna jau ir — to ignorējam)
    for statement in (
        "ALTER TABLE users ADD COLUMN language TEXT NOT NULL DEFAULT 'lv'",
        "ALTER TABLE users ADD COLUMN last_advice_at TEXT",
        "ALTER TABLE users ADD COLUMN advice_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN evening_time TEXT NOT NULL DEFAULT '21:00'",
        "ALTER TABLE answers ADD COLUMN period TEXT NOT NULL DEFAULT 'legacy'",
    ):
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    # sesiju tabulai jāmaina primārā atslēga (pievienojas period), tāpēc to pārbūvējam.
    # Vecās sesijas kļūst par 'legacy'; nepabeigtās vecās tiek dzēstas (to atbildes paliek).
    session_cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
    if "period" not in session_cols:
        conn.executescript(
            """
            ALTER TABLE sessions RENAME TO sessions_old;
            CREATE TABLE sessions (
                chat_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                period TEXT NOT NULL DEFAULT 'legacy',
                current_index INTEGER NOT NULL DEFAULT 0,
                done INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, date, period)
            );
            INSERT INTO sessions (chat_id, date, period, current_index, done)
                SELECT chat_id, date, 'legacy', current_index, done FROM sessions_old;
            DROP TABLE sessions_old;
            DELETE FROM sessions WHERE period = 'legacy' AND done = 0;
            """
        )
    # sākotnējais frāžu pildījums — tikai vienreiz, ja tabula vēl tukša
    count = conn.execute("SELECT COUNT(*) FROM advice").fetchone()[0]
    if count == 0:
        for lang, phrases in ADVICE_SEED.items():
            conn.executemany(
                "INSERT INTO advice (lang, phrase) VALUES (?, ?)",
                [(lang, p) for p in phrases],
            )
    conn.commit()
    conn.close()


def today_str():
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def period_for_now():
    """Pirms SESSION_SPLIT_HOUR — rīts, no tās — vakars (izmanto "Sākt tagad")."""
    return "morning" if datetime.now(TIMEZONE).hour < SESSION_SPLIT_HOUR else "evening"


def parse_hhmm(text):
    """Atgriež laiku formātā HH:MM vai None, ja ievade nav derīgs laiks."""
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text or "")
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if 0 <= hours < 24 and 0 <= minutes < 60:
        return f"{hours:02d}:{minutes:02d}"
    return None


def get_user_language(chat_id):
    conn = db()
    row = conn.execute("SELECT language FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    if row and row[0] in TEXTS:
        return row[0]
    return DEFAULT_LANGUAGE


def ensure_user(chat_id, lang=None):
    """Izveido lietotāju, ja tāda vēl nav. Esošam lietotājam neko nemaina —
    tāpēc atgriezušam lietotājam saglabājas viņa iepriekš izvēlētā valoda."""
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO users (chat_id, morning_time, evening_time, language, active) "
        "VALUES (?, ?, ?, ?, 1)",
        (chat_id, DEFAULT_MORNING_TIME, DEFAULT_EVENING_TIME, lang or DEFAULT_LANGUAGE),
    )
    conn.commit()
    conn.close()


def set_user_language(chat_id, lang):
    ensure_user(chat_id, lang)
    conn = db()
    conn.execute("UPDATE users SET language=? WHERE chat_id=?", (lang, chat_id))
    conn.commit()
    conn.close()


def get_user_times(chat_id):
    """Atgriež (rīta laiks, vakara laiks)."""
    conn = db()
    row = conn.execute(
        "SELECT morning_time, evening_time FROM users WHERE chat_id=?", (chat_id,)
    ).fetchone()
    conn.close()
    if row:
        return row[0], row[1]
    return DEFAULT_MORNING_TIME, DEFAULT_EVENING_TIME


def set_user_time(chat_id, period, hhmm):
    column = "morning_time" if period == "morning" else "evening_time"
    ensure_user(chat_id)
    conn = db()
    conn.execute(f"UPDATE users SET {column}=? WHERE chat_id=?", (hhmm, chat_id))
    conn.commit()
    conn.close()


def get_session(chat_id, date, period):
    """Atgriež (done, current_index) vai None, ja sesijas nav."""
    conn = db()
    row = conn.execute(
        "SELECT done, current_index FROM sessions WHERE chat_id=? AND date=? AND period=?",
        (chat_id, date, period),
    ).fetchone()
    conn.close()
    return row


def get_active_session(chat_id):
    """Pēdējā uzsāktā, vēl nepabeigtā sesija: (date, period, current_index) vai None."""
    conn = db()
    row = conn.execute(
        "SELECT date, period, current_index FROM sessions WHERE chat_id=? AND done=0 "
        "ORDER BY rowid DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    conn.close()
    return row


# ---------- Palīgfunkcijas ----------

def esc(text):
    """HTML izbēgšana Telegram HTML režīmam (pēdiņas nav jāizbēg teksta mezglos)."""
    return html_escape(str(text), quote=False)


async def send_question(chat_id, idx, period, lang, context: ContextTypes.DEFAULT_TYPE):
    questions = QUESTIONS[period].get(lang, QUESTIONS[period][DEFAULT_LANGUAGE])
    prompts = REFLECTION_PROMPTS[period].get(lang, REFLECTION_PROMPTS[period][DEFAULT_LANGUAGE])
    total = len(questions)
    reflection_label = t(lang, "reflection_label")
    text = (
        f"({idx + 1}/{total}) {esc(questions[idx])}\n\n"
        f"{tg_emoji('❓')} {esc(reflection_label)}: {esc(prompts[idx])}"
    )
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")


async def start_session(chat_id, context: ContextTypes.DEFAULT_TYPE, period, date=None):
    """Sāk rīta vai vakara jautājumu sesiju. Ja šim periodam šajā datumā sesija jau
    pastāv (pabeigta vai nē), neko nedara un atgriež False."""
    if date is None:
        date = today_str()
    conn = db()
    existing = conn.execute(
        "SELECT 1 FROM sessions WHERE chat_id=? AND date=? AND period=?",
        (chat_id, date, period),
    ).fetchone()
    if existing:
        conn.close()
        return False
    # Iepriekšējās nepabeigtās sesijas (piem., ignorēts rīta atgādinājums) tiek slēgtas,
    # lai jaunā atbilde nenonāk pie veca jautājuma. Jau saglabātās atbildes paliek.
    conn.execute("DELETE FROM sessions WHERE chat_id=? AND done=0", (chat_id,))
    conn.execute(
        "INSERT INTO sessions (chat_id, date, period, current_index, done) VALUES (?, ?, ?, 0, 0)",
        (chat_id, date, period),
    )
    conn.commit()
    conn.close()
    lang = get_user_language(chat_id)
    day_line = f"{full_date_label(date, lang)} · {day_suffix(chat_id, date, lang)}"
    await context.bot.send_message(
        chat_id=chat_id, text=f"{t(lang, 'greeting_' + period)}\n{day_line}"
    )
    await send_question(chat_id, 0, period, lang, context)
    return True


def _to_minutes(hhmm):
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


async def check_and_trigger(context: ContextTypes.DEFAULT_TYPE):
    """Palaižas ik minūti. Katram aktīvam lietotājam sāk rīta/vakara sesiju, kad pienācis
    viņa iestatītais laiks un šim periodam šodien vēl nav sesijas.

    Lietotājiem, kuriem jau ir vēsture, atgādinājums tiek sūtīts arī tad, ja plānotais
    laiks pagājis līdz REMINDER_WINDOW_MIN minūtēm (lai bota pārstartēšana vai izlaista
    minūte neizraisītu atgādinājuma zudumu). Pavisam jauniem lietotājiem — tikai pašā
    minūtē, lai reģistrējoties pēc noklusējuma laika viņi nesaņemtu sesiju uzreiz."""
    now = datetime.now(TIMEZONE)
    now_min = now.hour * 60 + now.minute
    date = today_str()
    conn = db()
    users = conn.execute(
        "SELECT chat_id, morning_time, evening_time FROM users WHERE active=1"
    ).fetchall()
    started_today = set(
        conn.execute("SELECT chat_id, period FROM sessions WHERE date=?", (date,)).fetchall()
    )
    has_history = {r[0] for r in conn.execute("SELECT DISTINCT chat_id FROM sessions").fetchall()}
    conn.close()
    for chat_id, morning_time, evening_time in users:
        window = REMINDER_WINDOW_MIN if chat_id in has_history else 1
        for period, hhmm in (("morning", morning_time), ("evening", evening_time)):
            try:
                diff = now_min - _to_minutes(hhmm)
            except (ValueError, AttributeError):
                continue
            if 0 <= diff < window and (chat_id, period) not in started_today:
                try:
                    await start_session(chat_id, context, period)
                except Exception:
                    logger.exception("Neizdevās sākt %s sesiju lietotājam %s", period, chat_id)


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


HISTORY_DAYS_FOR_ENCOURAGEMENT = 7

# Secība, kādā bloki tiek rādīti vienas dienas ierakstā
PERIOD_ORDER = {"legacy": 0, "morning": 1, "evening": 2}


def topic_label(period, idx, lang):
    """Atgriež (emocijzīme, teksts) jautājuma apzīmējumam konkrētā periodā."""
    table = LEGACY_TOPIC_LABELS if period == "legacy" else TOPIC_LABELS.get(period, {})
    labels = table.get(lang) or table.get(DEFAULT_LANGUAGE) or []
    if 0 <= idx < len(labels):
        return labels[idx]
    return ("❓", f"Q{idx + 1}")


def get_recent_answers_by_date(chat_id, days=HISTORY_DAYS_FOR_ENCOURAGEMENT):
    """Atgriež pēdējo N dienu atbildes kā {date: [(period, question_index, answer), ...]},
    sakārtotas hronoloģiski (vecākā -> jaunākā), katras dienas ietvaros rīts pirms vakara."""
    conn = db()
    dates = conn.execute(
        "SELECT DISTINCT date FROM answers WHERE chat_id=? ORDER BY date DESC LIMIT ?",
        (chat_id, days),
    ).fetchall()
    result = {}
    for (d,) in dates:
        rows = conn.execute(
            "SELECT period, question_index, answer_text FROM answers "
            "WHERE chat_id=? AND date=? ORDER BY id",
            (chat_id, d),
        ).fetchall()
        rows.sort(key=lambda r: (PERIOD_ORDER.get(r[0], 0), r[1]))
        result[d] = rows
    conn.close()
    return dict(sorted(result.items()))


async def generate_encouragement(chat_id, lang: str, period, date) -> str | None:
    """Ģenerē īsu, personalizētu ziņu pēc pabeigtas sesijas. Rītā — uzmundrinājums par
    šodienas galveno uzdevumu un prioritātēm; vakarā — apsveikums par paveikto, kas
    godīgi sasaistīts ar rīta nodomu. Atgriež None, ja OpenAI izsaukums neizdodas
    (piem., nav kredītu) — tad vienkārši izlaižam šo ziņu, negraujot pārējo plūsmu."""
    if not openai_client:
        return None
    language_name = LANGUAGE_NAMES_FOR_PROMPT.get(lang, "English")
    recent = get_recent_answers_by_date(chat_id)
    if not recent:
        return None
    blocks = []
    for d, entries in recent.items():
        label = "TODAY" if d == date else d
        lines = [f"({p}) {topic_label(p, idx, lang)[1]}: {a}" for p, idx, a in entries]
        blocks.append(f"[{label}]\n" + "\n".join(lines))
    context_lines = "\n\n".join(blocks)
    if period == "morning":
        task = (
            "It is MORNING and they just answered today's morning questions. Write a "
            "short message (2-4 sentences): (1) acknowledge something specific and real "
            "from TODAY's morning answers, such as what they are grateful for or who they "
            "say they are today, (2) warmly encourage them about TODAY's main task and "
            "priorities, referencing their actual words, (3) only if there is a genuine "
            "pattern or follow-through from earlier days, mention it briefly — never "
            "invent one. "
        )
    else:
        task = (
            "It is EVENING and they just answered today's evening questions. Write a "
            "short message (3-5 sentences): (1) congratulate them specifically on what "
            "they did, learned or experienced today, referencing their actual words, "
            "(2) if TODAY's morning answers contain a main task or priorities, briefly "
            "and honestly connect them with what the evening answers show — only what is "
            "evident, never invent progress, (3) close warmly with a wish for a calm "
            "evening and rest. "
        )
    system_prompt = (
        "You are a warm, genuine companion helping someone reflect on their day. "
        f"Below are their reflection answers from up to the last {HISTORY_DAYS_FOR_ENCOURAGEMENT} "
        "days, oldest first, each tagged with its session (morning or evening) and topic; "
        "today's entries are marked [TODAY]. "
        + task
        + "Be specific and genuine, not generic or saccharine. "
        f"No preamble, no greeting, just the message itself. Respond ONLY in {language_name}."
    )
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": context_lines},
            ],
            max_tokens=300,
            temperature=0.8,
        )
        return response.choices[0].message.content.strip()
    except Exception:
        logger.exception("Neizdevās ģenerēt uzmundrinājumu")
        return None


def full_date_label(date_str, lang):
    """Datums ar nedēļas dienu, bez gada, piem. "Otrdiena, 8. septembris"."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return date_str
    weekday = WEEKDAYS.get(lang, WEEKDAYS[DEFAULT_LANGUAGE])[d.weekday()]
    month = MONTHS.get(lang, MONTHS[DEFAULT_LANGUAGE])[d.month - 1]
    if lang == "en":
        return f"{weekday}, {month} {d.day}"
    if lang == "ru":
        return f"{weekday}, {d.day} {month}"
    return f"{weekday}, {d.day}. {month}"


def relative_date_label(date_str, lang):
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return date_str
    today = datetime.now(TIMEZONE).date()
    if d == today:
        return t(lang, "today_label")
    if d == today - timedelta(days=1):
        return t(lang, "yesterday_label")
    return full_date_label(date_str, lang)


def day_counter(chat_id, date):
    """Atgriež (n, m): n — kura diena kopš pirmā ieraksta (ieskaitot izlaistās dienas),
    m — cik dienās līdz šim datumam ir pabeigta vismaz viena sesija (rīts vai vakars)."""
    conn = db()
    first = conn.execute("SELECT MIN(date) FROM answers WHERE chat_id=?", (chat_id,)).fetchone()[0]
    done_days = conn.execute(
        "SELECT COUNT(DISTINCT date) FROM sessions WHERE chat_id=? AND done=1 AND date<=?",
        (chat_id, date),
    ).fetchone()[0]
    conn.close()
    try:
        current = datetime.strptime(date, "%Y-%m-%d").date()
        start = datetime.strptime(first, "%Y-%m-%d").date() if first else current
    except (ValueError, TypeError):
        return 1, done_days
    return max((current - start).days + 1, 1), done_days


def day_suffix(chat_id, date, lang):
    n, m = day_counter(chat_id, date)
    return f"{t(lang, 'day_word')} {n}/{m}"


def format_entry(chat_id, date, lang) -> str:
    conn = db()
    rows = conn.execute(
        "SELECT period, question_index, answer_text FROM answers "
        "WHERE chat_id=? AND date=? ORDER BY id",
        (chat_id, date),
    ).fetchall()
    conn.close()
    label = relative_date_label(date, lang)
    if not rows:
        return t(lang, "no_entries_for_date", date=esc(label))
    rows.sort(key=lambda r: (PERIOD_ORDER.get(r[0], 0), r[1]))
    header = f"{tg_emoji('📓')} {esc(label)} · {esc(day_suffix(chat_id, date, lang))}"
    lines = [f"<b>{header}</b>"]
    current_period = None
    for period, idx, answer in rows:
        if period != current_period:
            current_period = period
            if period in PERIODS:
                lines.append("")
                period_emoji, period_text = t(lang, "period_" + period).split(" ", 1)
                lines.append(f"<b>{tg_emoji(period_emoji)} {esc(period_text)}</b>")
        emoji, text = topic_label(period, idx, lang)
        lines.append(f"<b>{tg_emoji(emoji)} {esc(text)}:</b> {esc(answer)}")
    return "\n".join(lines)


TELEGRAM_TEXT_LIMIT = 3900  # Telegram ziņas robeža ir 4096 rakstzīmes


def split_for_telegram(text, limit=TELEGRAM_TEXT_LIMIT):
    """Sadala garu HTML tekstu daļās pa rindām. Mūsu tagi nekad neiet pāri rindai,
    tāpēc dalīšana pa rindām ir droša."""
    if len(text) <= limit:
        return [text]
    parts = []
    current = ""

    def flush():
        nonlocal current
        if current.strip():
            parts.append(current)
        current = ""

    for line in text.split("\n"):
        while len(line) > limit:  # ļoti gara viena rinda (piem., gara atbilde)
            cut = line.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            amp = line.rfind("&", 0, cut)
            if amp > 0 and cut - amp < 8 and ";" not in line[amp:cut]:
                cut = amp  # nesagriež HTML entītiju pušu
            flush()
            parts.append(line[:cut])
            line = line[cut:].lstrip(" ")
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            flush()
            current = line
        else:
            current = candidate
    flush()
    return parts


async def send_html(context: ContextTypes.DEFAULT_TYPE, chat_id, text):
    for part in split_for_telegram(text):
        await context.bot.send_message(chat_id=chat_id, text=part, parse_mode="HTML")


async def send_today_entry(chat_id, context: ContextTypes.DEFAULT_TYPE):
    lang = get_user_language(chat_id)
    await send_html(context, chat_id, format_entry(chat_id, today_str(), lang))


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
        await send_html(context, chat_id, format_entry(chat_id, date, lang))


async def prompt_language_change(chat_id, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=chat_id, text=CHOOSE_LANGUAGE_TEXT, reply_markup=language_keyboard()
    )


def get_random_advice(lang):
    conn = db()
    row = conn.execute(
        "SELECT phrase FROM advice WHERE lang=? ORDER BY RANDOM() LIMIT 1", (lang,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


ADVICE_COOLDOWN_HOURS = 4


def get_last_advice_at(chat_id):
    conn = db()
    row = conn.execute(
        "SELECT last_advice_at FROM users WHERE chat_id=?", (chat_id,)
    ).fetchone()
    conn.close()
    if row and row[0]:
        try:
            return datetime.fromisoformat(row[0])
        except ValueError:
            return None
    return None


def set_last_advice_at(chat_id, dt):
    conn = db()
    conn.execute(
        "UPDATE users SET last_advice_at=?, advice_count=advice_count+1 WHERE chat_id=?",
        (dt.isoformat(), chat_id),
    )
    conn.commit()
    conn.close()


# Kuri jautājumi (pēc indeksa) katrā periodā der kā iedvesma jaunām padoma frāzēm
INSPIRATION_TOPICS = {
    "morning": {0, 1, 3},   # pateicība, kas ES esmu, galvenais uzdevums
    "evening": {1, 2, 4, 5},  # kas jauns, pateicība, iemācījos, sapratu
    "legacy": {0, 1, 3},
}


async def generate_and_store_new_advice(chat_id):
    """Analizē lietotāja pēdējo dienu atbilžu modeļus (pateicība, jaunais, iemācītais)
    un ģenerē VIENU jaunu, VISPĀRINĀTU padoma frāzi visās 3 valodās, pievienojot
    to kopīgajai advice tabulai. Frāze nedrīkst saturēt personiskas detaļas, jo
    tā nonāk kopīgajā, visiem redzamajā krājumā. Kļūdas gadījumā vienkārši
    izlaižam — tas nav kritiski galvenajai plūsmai."""
    if not openai_client:
        return
    recent = get_recent_answers_by_date(chat_id)
    if not recent:
        return
    lines = []
    for d, entries in recent.items():
        for period, idx, answer in entries:
            if idx in INSPIRATION_TOPICS.get(period, set()):
                lines.append(f"{topic_label(period, idx, 'en')[1]}: {answer}")
    if not lines:
        return
    context_text = "\n".join(lines)
    examples = "\n".join(ADVICE_SEED["en"][:5])
    system_prompt = (
        "You write short, punchy, universal wisdom one-liners in the style of "
        f"these examples:\n{examples}\n\n"
        "Based on the underlying THEMES in the reflection answers below (not the "
        "specific details), write ONE new short advice one-liner (under 15 words) "
        "inspired by that theme — but fully GENERALIZED and universal, with NO "
        "personal or private details from the answers. It must read like general "
        "life advice anyone could relate to, never a summary of this person's "
        "specific situation. Respond ONLY with a JSON object with exactly these "
        'keys: {"lv": "...", "en": "...", "ru": "..."} — the same piece of wisdom '
        "written naturally in each language (not literal translations)."
    )
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": context_text},
            ],
            max_tokens=200,
            temperature=0.9,
            response_format={"type": "json_object"},
        )
        data = json.loads(response.choices[0].message.content)
        conn = db()
        for lang_code in ("lv", "en", "ru"):
            phrase = (data.get(lang_code) or "").strip()
            if phrase:
                conn.execute(
                    "INSERT INTO advice (lang, phrase) VALUES (?, ?)", (lang_code, phrase)
                )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Neizdevās ģenerēt jaunu padoma frāzi")


# ---------- Komandas ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    telegram_lang_code = user.language_code if user else None
    # ensure_user neko nemaina esošam lietotājam — auto-noteikšana attiecas tikai uz jauniem.
    ensure_user(chat_id, detect_language(telegram_lang_code))

    lang = get_user_language(chat_id)
    morning_time, evening_time = get_user_times(chat_id)
    name = user.first_name if user else None
    greeting = t(lang, "greeting_named", name=name) if name else t(lang, "greeting_plain")
    intro = t(lang, "intro", morning=morning_time, evening=evening_time)
    message = f"{greeting}\n\n{intro}\n\n{t(lang, 'language_hint')}"
    await update.message.reply_text(message, reply_markup=main_menu_keyboard(lang))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang = get_user_language(chat_id)
    await update.message.reply_text(
        t(lang, "help", split=f"{SESSION_SPLIT_HOUR}:00"), reply_markup=main_menu_keyboard(lang)
    )


async def show_time_menu(chat_id, context: ContextTypes.DEFAULT_TYPE):
    lang = get_user_language(chat_id)
    morning_time, evening_time = get_user_times(chat_id)
    await context.bot.send_message(
        chat_id=chat_id,
        text=t(lang, "times_overview", morning=morning_time, evening=evening_time),
        reply_markup=period_picker_keyboard(lang),
    )


async def cmd_laiks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_time"] = None
    await show_time_menu(update.effective_chat.id, context)


async def cmd_tagad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sāk rīta vai vakara jautājumus uzreiz. Periodu nosaka pulkstenis:
    pirms SESSION_SPLIT_HOUR — rīts, vēlāk — vakars."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    ensure_user(chat_id, detect_language(user.language_code if user else None))
    lang = get_user_language(chat_id)
    period = period_for_now()
    date = today_str()
    session = get_session(chat_id, date, period)
    if session and session[0] == 1:
        await update.message.reply_text(t(lang, f"already_done_{period}"))
        await send_html(context, chat_id, format_entry(chat_id, date, lang))
        return
    if session:
        # sesija jau sākta, bet nepabeigta — turpinām no tā paša jautājuma
        await update.message.reply_text(t(lang, "resume_note"))
        await send_question(chat_id, session[1], period, lang, context)
        return
    await start_session(chat_id, context, period, date)


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
    un kas ir saglabāts datubāzē, lai varētu pārbaudīt valodas un laiku iestatījumus."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    tg_code = user.language_code if user else None
    detected = detect_language(tg_code)
    stored = get_user_language(chat_id)
    morning_time, evening_time = get_user_times(chat_id)
    n, m = day_counter(chat_id, today_str())
    text = (
        "🔧 Debug info\n\n"
        f"Telegram language_code: {tg_code!r}\n"
        f"Auto-noteiktā valoda (šobrīd): {detected}\n"
        f"Saglabātā valoda (datubāzē): {stored}\n"
        f"Rīta laiks: {morning_time}\n"
        f"Vakara laiks: {evening_time}\n"
        f"'Sākt tagad' šobrīd sāktu: {period_for_now()}\n"
        f"Dienas skaitītājs: {n}/{m}\n"
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
    """Testēšanai — nekavējoties pārslēdz uz nākamo simulēto sesiju (rīts -> vakars ->
    nākamās dienas rīts) un sāk to, negaidot reālu laiku vai plānoto atgādinājumu."""
    chat_id = update.effective_chat.id
    conn = db()
    row = conn.execute(
        "SELECT date, period FROM sessions WHERE chat_id=? ORDER BY date DESC, "
        "CASE period WHEN 'morning' THEN 1 WHEN 'evening' THEN 2 ELSE 0 END DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    conn.close()
    today = datetime.now(TIMEZONE).date()
    if not row:
        next_date, next_period = today, "morning"
    else:
        try:
            last_date = datetime.strptime(row[0], "%Y-%m-%d").date()
        except ValueError:
            last_date = today
        if last_date < today:
            next_date, next_period = today, "morning"
        elif row[1] == "morning":
            next_date, next_period = last_date, "evening"
        else:
            next_date, next_period = last_date + timedelta(days=1), "morning"
    await start_session(chat_id, context, next_period, next_date.strftime("%Y-%m-%d"))


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    lang = query.data.split(":", 1)[1]
    if lang not in TEXTS:
        lang = DEFAULT_LANGUAGE
    set_user_language(chat_id, lang)
    await query.answer()
    await query.edit_message_text(LANGUAGES[lang])
    morning_time, evening_time = get_user_times(chat_id)
    await context.bot.send_message(
        chat_id=chat_id,
        text=t(lang, "intro", morning=morning_time, evening=evening_time),
        reply_markup=main_menu_keyboard(lang),
    )


async def pickperiod_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lietotājs izvēlējies, kuru laiku mainīt (rīts vai vakars)."""
    query = update.callback_query
    chat_id = query.message.chat_id
    lang = get_user_language(chat_id)
    period = query.data.split(":", 1)[1]
    await query.answer()
    if period not in PERIODS:
        return
    morning_time, evening_time = get_user_times(chat_id)
    current = morning_time if period == "morning" else evening_time
    await query.edit_message_text(
        t(lang, "choose_time", period=t(lang, f"period_{period}"), current=current),
        reply_markup=time_menu_keyboard(lang, period),
    )


async def settime_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    lang = get_user_language(chat_id)
    parts = query.data.split(":", 2)  # settime:<period>:<HH:MM vai custom>
    await query.answer()
    if len(parts) != 3 or parts[1] not in PERIODS:
        return
    period, value = parts[1], parts[2]
    period_label = t(lang, f"period_{period}")

    if value == "custom":
        context.user_data["awaiting_time"] = period
        await query.edit_message_text(t(lang, "custom_time_prompt", period=period_label))
        return

    new_time = parse_hhmm(value)
    if not new_time:
        return
    set_user_time(chat_id, period, new_time)
    await query.edit_message_text(t(lang, "time_set", period=period_label, time=new_time))


# ---------- Atbilžu apstrāde ----------

async def _save_answer_and_advance(chat_id, date, period, idx, lang, answer_text, is_voice, context):
    questions = QUESTIONS[period].get(lang, QUESTIONS[period][DEFAULT_LANGUAGE])
    next_idx = idx + 1
    finished = next_idx >= len(questions)
    conn = db()
    conn.execute(
        "INSERT INTO answers (chat_id, date, period, question_index, question_text, "
        "answer_text, is_voice, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            chat_id,
            date,
            period,
            idx,
            questions[idx],
            answer_text,
            1 if is_voice else 0,
            datetime.now(TIMEZONE).isoformat(),
        ),
    )
    conn.execute(
        "UPDATE sessions SET current_index=?, done=? WHERE chat_id=? AND date=? AND period=?",
        (next_idx, 1 if finished else 0, chat_id, date, period),
    )
    conn.commit()
    conn.close()

    if not finished:
        await send_question(chat_id, next_idx, period, lang, context)
        return

    await context.bot.send_message(chat_id=chat_id, text=t(lang, f"thanks_{period}"))
    await send_html(context, chat_id, format_entry(chat_id, date, lang))
    encouragement = await generate_encouragement(chat_id, lang, period, date)
    if encouragement:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{tg_emoji('✨')} {esc(encouragement)}",
            parse_mode="HTML",
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text
    lang = get_user_language(chat_id)

    action = BUTTON_ACTIONS.get(text)
    if action:
        context.user_data["awaiting_time"] = None
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
        await show_time_menu(chat_id, context)
        return
    if action == "help":
        await cmd_help(update, context)
        return
    if action == "language":
        await prompt_language_change(chat_id, context)
        return
    if action == "advice":
        last = get_last_advice_at(chat_id)
        now = datetime.now(TIMEZONE)
        if last and (now - last) < timedelta(hours=ADVICE_COOLDOWN_HOURS):
            cooldown_messages = ADVICE_COOLDOWN_MESSAGES.get(
                lang, ADVICE_COOLDOWN_MESSAGES[DEFAULT_LANGUAGE]
            )
            await update.message.reply_text(random.choice(cooldown_messages))
            return
        set_last_advice_at(chat_id, now)
        advice = get_random_advice(lang)
        if advice:
            await update.message.reply_text(
                f"{tg_emoji('💡')} {esc(advice)}", parse_mode="HTML"
            )
        await generate_and_store_new_advice(chat_id)
        return

    awaiting = context.user_data.get("awaiting_time")
    if awaiting in PERIODS:
        new_time = parse_hhmm(text)
        if new_time:
            set_user_time(chat_id, awaiting, new_time)
            context.user_data["awaiting_time"] = None
            await update.message.reply_text(
                t(lang, "time_set", period=t(lang, f"period_{awaiting}"), time=new_time)
            )
        else:
            await update.message.reply_text(t(lang, "invalid_time_format"))
        return

    session = get_active_session(chat_id)
    if not session:
        await update.message.reply_text(t(lang, "no_active_question"))
        return
    date, period, idx = session
    await _save_answer_and_advance(chat_id, date, period, idx, lang, text, False, context)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang = get_user_language(chat_id)
    session = get_active_session(chat_id)
    if not session:
        await update.message.reply_text(t(lang, "no_active_question"))
        return
    if not openai_client:
        await update.message.reply_text(t(lang, "voice_not_configured"))
        return
    date, period, idx = session
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
    await _save_answer_and_advance(chat_id, date, period, idx, lang, text, True, context)


# ---------- Palaišana ----------

# Komandas, kas redzamas Telegram "/" ieteikumu sarakstā (pēc lietotāja valodas).
# /reset un /next apzināti NAV šeit iekļautas — tās joprojām strādā, ja tās
# uzraksta ar roku, bet nav publiski redzamas.
PUBLIC_COMMANDS = [
    ("start", {"lv": "Sākt / restartēt botu", "en": "Start / restart the bot", "ru": "Запустить / перезапустить бота"}),
    ("tagad", {"lv": "Sākt rīta/vakara jautājumus tagad", "en": "Start morning/evening questions now", "ru": "Начать утренние/вечерние вопросы сейчас"}),
    ("sodien", {"lv": "Šodienas ieraksts", "en": "Today's entry", "ru": "Запись за сегодня"}),
    ("vesture", {"lv": "Pēdējo dienu ieraksti", "en": "Recent entries", "ru": "Последние записи"}),
    ("laiks", {"lv": "Mainīt rīta/vakara laiku", "en": "Change morning/evening time", "ru": "Изменить время утра/вечера"}),
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
    app.add_handler(CallbackQueryHandler(pickperiod_callback, pattern=r"^pickperiod:"))
    app.add_handler(CallbackQueryHandler(settime_callback, pattern=r"^settime:"))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.job_queue.run_repeating(check_and_trigger, interval=60, first=5)

    logger.info("Bots startē...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
