import os
import re
import json
import time
import threading
from threading import Thread
from flask import Flask, jsonify
import telebot
from telebot import types
import groq

# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
ADMIN_ID = 5076963429

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

bot = telebot.TeleBot(BOT_TOKEN)
client = groq.Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

BUFFER_SECONDS = 30
MIN_ANKETA_LENGTH = 100
MAX_TELEGRAM_TEXT = 4096

STATE_FILE = "bot_state.json"
STATE_SAVE_DELAY = 2

# Активные анкеты
user_buffers = {}          # chat_id -> list текста
user_media = {}            # chat_id -> list message_id
user_timers = {}
user_locks = {}
user_categories = {}
user_last_messages = {}
user_finish_buttons = {}

# Данные для админской панели
known_users = {}
recent_submissions = []
MAX_RECENT_SUBMISSIONS = 50

state_timer = None
state_lock = threading.Lock()
polling_alive = False
polling_thread = None
polling_lock = threading.Lock()

WELCOME_TEXT = (
    "👋 Приветствуем!\n\n"
    "Для вступления нажмите «📝 Создать анкету» и выберите категорию персонажа.\n\n"
    "📌 Текст, фотографии, музыку, документы и другие материалы можно "
    "отправлять отдельными сообщениями после начала анкеты.\n"
    "📌 Все части собираются в одну анкету.\n"
    "📌 После проверки бот выдаст замечания или отправит анкету администрации.\n\n"
    "❓ По вопросам и проблемам: @CrazyCrabSalad"
)

SYSTEM_PROMPT = """
Ты — строгий помощник модератора текстовой ролевой игры по фандому
«Дом, в котором».

ВАЖНЕЙШЕЕ ПРАВИЛО:
НЕ ВЫДУМЫВАЙ ЗАМЕЧАНИЯ.
Замечание можно делать только тогда, когда оно прямо подтверждается
текстом анкеты или явно следует из перечисленных обязательных критериев.
Если сомневаешься — НЕ считай это ошибкой.

КАТЕГОРИИ:
- «Домовец» — проверяется по правилам Домовца.
- «Персонал» — все сотрудники/персонал.
- «Другое» — рассматривай как ПЕРСОНАЛ.
- «Наружник» больше НЕ существует.

ДОМОВЕЦ:
1. Кличка.
2. Стая: Жрецы, Искры, Гавена, Утопленники, Кометы, Орфы.
   Мистерийцы допустимы только если игрок действительно использует эту стаю
   по актуальному лору.
3. Пол.
4. Возраст — строго 14–18 лет.
5. Заболевание — строго обязательно. Пункт выполнен, если:
   а) указано физическое заболевание/инвалидность;
   ИЛИ
   б) в биографии есть логичное объяснение попадания в Дом через
   доплату, связи, взятку, перевод по блату и т.п.
6. Внешность.
7. Характер — минимум 200 символов.
8. Причина попадания.
9. Возраст попадания.
10. Умения.
11. Юз.

ПЕРСОНАЛ:
1. Кличка.
2. Пол.
3. Возраст — 20+ лет.
4. Внешность.
5. Характер — минимум 200 символов.
6. Предыстория — минимум 50–70 символов.
7. Должность.
8. Юз.

Не требуй конкретного порядка, идеальной разметки или художественного стиля.
Не используй скрытые критерии.
Не проверяй канон по догадкам.
Логическая ошибка — только грубое противоречие внутри анкеты или
явное нарушение возраста/категории.

ОРФОГРАФИЯ:
Указывай только явные ошибки и опечатки. Для каждой дай фрагмент и исправление.
Не переписывай текст ради стиля.

ДУБЛИКАТЫ:
Указывай только явное повторение пункта или большого фрагмента подряд.

АНТИ-БРЕД:
Если текст — спам, случайные слова, бессмыслица или попытка обойти проверку,
статус «Требует правок». Не называй бредом обычное короткое описание,
если обязательные пункты распознаются.

АПЕЛЛЯЦИЯ:
Если есть слово «Апелляция» и раздел «Пояснение:», это апелляция.
Не отклоняй её только за несогласие с замечанием.
В отчёте укажи, что это апелляция и окончательное решение за администрацией.

ФОРМАТ СТРОГО:

СТАТУС: [Принять / Требует правок]

ЗАМЕЧАНИЯ ДЛЯ ИГРОКА:
Если есть проблема:
1. [конкретное замечание]
...
Если проблем нет:
Нет

ОТЧЕТ ДЛЯ АДМИНА:
[2–3 предложения]

Никаких выдуманных ошибок. Если всё обязательное есть и явных проблем нет —
обязательно «Принять».
"""

# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!", 200

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "telegram_polling": polling_alive,
        "time": int(time.time())
    }), 200

def run_flask():
    port = int(os.environ.get("PORT", 1000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ============================================================
# СОСТОЯНИЕ
# ============================================================

def save_state_now():
    data = {
        "known_users": known_users,
        "recent_submissions": recent_submissions[-MAX_RECENT_SUBMISSIONS:]
    }
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
        print(f"[STATE] Состояние сохранено: {STATE_FILE}")
    except Exception as e:
        print(f"[STATE] Ошибка сохранения: {type(e).__name__}: {e}")

def delayed_save():
    global state_timer
    state_timer = None
    save_state_now()

def schedule_state_save():
    global state_timer
    with state_lock:
        if state_timer is not None:
            return
        state_timer = threading.Timer(STATE_SAVE_DELAY, delayed_save)
        state_timer.daemon = True
        state_timer.start()

def load_state():
    global known_users, recent_submissions
    if not os.path.exists(STATE_FILE):
        print("[STATE] Файл состояния не найден.")
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        known_users = data.get("known_users", {})
        recent_submissions = data.get("recent_submissions", [])[-MAX_RECENT_SUBMISSIONS:]
        print(
            f"[STATE] Состояние загружено: "
            f"{len(known_users)} игроков, {len(recent_submissions)} анкет."
        )
    except Exception as e:
        print(f"[STATE] Ошибка загрузки: {type(e).__name__}: {e}")

def remember_user(message):
    cid = str(message.chat.id)
    first = getattr(message.from_user, "first_name", "") or ""
    last = getattr(message.from_user, "last_name", "") or ""
    username = getattr(message.from_user, "username", "") or ""
    known_users[cid] = {
        "name": (first + " " + last).strip() or "Неизвестно",
        "username": username,
        "last_seen": int(time.time())
    }
    schedule_state_save()

def add_recent_submission(chat_id, category, text):
    info = known_users.get(str(chat_id), {})
    recent_submissions.append({
        "chat_id": chat_id,
        "name": info.get("name", "Неизвестно"),
        "username": info.get("username", ""),
        "category": category,
        "preview": text[:300],
        "time": int(time.time())
    })
    del recent_submissions[:-MAX_RECENT_SUBMISSIONS]
    schedule_state_save()

# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def main_keyboard(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if chat_id == ADMIN_ID:
        markup.row(
            types.KeyboardButton("💬 Написать сообщение"),
            types.KeyboardButton("📋 Последние игроки")
        )
        markup.row(types.KeyboardButton("📋 Последние анкеты"))
    else:
        markup.row(
            types.KeyboardButton("📝 Создать анкету"),
            types.KeyboardButton("📩 Апелляция")
        )
        markup.row(types.KeyboardButton("❓ Помощь"))
    return markup

def category_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🏠 Домовец", callback_data="cat_домовец"),
        types.InlineKeyboardButton("👤 Персонал", callback_data="cat_персонал")
    )
    markup.add(
        types.InlineKeyboardButton("📁 Другое", callback_data="cat_другое")
    )
    return markup

def finish_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(
            "✅ Закончить отправку анкеты",
            callback_data="finish_anketa"
        )
    )
    return markup

def cancel_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("↩️ Отмена", callback_data="admin_cancel")
    )
    return markup

# ============================================================
# ВСПОМОГАТЕЛЬНОЕ
# ============================================================

def cancel_timer(chat_id):
    timer = user_timers.pop(chat_id, None)
    if timer:
        try:
            timer.cancel()
        except Exception:
            pass

def remove_finish_button(chat_id):
    msg_id = user_finish_buttons.pop(chat_id, None)
    if msg_id:
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass

def clear_user(chat_id):
    cancel_timer(chat_id)
    remove_finish_button(chat_id)
    user_buffers.pop(chat_id, None)
    user_media.pop(chat_id, None)
    user_locks.pop(chat_id, None)
    user_categories.pop(chat_id, None)
    user_last_messages.pop(chat_id, None)

def safe_send(chat_id, text, **kwargs):
    kwargs.pop("parse_mode", None)
    return bot.send_message(chat_id, text, **kwargs)

def split_text(text, limit=MAX_TELEGRAM_TEXT):
    if len(text) <= limit:
        return [text]
    result = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < 1000:
            cut = limit
        result.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        result.append(text)
    return result

def send_long(chat_id, text, reply_markup=None):
    chunks = split_text(text)
    last = None
    for i, chunk in enumerate(chunks):
        last = safe_send(
            chat_id,
            chunk,
            reply_markup=reply_markup if i == len(chunks) - 1 else None
        )
    return last

def normalize_category(raw):
    raw = raw.strip().lower()
    if raw == "домовец":
        return "Домовец"
    if raw in ("персонал", "другое"):
        return "Персонал"
    return raw.capitalize()

def get_category(chat_id):
    value = user_categories.get(chat_id)
    if value == "Апелляция":
        return "Апелляция"
    if value in ("Домовец", "Персонал"):
        return value
    return None

def extract_status(report):
    first = report[:500].lower()
    if re.search(r"статус\s*:\s*требует\s+правок", first):
        return False
    if re.search(r"статус\s*:\s*принять", first):
        return True
    return False

def schedule_analysis(chat_id):
    cancel_timer(chat_id)
    timer = threading.Timer(BUFFER_SECONDS, finalize_anketa, args=(chat_id,))
    timer.daemon = True
    user_timers[chat_id] = timer
    timer.start()

# ============================================================
# ИИ
# ============================================================

def analyze_anketa(text, category, appeal=False):
    if not client:
        return None, "Ошибка ИИ: не настроен GROQ_API_KEY."

    extra = ""
    if appeal:
        extra = """
ЭТО АПЕЛЛЯЦИЯ.
Обязательно укажи в отчёте, что это апелляция и окончательное решение
остаётся за администрацией.
"""

    prompt = f"""
КАТЕГОРИЯ: {"Домовец" if category == "Домовец" else "Персонал"}
АПЕЛЛЯЦИЯ: {"ДА" if appeal else "НЕТ"}

АНКЕТА:
----------------
{text}
----------------

{extra}

Проведи проверку строго по SYSTEM PROMPT.
Каждое замечание должно иметь доказательство в тексте.
Если не можешь показать конкретный фрагмент или правило — не пиши замечание.
"""

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            temperature=0.0,
            max_tokens=1400
        )
        report = response.choices[0].message.content.strip()
        return extract_status(report), report
    except Exception as e:
        return None, f"Ошибка ИИ: {type(e).__name__}: {e}"

# ============================================================
# START
# ============================================================

@bot.message_handler(commands=["start"])
def start(message):
    remember_user(message)
    clear_user(message.chat.id)

    if message.chat.id == ADMIN_ID:
        safe_send(
            ADMIN_ID,
            "👑 Админ-панель\n\nВыберите действие:",
            reply_markup=main_keyboard(ADMIN_ID)
        )
    else:
        safe_send(
            message.chat.id,
            WELCOME_TEXT,
            reply_markup=main_keyboard(message.chat.id)
        )

# ============================================================
# МЕНЮ ИГРОКА
# ============================================================

@bot.message_handler(func=lambda m: m.chat.id != ADMIN_ID and m.text in [
    "📝 Создать анкету", "📩 Апелляция", "❓ Помощь"
])
def player_menu(message):
    remember_user(message)
    chat_id = message.chat.id

    if message.text == "📝 Создать анкету":
        clear_user(chat_id)
        safe_send(
            chat_id,
            "Выберите категорию анкеты:",
            reply_markup=category_keyboard()
        )
        return

    if message.text == "📩 Апелляция":
        clear_user(chat_id)
        user_categories[chat_id] = "Апелляция"
        user_buffers[chat_id] = []
        user_media[chat_id] = []

        msg = safe_send(
            chat_id,
            "📩 Апелляция\n\n"
            "Отправьте анкету и материалы несколькими сообщениями.\n"
            "Обязательно добавьте слово «Апелляция» и раздел «Пояснение:».\n\n"
            "Когда закончите — нажмите кнопку ниже.",
            reply_markup=finish_keyboard()
        )
        user_finish_buttons[chat_id] = msg.message_id
        return

    safe_send(
        chat_id,
        "❓ Помощь\n\n"
        "Сначала нажмите «📝 Создать анкету» и выберите категорию.\n"
        "Только после этого бот начнёт принимать текст, фотографии, "
        "музыку и другие материалы.",
        reply_markup=main_keyboard(chat_id)
    )

# ============================================================
# ВЫБОР КАТЕГОРИИ
# ============================================================

@bot.callback_query_handler(func=lambda c: c.data.startswith("cat_"))
def category_choice(call):
    chat_id = call.message.chat.id
    if chat_id == ADMIN_ID:
        bot.answer_callback_query(call.id, "Недоступно для администратора.")
        return

    category = normalize_category(call.data.replace("cat_", "", 1))
    user_categories[chat_id] = category
    user_buffers[chat_id] = []
    user_media[chat_id] = []
    cancel_timer(chat_id)

    bot.answer_callback_query(call.id)

    msg = safe_send(
        chat_id,
        f"Выбрана категория: {category}.\n\n"
        "📌 Теперь отправляйте материалы анкеты.\n"
        "Можно отправлять любое количество текста, фотографий, музыки, "
        "документов, видео и голосовых отдельными сообщениями.\n\n"
        "Кнопка завершения будет переноситься к последнему сообщению.",
        reply_markup=finish_keyboard()
    )
    user_finish_buttons[chat_id] = msg.message_id

# ============================================================
# ПРИЁМ АНКЕТЫ И ВЛОЖЕНИЙ
# ============================================================

@bot.message_handler(content_types=[
    "text", "photo", "document", "audio", "voice",
    "video", "animation", "video_note", "sticker"
])
def receive(message):
    chat_id = message.chat.id
    remember_user(message)

    # Администратор имеет отдельную обработку.
    if chat_id == ADMIN_ID:
        return

    # КЛЮЧЕВОЕ ОГРАНИЧЕНИЕ:
    # пока не нажаты «Создать анкету» и категория,
    # принимается НИЧЕГО — ни текст, ни фото, ни музыка.
    if not get_category(chat_id):
        safe_send(
            chat_id,
            "⚠️ Сначала нажмите «📝 Создать анкету» и выберите категорию.\n\n"
            "До этого бот не принимает сообщения и вложения.",
            reply_markup=main_keyboard(chat_id)
        )
        return

    key = (message.message_id, message.content_type)
    if user_last_messages.get(chat_id) == key:
        return
    user_last_messages[chat_id] = key

    remove_finish_button(chat_id)

    text = (message.text or message.caption or "").strip()
    if text:
        user_buffers.setdefault(chat_id, []).append(text)

    if message.content_type != "text":
        user_media.setdefault(chat_id, []).append(message.message_id)

    schedule_analysis(chat_id)

    text_len = sum(len(x) for x in user_buffers.get(chat_id, []))
    media_count = len(user_media.get(chat_id, []))

    msg = safe_send(
        chat_id,
        f"📨 Получено. Текст: ~{text_len} символов. "
        f"Вложений: {media_count}.\n\n"
        "Отправляйте следующее сообщение или нажмите кнопку завершения.",
        reply_markup=finish_keyboard()
    )
    user_finish_buttons[chat_id] = msg.message_id

# ============================================================
# ЗАВЕРШЕНИЕ
# ============================================================

@bot.callback_query_handler(func=lambda c: c.data == "finish_anketa")
def finish(call):
    chat_id = call.message.chat.id
    if chat_id == ADMIN_ID:
        bot.answer_callback_query(call.id, "Недоступно.")
        return
    bot.answer_callback_query(call.id, "Анкета отправлена на проверку.")
    remove_finish_button(chat_id)
    finalize_anketa(chat_id)

def forward_media(chat_id, source_chat_id, media_ids):
    if not media_ids:
        return

    safe_send(
        ADMIN_ID,
        f"📎 Вложения анкеты: {len(media_ids)} шт."
    )

    for mid in media_ids:
        try:
            bot.forward_message(
                ADMIN_ID,
                source_chat_id,
                mid
            )
        except Exception as e:
            print(
                f"[MEDIA] Ошибка пересылки {mid}: "
                f"{type(e).__name__}: {e}"
            )

# ============================================================
# ФИНАЛИЗАЦИЯ
# ============================================================

def finalize_anketa(chat_id):
    lock = user_locks.setdefault(chat_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return

    try:
        cancel_timer(chat_id)
        remove_finish_button(chat_id)

        parts = user_buffers.get(chat_id, [])
        media_ids = list(user_media.get(chat_id, []))
        category = get_category(chat_id)

        if not category:
            return

        text = "\n\n".join(parts).strip()
        appeal = category == "Апелляция"

        user_buffers.pop(chat_id, None)
        user_media.pop(chat_id, None)

        if len(text) < MIN_ANKETA_LENGTH:
            safe_send(
                chat_id,
                "❌ Недостаточно текста для автоматической проверки.\n\n"
                f"Минимум: {MIN_ANKETA_LENGTH} символов.\n"
                "Добавьте основной текст анкеты и начните подачу заново.",
                reply_markup=main_keyboard(chat_id)
            )
            user_categories.pop(chat_id, None)
            return

        safe_send(
            chat_id,
            "⏳ Анкета собрана. Проверяю обязательные пункты, "
            "объём, ошибки и логику..."
        )

        passed, report = analyze_anketa(
            text,
            category if not appeal else "Домовец",
            appeal
        )

        if passed is None:
            safe_send(
                chat_id,
                "⚠️ Не удалось выполнить автоматическую проверку.\n\n"
                f"{report}\n\n"
                "Анкета НЕ отправлена администрации.",
                reply_markup=main_keyboard(chat_id)
            )
            user_categories.pop(chat_id, None)
            return

        if not passed and not appeal:
            send_long(
                chat_id,
                "⚠️ Ваша анкета требует правок:\n\n"
                f"{report}\n\n"
                "Исправьте недочёты и отправьте анкету заново.\n"
                "Если не согласны — используйте «📩 Апелляция».",
                reply_markup=main_keyboard(chat_id)
            )
            user_categories.pop(chat_id, None)
            return

        info = known_users.get(str(chat_id), {})
        name = info.get("name", "Неизвестно")
        username = info.get("username", "")
        username_text = f"@{username}" if username else "Отсутствует"

        title = (
            "📩 АПЕЛЛЯЦИЯ — АНКЕТА НА РАССМОТРЕНИЕ"
            if appeal else
            "📥 НОВАЯ ГОТОВАЯ АНКЕТА!"
        )

        admin_text = (
            f"{title}\n\n"
            f"👤 Игрок: {name}\n"
            f"🔹 Username: {username_text}\n"
            f"🆔 ID: {chat_id}\n"
            f"📂 Категория: {'Апелляция' if appeal else category}\n\n"
            "========== АНАЛИЗ ИИ ==========\n\n"
            f"{report}\n\n"
            "========== ПОЛНЫЙ ТЕКСТ АНКЕТЫ ==========\n\n"
            f"{text}\n\n"
            f"========== ВЛОЖЕНИЯ ==========\n{len(media_ids)} шт."
        )

        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton(
                "✅ Принять",
                callback_data=f"accept_{chat_id}"
            ),
            types.InlineKeyboardButton(
                "❌ Отклонить",
                callback_data=f"reject_{chat_id}"
            )
        )
        markup.add(
            types.InlineKeyboardButton(
                "💬 Написать",
                callback_data=f"reply_{chat_id}"
            )
        )

        send_long(ADMIN_ID, admin_text, reply_markup=markup)
        forward_media(ADMIN_ID, chat_id, media_ids)
        add_recent_submission(
            chat_id,
            "Апелляция" if appeal else category,
            text
        )

        if appeal:
            safe_send(
                chat_id,
                "📩 Ваша апелляция вместе с анкетой и вложениями "
                "передана администрации. Ожидайте ответа.",
                reply_markup=main_keyboard(chat_id)
            )
        else:
            safe_send(
                chat_id,
                "✅ Автоматическая проверка пройдена.\n\n"
                "Ваша анкета и все вложения отправлены администрации.",
                reply_markup=main_keyboard(chat_id)
            )

        user_categories.pop(chat_id, None)

    except Exception as e:
        print(f"[FINALIZE] Ошибка: {type(e).__name__}: {e}")
        try:
            safe_send(
                chat_id,
                "⚠️ Произошла техническая ошибка при обработке анкеты. "
                "Попробуйте ещё раз позже.",
                reply_markup=main_keyboard(chat_id)
            )
        except Exception:
            pass
    finally:
        lock.release()

# ============================================================
# АДМИНСКОЕ МЕНЮ
# ============================================================

@bot.message_handler(func=lambda m: m.chat.id == ADMIN_ID and m.text in [
    "💬 Написать сообщение",
    "📋 Последние игроки",
    "📋 Последние анкеты"
])
def admin_menu(message):
    if message.text == "💬 Написать сообщение":
        msg = safe_send(
            ADMIN_ID,
            "💬 Введите Telegram ID игрока или его @username:",
            reply_markup=cancel_keyboard()
        )
        bot.register_next_step_handler(msg, admin_target)
        return

    if message.text == "📋 Последние игроки":
        if not known_users:
            safe_send(
                ADMIN_ID,
                "📭 Игроков пока нет.",
                reply_markup=main_keyboard(ADMIN_ID)
            )
            return

        lines = ["👥 Последние игроки:\n"]
        for cid, info in list(known_users.items())[-20:][::-1]:
            username = info.get("username", "")
            lines.append(
                f"• {info.get('name', 'Неизвестно')} — "
                f"{('@' + username) if username else 'без username'} — ID {cid}"
            )

        safe_send(
            ADMIN_ID,
            "\n".join(lines),
            reply_markup=main_keyboard(ADMIN_ID)
        )
        return

    if message.text == "📋 Последние анкеты":
        if not recent_submissions:
            safe_send(
                ADMIN_ID,
                "📭 Анкет пока нет.",
                reply_markup=main_keyboard(ADMIN_ID)
            )
            return

        lines = ["📋 Последние анкеты:\n"]
        for item in recent_submissions[-10:][::-1]:
            lines.append(
                f"• {item.get('name', 'Неизвестно')} — "
                f"{item.get('category', '—')} — "
                f"ID {item.get('chat_id')}"
            )

        safe_send(
            ADMIN_ID,
            "\n".join(lines),
            reply_markup=main_keyboard(ADMIN_ID)
        )

def find_target(raw):
    raw = (raw or "").strip()

    if raw.startswith("@"):
        wanted = raw[1:].lower()
        for cid, info in known_users.items():
            if info.get("username", "").lower() == wanted:
                return int(cid)
        return None

    try:
        return int(raw)
    except ValueError:
        return None

def admin_target(message):
    if message.from_user.id != ADMIN_ID:
        return

    if message.text == "↩️ Отмена":
        safe_send(
            ADMIN_ID,
            "Отменено.",
            reply_markup=main_keyboard(ADMIN_ID)
        )
        return

    target = find_target(message.text)
    if target is None:
        msg = safe_send(
            ADMIN_ID,
            "❌ Не удалось найти игрока.\n\n"
            "Введите Telegram ID или @username ещё раз:",
            reply_markup=cancel_keyboard()
        )
        bot.register_next_step_handler(msg, admin_target)
        return

    info = known_users.get(str(target), {})
    name = info.get("name", "Неизвестно")
    username = info.get("username", "")
    safe_send(
        ADMIN_ID,
        f"👤 Игрок выбран.\n\n"
        f"Имя: {name}\n"
        f"Username: {('@' + username) if username else 'нет'}\n"
        f"ID: {target}\n\n"
        "Теперь нажмите «💬 Написать сообщение».",
        reply_markup=types.InlineKeyboardMarkup()
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(
            "💬 Написать сообщение",
            callback_data=f"admin_msg_{target}"
        )
    )
    markup.add(
        types.InlineKeyboardButton(
            "↩️ Назад",
            callback_data="admin_back"
        )
    )
    # Второе сообщение с кнопками — проще и надёжнее, чем пытаться
    # изменить уже отправленный текст.
    safe_send(
        ADMIN_ID,
        "Выберите действие:",
        reply_markup=markup
    )

# ============================================================
# АДМИНСКИЕ CALLBACK
# ============================================================

@bot.callback_query_handler(func=lambda c: c.data.startswith((
    "accept_", "reject_", "reply_", "admin_msg_",
    "admin_back", "admin_cancel"
)))
def admin_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Нет прав администратора.")
        return

    data = call.data

    if data == "admin_back":
        bot.answer_callback_query(call.id)
        safe_send(
            ADMIN_ID,
            "👑 Админ-панель",
            reply_markup=main_keyboard(ADMIN_ID)
        )
        return

    if data == "admin_cancel":
        bot.answer_callback_query(call.id, "Отменено.")
        safe_send(
            ADMIN_ID,
            "Отменено.",
            reply_markup=main_keyboard(ADMIN_ID)
        )
        return

    if data.startswith("admin_msg_"):
        target = int(data.replace("admin_msg_", "", 1))
        bot.answer_callback_query(call.id)
        msg = safe_send(
            ADMIN_ID,
            f"💬 Введите сообщение для игрока {target}:",
            reply_markup=cancel_keyboard()
        )
        bot.register_next_step_handler(msg, send_admin_message, target)
        return

    action, raw_id = data.split("_", 1)
    try:
        target = int(raw_id)
    except ValueError:
        bot.answer_callback_query(call.id, "Некорректный ID.")
        return

    if action == "accept":
        safe_send(
            target,
            "🎉 Ваша анкета успешно принята администрацией! Поздравляем!"
        )
        bot.answer_callback_query(call.id, "Анкета принята.")
        try:
            bot.edit_message_reply_markup(
                ADMIN_ID,
                call.message.message_id,
                reply_markup=None
            )
        except Exception:
            pass

    elif action == "reject":
        safe_send(
            target,
            "❌ К сожалению, ваша анкета была отклонена администрацией.\n\n"
            "Если хотите узнать причину или не согласны с решением, "
            "обратитесь к @CrazyCrabSalad."
        )
        bot.answer_callback_query(call.id, "Анкета отклонена.")
        try:
            bot.edit_message_reply_markup(
                ADMIN_ID,
                call.message.message_id,
                reply_markup=None
            )
        except Exception:
            pass

    elif action == "reply":
        bot.answer_callback_query(call.id)
        msg = safe_send(
            ADMIN_ID,
            f"💬 Введите сообщение для игрока {target}:",
            reply_markup=cancel_keyboard()
        )
        bot.register_next_step_handler(msg, send_admin_message, target)

def send_admin_message(message, target):
    if message.from_user.id != ADMIN_ID:
        return

    if message.text == "↩️ Отмена":
        safe_send(
            ADMIN_ID,
            "Отправка отменена.",
            reply_markup=main_keyboard(ADMIN_ID)
        )
        return

    text = (message.text or "").strip()
    if not text:
        safe_send(
            ADMIN_ID,
            "❌ Пустое сообщение не отправлено.",
            reply_markup=main_keyboard(ADMIN_ID)
        )
        return

    try:
        safe_send(
            target,
            "💬 Сообщение от администрации:\n\n" + text
        )
        safe_send(
            ADMIN_ID,
            "✅ Сообщение успешно отправлено.",
            reply_markup=main_keyboard(ADMIN_ID)
        )
    except Exception as e:
        safe_send(
            ADMIN_ID,
            f"❌ Не удалось отправить сообщение.\n\n"
            f"{type(e).__name__}: {e}",
            reply_markup=main_keyboard(ADMIN_ID)
        )

# ============================================================
# АДМИНСКИЙ FALLBACK
# ============================================================

@bot.message_handler(func=lambda m: m.chat.id == ADMIN_ID, content_types=["text"])
def admin_fallback(message):
    if message.text not in [
        "💬 Написать сообщение",
        "📋 Последние игроки",
        "📋 Последние анкеты"
    ]:
        safe_send(
            ADMIN_ID,
            "👑 Админ-панель",
            reply_markup=main_keyboard(ADMIN_ID)
        )

# ============================================================
# УСТОЙЧИВЫЙ POLLING
# ============================================================

def polling_worker():
    global polling_alive

    print("[BOT] Поток Telegram polling запущен.")

    while True:
        try:
            print("[BOT] Запускаю Telegram polling...")
            polling_alive = True

            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=25,
                skip_pending=False,
                allowed_updates=["message", "callback_query"]
            )

        except Exception as e:
            polling_alive = False
            print(
                f"[BOT] Polling остановился: "
                f"{type(e).__name__}: {e}"
            )
            print("[BOT] Перезапуск polling через 5 секунд...")
            time.sleep(5)

        finally:
            polling_alive = False

def start_polling():
    global polling_thread
    with polling_lock:
        if polling_thread and polling_thread.is_alive():
            return
        polling_thread = Thread(
            target=polling_worker,
            daemon=True
        )
        polling_thread.start()

# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("[START] Запуск бота...")
    print(f"[START] PID: {os.getpid()}")
    print(f"[START] Файл состояния: {STATE_FILE}")

    load_state()

    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("[START] Flask запущен.")

    start_polling()
    print("[START] Telegram polling запущен.")

    while True:
        time.sleep(60)
