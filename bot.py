import json
import os
import logging
import random
from datetime import datetime
import pytz
import asyncio
import requests
import re
import base64

from aiogram import Bot, Dispatcher, types, executor
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# --- Налаштування ---
BOT_TOKEN = "8270366283:AAHxjn64fkdB9xXDIkVwe7h2iiJST5aBemU"
MISTRAL_API_KEY = "bfDtPtgSLxjZZSsEnox8vv0Z094YacXO"

DB_PATH = "llbot.json"
DAILY_REMINDER_HOUR = 16  # Київський час
DAILY_REMINDER_MINUTE = 23
TIMEZONE = "Europe/Kiev"

# --- Логування ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("aiogram").setLevel(logging.ERROR)

# --- Бот ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

quiz_states = {}

# --- Допоміжні функції для JSON бази ---
def load_db():
    if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) == 0:
        return {"users": {}, "words": {}}
    with open(DB_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_to_github():
    """
    Якщо задано GITHUB_TOKEN і REPO_NAME, заливає оновлений JSON у GitHub.
    """
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO", "Polilen/tgbot")
    file_path = DB_PATH

    if not token:
        logger.warning("⚠️ GITHUB_TOKEN не задано, пропускаю оновлення GitHub.")
        return

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    url = f"https://api.github.com/repos/{repo}/contents/{file_path}"
    headers = {"Authorization": f"token {token}"}

    # Отримуємо SHA поточного файлу (якщо існує)
    r = requests.get(url, headers=headers)
    sha = r.json().get("sha") if r.status_code == 200 else None

    # Кодуємо файл у Base64 — GitHub цього вимагає
    encoded_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")

    data = {
        "message": "update JSON",
        "content": encoded_content,
        "sha": sha
    }

    response = requests.put(url, headers=headers, json=data)
    if response.status_code not in (200, 201):
        logger.error(f"❌ Не вдалося оновити JSON у GitHub: {response.text}")
    else:
        logger.info("✅ JSON успішно оновлено у GitHub.")


def save_db(db):
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    save_to_github()

def ensure_user(user_id: int, language: str = None):
    db = load_db()
    uid = str(user_id)
    if uid not in db["users"]:
        db["users"][uid] = {
            "language": language or "",
            "quiz_score": 0,
            "words_learned": 0,
            "last_activity": datetime.utcnow().isoformat()
        }
    else:
        if language:
            db["users"][uid]["language"] = language
        db["users"][uid]["last_activity"] = datetime.utcnow().isoformat()
    save_db(db)

def add_word_to_db(user_id: int, word: str, translation: str):
    db = load_db()
    uid = str(user_id)
    if uid not in db["words"]:
        db["words"][uid] = []
    db["words"][uid].append({
        "word": word.strip(),
        "translation": translation.strip(),
        "known": False,
        "added_at": datetime.utcnow().isoformat()
    })
    save_db(db)

def clean_word_text(text: str):
    """
    Перетворює рядки виду 'Dolphin** — Дельфін' у 'Dolphin — Дельфін'.
    Ігнорує будь-які коментарі AI після слова.
    """
    if '—' not in text:
        return None, None
    eng, ukr = text.split('—', 1)
    # Видаляємо *, цифри, точки, дужки, зайві пробіли
    eng = re.sub(r'[\*\d\.\(\[].*?[\)\]]|[\*\d\.]+', '', eng).strip()
    ukr = re.sub(r'[\*\d\.]+', '', ukr).strip()
    return eng, ukr

def get_user_words(user_id: int):
    db = load_db()
    return db["words"].get(str(user_id), [])

def mark_word_known(user_id: int, word_index: int):
    db = load_db()
    uid = str(user_id)
    if uid in db["words"] and 0 <= word_index < len(db["words"][uid]):
        db["words"][uid][word_index]["known"] = True
        save_db(db)

def increment_quiz_score(user_id: int, score: int):
    db = load_db()
    uid = str(user_id)
    if uid in db["users"]:
        db["users"][uid]["quiz_score"] += score
        save_db(db)

# --- Допоміжні функції Mistral AI ---
def mistral_translate(word: str) -> str:
    """
    Перекладає англійське слово українською за допомогою Mistral AI.
    """
    url = "https://api.mistral.ai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {MISTRAL_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "mistral-small-latest",
        "messages": [
            {"role": "system", "content": "Ти перекладач. Перекладай з англійської на українську лише одним словом."},
            {"role": "user", "content": f"Переклади слово '{word}' українською."}
        ]
    }

    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 200:
        result = response.json()
        return result["choices"][0]["message"]["content"].strip()
    else:
        logger.error(f"Mistral API error: {response.text}")
        return None

def mistral_generate_topic_words(topic: str, n: int = 5) -> dict:
    """
    Генерує n слів по темі з перекладом українською.
    Повертає словник {англ: укр}.
    """
    url = "https://api.mistral.ai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {MISTRAL_API_KEY}",
        "Content-Type": "application/json"
    }
    prompt = (
        f"Сформуй {n} англійських слів по темі '{topic}' і дай переклад українською в форматі "
        f"'word — переклад', лише слова, без пояснень."
    )
    data = {
        "model": "mistral-small-latest",
        "messages": [
            {"role": "system", "content": "Ти помічник для вивчення англійських слів."},
            {"role": "user", "content": prompt}
        ]
    }

    response = requests.post(url, headers=headers, json=data)
    words_dict = {}
    if response.status_code == 200:
        result = response.json()
        text = result["choices"][0]["message"]["content"].strip()
        for line in text.split("\n"):
            if "—" in line:
                eng, ukr = line.split("—", 1)
                words_dict[eng.strip()] = ukr.strip()
    else:
        logger.error(f"Mistral API error: {response.text}")
    return words_dict


@dp.message_handler(lambda msg: msg.text in ["/help", "/quiz", "/start"])
async def handle_buttons(message: types.Message):
    if message.text == "/help":
        await cmd_help(message)
    elif message.text == "/quiz":
        await cmd_quiz(message)
    elif message.text == "/start":
        await cmd_start(message)
# --- Обробники команд ---
@dp.message_handler(commands=["help"])
async def cmd_help(message: types.Message):
    help_text = (
        "📌 Доступні команди:\n\n"
        "/start — почати роботу\n"
        "/addword слово-переклад — додати слово\n"
        "/translate слово — додати слово з перекладом\n"
        "/aiword число — додати вказану кількість слів через AI\n"
        "/aitopic тема число — додати слова по темі через AI (до 10)\n"
        "/mywords — показати свої слова\n"
        "/quiz — почати вікторину\n"
        "/stats — показати статистику\n"
        "/editword номер слово-переклад — редагувати слово\n"
        "/deleteword номер — видалити слово за номером\n"
        "/clearwords — видалити всі слова\n"
    )
    await message.answer(help_text, parse_mode="Markdown")

@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    # Устанавливаем язык по умолчанию
    language = "English"
    ensure_user(message.from_user.id, language)
    
    # Создаём клавиатуру с кнопками
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(
        KeyboardButton("/help"),
        KeyboardButton("/quiz"),
        KeyboardButton("/start")
    )
    
    await message.answer(
        "👋 Привіт! Це бот для вивчення англійських слів.\n\n"
        "Можеш скористатися кнопками нижче для навігації або ввести команду вручну:\n"
        "/addword слово-переклад — додати слово\n"
        "/translate слово — перекласти слово через AI\n"
        "/aiword число — згенерувати слова через AI\n"
        "/aitopic тема число — згенерувати слова по темі\n"
        "/mywords — переглянути свої слова\n"
        "/quiz — почати вікторину\n\n"
        "Щоб побачити усі команди, введи /help.",
        reply_markup=kb
    )
@dp.message_handler(commands=['addword'])
async def cmd_addword(message: types.Message):
    args = message.get_args()
    if not args:
        await message.answer("Введи слова у форматі:\n/addword cat - кіт\ndog - собака")
        return
    lines = [l.strip() for l in args.split("\n") if l.strip()]
    added = []
    for line in lines:
        if '-' in line:
            parts = line.split('-', 1)
            word, translation = parts[0].strip(), parts[1].strip()
            if word and translation:
                add_word_to_db(message.from_user.id, word, translation)
                added.append(f"{word} — {translation}")
    if added:
        await message.answer("✅ Додані слова:\n" + "\n".join(added))
    else:
        await message.answer("❌ Не вдалося розпізнати слова. Використовуй формат 'word - translation'.")

# --- AI word generators ---
def mistral_generate_unique_words(existing_words: set, count: int = 1) -> dict:
    url = "https://api.mistral.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {MISTRAL_API_KEY}", "Content-Type": "application/json"}
    existing_str = ", ".join(existing_words) if existing_words else "немає слів"
    prompt = f"Згенеруй {count} англійських слів з перекладом українською у форматі 'англ — укр', не включаючи ці слова: {existing_str}. Кожне слово має бути новим та унікальним."
    data = {"model": "mistral-small-latest", "messages": [{"role": "system", "content": "Ти перекладач і вчитель англійської мови."}, {"role": "user", "content": prompt}]}

    response = requests.post(url, headers=headers, json=data)
    result = {}
    if response.status_code == 200:
        resp_json = response.json()
        content = resp_json["choices"][0]["message"]["content"].strip()
        lines = [l.strip() for l in content.split("\n") if l.strip()]
        for line in lines:
            if '—' in line:
                eng, ukr = line.split('—', 1)
                eng, ukr = eng.strip(), ukr.strip()
                if eng not in existing_words:
                    result[eng] = ukr
    else:
        logger.error(f"Mistral API error: {response.text}")

    return result

@dp.message_handler(commands=["translate"])
async def cmd_translate(message: types.Message):
    args = message.get_args().strip()
    if not args:
        await message.answer("Введи слово для перекладу. Приклад:\n/translate dog")
        return

    word = args
    await message.answer(f"⏳ Перекладаю слово '{word}'...")
    translation = mistral_translate(word)
    if translation:
        eng, ukr = clean_word_text(f"{word} — {translation}")
        add_word_to_db(message.from_user.id, eng, ukr)
        await message.answer(f"✅ Додано: {eng} — {ukr}")
    else:
        await message.answer("❌ Не вдалося перекласти слово.")

@dp.message_handler(commands=["aiword"])
async def cmd_aiword(message: types.Message):
    args = message.get_args().strip()
    
    if not args.isdigit():
        await message.answer("Введи кількість слів для генерації. Приклад: /aiword 5")
        return
    
    count = int(args)
    count = min(count, 10)  # максимум 10 слів

    existing_words = {w['word'] for w in get_user_words(message.from_user.id)}
    await message.answer(f"⏳ Генерую {count} унікальних слів через Mistral AI...")

    words_dict = mistral_generate_unique_words(existing_words, count)
    added = {}

    if words_dict:
        for eng_raw, ukr_raw in words_dict.items():
            eng, ukr = clean_word_text(f"{eng_raw} — {ukr_raw}")
            if eng and ukr and eng not in existing_words:
                add_word_to_db(message.from_user.id, eng, ukr)
                existing_words.add(eng)
                added[eng] = ukr

        added_words_text = "\n".join([f"{e} — {u}" for e, u in added.items()])
        await message.answer(f"✅ Додані слова:\n{added_words_text}")
    else:
        await message.answer("❌ Не вдалося згенерувати нові унікальні слова.")


@dp.message_handler(commands=["aitopic"])
async def cmd_aitopic(message: types.Message):
    args = message.get_args().strip()
    if not args:
        topic = random.choice(["food","animals","colors","house","school","nature"])
        count = 5
    else:
        parts = args.split()
        topic = parts[0]
        count = int(parts[1]) if len(parts) > 1 else 5
        count = min(count, 10)

    existing_words = {w['word'] for w in get_user_words(message.from_user.id)}
    await message.answer(f"⏳ Генерую до {count} унікальних слів за темою '{topic}' через Mistral AI...")

    prompt_words = ", ".join(existing_words) if existing_words else "немає слів"
    url = "https://api.mistral.ai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {MISTRAL_API_KEY}",
        "Content-Type": "application/json"
    }
    prompt = (
        f"Згенеруй {count} англійських слів за темою '{topic}' з перекладом українською у форматі 'англ — укр', "
        f"не включаючи ці слова: {prompt_words}. Кожне слово має бути новим та унікальним. "
        f"Не додавай будь-які коментарі чи пояснення після слів."
    )
    data = {
        "model": "mistral-small-latest",
        "messages": [
            {"role": "system", "content": "Ти перекладач і вчитель англійської мови."},
            {"role": "user", "content": prompt}
        ]
    }

    response = requests.post(url, headers=headers, json=data)
    added = {}
    if response.status_code == 200:
        resp_json = response.json()
        content = resp_json["choices"][0]["message"]["content"].strip()
        lines = [l.strip() for l in content.split("\n") if l.strip()]
        for line in lines:
            eng, ukr = clean_word_text(line)
            if eng and ukr and eng not in existing_words:
                add_word_to_db(message.from_user.id, eng, ukr)
                existing_words.add(eng)
                added[eng] = ukr

    if added:
        await message.answer("✅ Додані слова:\n" + "\n".join([f"{e} — {u}" for e, u in added.items()]))
    else:
        await message.answer("❌ Не вдалося згенерувати нові унікальні слова за цією темою.")

# --- MyWords, Stats, Quiz обробники ---
@dp.message_handler(commands=["mywords"])
async def cmd_mywords(message: types.Message):
    words = get_user_words(message.from_user.id)
    if not words:
        await message.answer("Словник порожній.")
        return
    lines = [f"{i+1}. {w['word']} — {w['translation']} {'✅' if w['known'] else '❌'}" for i, w in enumerate(words)]
    await message.answer("\n".join(lines))

@dp.message_handler(commands=["stats"])
async def cmd_stats(message: types.Message):
    db = load_db()
    uid = str(message.from_user.id)
    user = db["users"].get(uid)
    if not user:
        await message.answer("Немає даних користувача.")
        return
    words = get_user_words(message.from_user.id)
    known = sum(1 for w in words if w["known"])
    await message.answer(
        f"📊 Статистика\nВсього слів: {len(words)}\nВивчено: {known}\nОчки вікторини: {user['quiz_score']}"
    )

@dp.message_handler(commands=['delword'])
async def cmd_delword(message: types.Message):
    args = message.get_args().strip()
    if not args.isdigit():
        await message.answer("Напиши номер слова для видалення. Приклад: /delword 3")
        return
    idx = int(args) - 1
    words = get_user_words(message.from_user.id)
    if 0 <= idx < len(words):
        removed = words.pop(idx)
        db = load_db()
        db["words"][str(message.from_user.id)] = words
        save_db(db)
        await message.answer(f"✅ Видалено слово: {removed['word']} — {removed['translation']}")
    else:
        await message.answer("❌ Слово з таким номером не знайдено.")

@dp.message_handler(commands=['editword'])
async def cmd_editword(message: types.Message):
    args = message.get_args().strip()
    if not args:
        await message.answer("Використовуй формат: /editword <номер> нове_слово - новий_переклад")
        return
    parts = args.split(maxsplit=1)
    if len(parts) < 2 or '-' not in parts[1]:
        await message.answer("Невірний формат. Приклад: /editword 2 cat - кіт")
        return
    idx = int(parts[0]) - 1
    new_word, new_trans = [p.strip() for p in parts[1].split('-', 1)]
    words = get_user_words(message.from_user.id)
    if 0 <= idx < len(words):
        words[idx]["word"] = new_word
        words[idx]["translation"] = new_trans
        db = load_db()
        db["words"][str(message.from_user.id)] = words
        save_db(db)
        await message.answer(f"✅ Слово оновлено: {new_word} — {new_trans}")
    else:
        await message.answer("❌ Слово з таким номером не знайдено.")

@dp.message_handler(commands=['clearwords'])
async def cmd_clearwords(message: types.Message):
    db = load_db()
    db["words"][str(message.from_user.id)] = []
    save_db(db)
    await message.answer("✅ Всі слова видалені.")

@dp.message_handler(commands=["quiz"])
async def cmd_quiz(message: types.Message):
    words = get_user_words(message.from_user.id)
    if not words:
        await message.answer("Слів немає. Додайте через /addword")
        return
    questions = []
    all_trans = [w["translation"] for w in words]
    for i, w in enumerate(random.sample(words, min(5, len(words)))):
        wrongs = [t for t in all_trans if t != w["translation"]]
        options = [w["translation"]] + random.sample(wrongs, min(3, len(wrongs)))
        random.shuffle(options)
        questions.append({"index": i, "word": w["word"], "correct": w["translation"], "options": options})
    quiz_states[message.from_user.id] = {"questions": questions, "current": 0, "score": 0}
    await send_quiz_question(message.from_user.id)

async def send_quiz_question(user_id: int):
    state = quiz_states.get(user_id)
    if not state:
        return
    if state["current"] >= len(state["questions"]):
        increment_quiz_score(user_id, state["score"])
        await bot.send_message(user_id, f"Вікторина завершена. Результат: {state['score']}/{len(state['questions'])}")
        quiz_states.pop(user_id)
        return
    q = state["questions"][state["current"]]
    kb = InlineKeyboardMarkup()
    for i, opt in enumerate(q["options"]):
        kb.add(InlineKeyboardButton(opt, callback_data=f"quiz|{state['current']}|{i}"))
    await bot.send_message(user_id, f"{q['word']} — обери переклад", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("quiz|"))
async def handle_quiz(callback: types.CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    state = quiz_states.get(user_id)
    if not state:
        return
    parts = callback.data.split("|")
    q_index, opt_index = int(parts[1]), int(parts[2])
    q = state["questions"][q_index]
    selected = q["options"][opt_index]
    words = get_user_words(user_id)
    word_idx = next((i for i, w in enumerate(words) if w["word"] == q["word"]), None)
    if selected == q["correct"]:
        state["score"] += 1
        if word_idx is not None:
            mark_word_known(user_id, word_idx)
        await bot.send_message(user_id, f"✅ Правильно! {q['word']} → {q['correct']}")
    else:
        await bot.send_message(user_id, f"❌ Неправильно. {q['word']} → {q['correct']}")
    state["current"] += 1
    await send_quiz_question(user_id)

# --- Планувальник ---
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

async def send_daily_words_async():
    db = load_db()
    for uid, words in db["words"].items():
        if not words:
            continue
        sample = random.sample(words, min(3, len(words)))
        lines = ["📅 Час повторити слова!"]
        for w in sample:
            lines.append(f"{w['word']} — {w['translation']}")
        await bot.send_message(int(uid), "\n".join(lines))

def start_scheduler():
    scheduler.add_job(send_daily_words_async, CronTrigger(hour=DAILY_REMINDER_HOUR, minute=DAILY_REMINDER_MINUTE))
    logger.info("Завдання на щоденне нагадування додано")

# --- Старт ---
async def on_startup(dp):
    start_scheduler()
    scheduler.start()
    logger.info("Scheduler запущено")

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
