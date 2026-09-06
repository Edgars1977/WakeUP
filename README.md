# Rīta refleksijas Telegram bots

Katru dienu noteiktā laikā bots uzdod 5 jautājumus (pateicība, uzvara,
problēma, nodomi, jautājums Visumam). Tu atbildi ar tekstu vai balss
ziņu — balss tiek automātiski pārvērsta tekstā. Visas atbildes tiek
saglabātas, un bots izveido ikdienas ierakstu, ko vari apskatīt ar
`/sodien` vai `/vesture`.

## 1. Izveido Telegram botu

1. Telegram atver sarunu ar **@BotFather**.
2. Nosūti `/newbot`, izvēlies vārdu un lietotājvārdu.
3. Saglabā izdoto **tokenu** (ievadam).

## 2. Sagatavo OpenAI API atslēgu (balss transkripcijai)

1. Dodies uz https://platform.openai.com/api-keys
2. Izveido jaunu atslēgu (ievadam).
3. Whisper transkripcija ir lēta (dažus centus par stundu audio), bet
   nepieciešams pievienot maksājumu kartes datus OpenAI kontam.

Ja nevēlies to darīt uzreiz — bots strādās arī bez tā, vienkārši balss
ziņas netiks apstrādātas, kamēr `OPENAI_API_KEY` nav iestatīts.

## 3. Izvieto uz Railway

1. Izveido kontu https://railway.app un jaunu projektu.
2. **New Project → Deploy from GitHub repo** (vispirms augšupielādē šo
   mapi uz savu GitHub repozitoriju) **vai** izmanto Railway CLI:
   ```
   railway login
   railway init
   railway up
   ```
3. Railway automātiski atpazīs `Dockerfile` un uzbūvēs konteineru.
4. **Variables** cilnē pievieno mainīgos no `.env.example`:
   - `TELEGRAM_BOT_TOKEN`
   - `OPENAI_API_KEY`
   - `TIMEZONE` (noklusēti `Europe/Riga`)
   - `DEFAULT_MORNING_TIME` (noklusēti `08:00`)
   - `DB_PATH=/data/bot.db`
5. **Svarīgi — datu noturība:** pievieno Railway **Volume** un piesaisti
   to ceļam `/data`, citādi datubāze pazudīs pēc katras pārizvietošanas.
   (Project → Volumes → New Volume → Mount path: `/data`)
6. Deploy. Logos vajadzētu redzēt "Bots startē...".

### Ja izmanto Render vietā

Render arī atbalsta Dockerfile izvietošanu, taču **bezmaksas Web
Service tips aiziet miegā pēc 15 min neaktivitātes** — tas nozīmē,
bots var nereaģēt uz ziņām un palaist garām rīta laiku, kamēr kāds to
neuzmodina ar pieprasījumu. Ja izvēlies Render bezmaksas tarifu, ieteicams
uzstādīt ārēju "ping" servisu (piem., UptimeRobot), kas ik pa 10 min
piekļūst servisam, lai tas nenoietu miegā. Render maksas "Background
Worker" tarifs šo problēmu nerada.

## 4. Sāc lietot

1. Telegram atrodi savu botu (pēc lietotājvārda, ko devi @BotFather).
2. Nosūti `/start`.
3. Ja vēlies citu rīta laiku: `/laiks 07:30`.
4. Lai izmēģinātu uzreiz, nesagaidot rītu: `/tagad`.
5. `/sodien` — šodienas ieraksts. `/vesture 14` — pēdējo 14 dienu ieraksti.

## Komandas

| Komanda | Ko dara |
|---|---|
| `/start` | Reģistrē tevi un parāda pamatinfo |
| `/laiks HH:MM` | Iestata rīta jautājumu laiku |
| `/tagad` | Sāk šodienas jautājumus uzreiz |
| `/sodien` | Parāda šodienas ierakstu |
| `/vesture N` | Parāda pēdējo N dienu ierakstus (noklusēti 7) |
| `/palidziba` | Palīdzība |

## Lokāla testēšana

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # ievadi savus datus
# ffmpeg jābūt instalētam sistēmā (macOS: brew install ffmpeg)
python bot.py
```
