import os
import re
import time
import threading
from threading import Thread
from flask import Flask
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

# Хранилище анкет в памяти.
# Важно: после перезапуска Render незавершённые анкеты исчезнут.
user_buffers = {}
user_timers = {}
user_locks = {}
user_categories = {}
user_last_messages = {}
# Сохраняем исходные Telegram entities (включая Premium/Custom Emoji).
user_entity_buffers = {}
# Фото анкеты: храним file_id, чтобы отправить их одним альбомом.
user_photo_buffers = {}

BUFFER_SECONDS = 30
MIN_ANKETA_LENGTH = 100
MAX_TELEGRAM_TEXT = 4096

WELCOME_TEXT = (
    "👋 Приветствуем!\n\n"
    "Для вступления отправьте сюда вашу анкету одним или несколькими сообщениями.\n\n"
    "📌 Как работает проверка:\n"
    "• Все первичные ошибки и замечания формирует автоматический бот-модератор.\n"
    "• Бот объединяет ваши сообщения в течение 30 секунд в одну анкету — отправляйте части подряд.\n"
    "• После отправки бот сразу же выдаст вам список правок или подтвердит принятие.\n"
    "• Как только ваша анкета будет полностью одобрена ботом, он автоматически отправит её на окончательное рассмотрение администрации.\n"
    "• Если анкета содержит грубые ошибки, выдуманные сведения или не проходит обязательные критерии, администрация её не получит.\n\n"
    "❓ По любым возникшим проблемам и вопросам обращайтесь к @CrazyCrabSalad."
)

SYSTEM_PROMPT = """
Ты — строгий помощник модератора текстовой ролевой игры по фандому
«Дом, в котором».

ВАЖНЕЙШЕЕ ПРАВИЛО:
НЕ ВЫДУМЫВАЙ ЗАМЕЧАНИЯ.
Замечание можно делать только тогда, когда оно прямо подтверждается
текстом анкеты или явно следует из перечисленных ниже обязательных критериев.
Если сомневаешься — НЕ считай это ошибкой.
Не требуй того, чего нет в критериях.
Не придумывай требования к стилю, биографии, стае или лору.
Не отклоняй анкету только потому, что персонаж тебе не нравится.

КАТЕГОРИИ:
- «Домовец» — проверяется по правилам Домовца.
- «Персонал» — сюда относятся все сотрудники/персонал.
- Если категория «Другое», рассматривай её как ПЕРСОНАЛ.
- Категории «Наружник» больше НЕ существует.

ОБЯЗАТЕЛЬНЫЕ ПУНКТЫ ДЛЯ «ДОМОВЦА»:

1. Кличка.
2. Стая:
   Жрецы, Искры, Гавена, Утопленники, Кометы, Орфы.
   Мистерийцы допустимы только если игрок действительно использует эту стаю
   по актуальному лору.
3. Пол.
4. Возраст — строго 14–18 лет.
5. Заболевание — СТРОГО ОБЯЗАТЕЛЬНЫЙ ПУНКТ.
   Пункт считается выполненным, если:
   а) указано физическое заболевание/инвалидность;
   ИЛИ
   б) в биографии есть логичное объяснение попадания в Дом через
      доплату, связи, взятку, перевод по блату и т.п.
   Если есть хотя бы одно из двух — пункт пройден.
   Не требуй перечисления симптомов, если их нет в правилах.
6. Внешность.
7. Характер — минимум 200 символов.
8. Причина попадания.
9. Возраст попадания.
10. Умения.
11. Юз.

ОБЯЗАТЕЛЬНЫЕ ПУНКТЫ ДЛЯ «ПЕРСОНАЛА»:

1. Кличка.
2. Пол.
3. Возраст — 20+ лет.
4. Внешность.
5. Характер — минимум 200 символов.
6. Предыстория — минимум 50–70 символов.
7. Должность.
8. Юз.

ПРОВЕРКА ПУНКТОВ:
- Не считай пункт отсутствующим, если он просто назван немного иначе,
  но его содержание очевидно.
- Не требуй конкретного порядка пунктов.
- Не требуй идеальной разметки.
- Не придирайся к художественному стилю.
- Не считай обычную фантазию логической ошибкой.
- Логической ошибкой является только грубое противоречие внутри самой анкеты
  или явное нарушение указанного возрастного/категориального критерия.
- Не проверяй «канон» по своим догадкам. Если правило не дано выше,
  не используй его как основание для отказа.

ДУБЛИКАТЫ:
- Если один и тот же пункт явно указан два раза и это не продолжение текста,
  укажи дублирование.
- Если весь текст анкеты или большой фрагмент буквально продублирован подряд,
  укажи, что текст продублирован.
- Не считай обычное повторение слов внутри описания дубликатом пункта.

ОБЪЁМ:
- Характер: минимум 200 символов.
- Предыстория персонала: минимум 50–70 символов.
- Не устанавливай другие минимумы, которых нет в критериях.

ОРФОГРАФИЯ И ПУНКТУАЦИЯ:
- Указывай только ЯВНЫЕ ошибки и опечатки.
- Для каждой ошибки приводи конкретный фрагмент и исправление.
- Не переписывай весь текст ради стилистических предпочтений.
- Если слово может быть авторским, сленговым или намеренно необычным,
  не объявляй его ошибкой без уверенности.
- Явные ошибки вроде «птиць» → «птиц», «видет» → «видит»,
  «недостаточност» → «недостаточность» можно указывать.
- Грубые ошибки в обязательных пунктах важнее мелких запятых.

АНТИ-БРЕД:
Если сообщение не похоже на анкету персонажа, например это набор случайных слов,
спам, бессмысленный текст, попытка обойти проверку или текст без структуры,
статус должен быть «Требует правок».
Но не называй бредом просто короткое/неидеально оформленное описание,
если обязательные пункты реально можно распознать.

АПЕЛЛЯЦИЯ:
Если в анкете встречается слово «Апелляция» и есть раздел
«Пояснение:», считай это заявкой на апелляцию.
Не отклоняй её только за то, что автор не согласен с предыдущим замечанием.
В отчёте отдельно укажи, что это апелляция и что окончательное решение
остаётся за администрацией.

ФОРМАТ ОТВЕТА — СТРОГО:

СТАТУС: [Принять / Требует правок]

ЗАМЕЧАНИЯ ДЛЯ ИГРОКА:
Если есть хотя бы одна подтверждённая проблема:
1. [конкретное замечание]
2. [конкретное замечание]
...
Если подтверждённых проблем НЕТ:
Нет

ОТЧЕТ ДЛЯ АДМИНА:
[Краткое резюме в 2–3 предложениях.]

ПОМНИ:
- Никаких выдуманных ошибок.
- Никаких скрытых критериев.
- Никаких «мне кажется».
- Если всё обязательное есть и явных проблем нет — обязательно «Принять».
"""

# ============================================================
# FLASK / RENDER
# ============================================================

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!", 200

def run_flask():
    port = int(os.environ.get("PORT", 1000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ============================================================
# КНОПКИ
# ============================================================

def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(
        types.KeyboardButton("📝 Отправить анкету"),
        types.KeyboardButton("📩 Апелляция")
    )
    markup.row(
        types.KeyboardButton("❓ Помощь")
    )
    return markup

def get_categories_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton(
            "🏠 Домовец",
            callback_data="cat_домовец"
        ),
        types.InlineKeyboardButton(
            "👤 Персонал",
            callback_data="cat_персонал"
        ),
    )
    return markup

def get_finish_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(
            "✅ Закончить отправку анкеты",
            callback_data="finish_anketa"
        )
    )
    return markup

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def cancel_user_timer(chat_id):
    timer = user_timers.pop(chat_id, None)
    if timer:
        try:
            timer.cancel()
        except Exception:
            pass

def clear_user_buffer(chat_id):
    cancel_user_timer(chat_id)
    user_buffers.pop(chat_id, None)
    user_locks.pop(chat_id, None)
    user_categories.pop(chat_id, None)
    user_last_messages.pop(chat_id, None)

def safe_send(chat_id, text, **kwargs):
    # Telegram Markdown может ломаться на пользовательском тексте.
    # Поэтому сначала пробуем отправить как есть без parse_mode.
    kwargs.pop("parse_mode", None)
    return bot.send_message(chat_id, text, **kwargs)

def split_for_telegram(text, limit=MAX_TELEGRAM_TEXT):
    if len(text) <= limit:
        return [text]

    chunks = []
    current = ""
    for line in text.splitlines(True):
        if len(current) + len(line) <= limit:
            current += line
        else:
            if current:
                chunks.append(current)
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line

    if current:
        chunks.append(current)
    return chunks

def send_long_message(chat_id, text, reply_markup=None):
    chunks = split_for_telegram(text)
    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == len(chunks) - 1 else None
        safe_send(chat_id, chunk, reply_markup=markup)

def normalize_category(raw):
    raw = raw.strip().lower()
    if raw == "домовец":
        return "Домовец"
    if raw in ("персонал", "другое"):
        return "Персонал"
    return raw.capitalize()

def extract_status(report):
    first_300 = report[:500].lower()
    if re.search(r"статус\s*:\s*требует\s+правок", first_300):
        return False
    if re.search(r"статус\s*:\s*принять", first_300):
        return True
    return False

def is_appeal(text):
    low = text.lower()
    return "апелляция" in low and re.search(r"пояснение\s*:", low) is not None

# ============================================================
# ИИ
# ============================================================

def analyze_anketa_with_ai(text, category, appeal=False):
    if not client:
        return None, "Ошибка ИИ: не настроен GROQ_API_KEY."

    category_for_ai = "Домовец" if category == "Домовец" else "Персонал"

    extra = ""
    if appeal:
        extra = """
ЭТО АПЕЛЛЯЦИЯ.
В тексте есть слово «Апелляция» и раздел «Пояснение:».
Не придумывай новые нарушения. Проверь только объективные критерии.
В отчёте обязательно укажи, что это апелляция и что окончательное решение
по спорному вопросу принимает администрация.
"""

    user_prompt = f"""
КАТЕГОРИЯ: {category_for_ai}
АПЕЛЛЯЦИЯ: {"ДА" if appeal else "НЕТ"}

АНКЕТА:
----------------
{text}
----------------

{extra}

Проведи проверку строго по SYSTEM PROMPT.
Каждое замечание должно иметь конкретное доказательство в тексте.
Если не можешь показать конкретный фрагмент или конкретное нарушенное
правило — НЕ ПИШИ такое замечание.

Особенно проверь:
- отсутствие обязательных пунктов;
- возраст;
- минимальные объёмы;
- явные опечатки;
- грубые противоречия;
- явные дубликаты;
- очевидный бред/спам.

Не добавляй требований от себя.
"""

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.0,
            max_tokens=1400
        )
        result = response.choices[0].message.content.strip()
        passed = extract_status(result)
        return passed, result
    except Exception as e:
        return None, f"Ошибка ИИ: {type(e).__name__}: {e}"

# ============================================================
# /START
# ============================================================

@bot.message_handler(commands=["start"])
def send_welcome(message):
    chat_id = message.chat.id
    clear_user_buffer(chat_id)
    user_categories[chat_id] = None
    safe_send(
        chat_id,
        WELCOME_TEXT,
        reply_markup=get_main_keyboard()
    )

# ============================================================
# МЕНЮ
# ============================================================

@bot.message_handler(
    func=lambda msg: msg.text in [
        "📝 Отправить анкету",
        "📩 Апелляция",
        "❓ Помощь"
    ]
)
def handle_menu_click(message):
    chat_id = message.chat.id

    if message.text == "📝 Отправить анкету":
        clear_user_buffer(chat_id)
        user_categories[chat_id] = None
        safe_send(
            chat_id,
            "Выберите категорию анкеты:",
            reply_markup=get_categories_keyboard()
        )
        return

    if message.text == "📩 Апелляция":
        clear_user_buffer(chat_id)
        user_categories[chat_id] = "Апелляция"
        safe_send(
            chat_id,
            "📩 Апелляция\n\n"
            "Отправьте анкету одним или несколькими сообщениями.\n"
            "Обязательно добавьте в текст:\n"
            "• слово «Апелляция»;\n"
            "• раздел «Пояснение:» — здесь аргументируйте, почему не согласны "
            "с замечаниями бота.\n\n"
            "Анкета вместе с пояснением будет передана администрации.",
            reply_markup=get_finish_keyboard()
        )
        return

    safe_send(
        chat_id,
        "❓ Помощь\n\n"
        "Если анкета состоит из нескольких частей, отправляйте их подряд "
        "в течение 30 секунд. Когда закончите раньше, нажмите "
        "«✅ Закончить отправку анкеты».\n\n"
        "По вопросам и проблемам: @CrazyCrabSalad",
        reply_markup=get_main_keyboard()
    )

# ============================================================
# ВЫБОР КАТЕГОРИИ
# ============================================================

@bot.callback_query_handler(func=lambda call: call.data.startswith("cat_"))
def handle_category_choice(call):
    chat_id = call.message.chat.id
    raw = call.data.replace("cat_", "", 1)
    category = normalize_category(raw)

    user_categories[chat_id] = category
    user_buffers[chat_id] = []
    user_entity_buffers[chat_id] = []
    user_photo_buffers[chat_id] = []
    cancel_user_timer(chat_id)

    bot.answer_callback_query(call.id)

    safe_send(
        chat_id,
        f"Выбрана категория: {category}.\n\n"
        "📌 Теперь отправляйте анкету.\n"
        "Можно одним или несколькими сообщениями.\n"
        "Бот будет объединять сообщения, которые вы отправите в течение "
        "30 секунд.\n\n"
        "Когда закончите раньше — нажмите кнопку ниже.",
        reply_markup=get_finish_keyboard()
    )

# ============================================================
# СБОР ЧАСТЕЙ АНКЕТЫ
# ============================================================

def schedule_analysis(chat_id):
    cancel_user_timer(chat_id)

    timer = threading.Timer(
        BUFFER_SECONDS,
        finalize_anketa,
        args=(chat_id,)
    )
    timer.daemon = True
    user_timers[chat_id] = timer
    timer.start()

def append_to_buffer(chat_id, text):
    if chat_id not in user_buffers:
        user_buffers[chat_id] = []
    user_buffers[chat_id].append(text)
    schedule_analysis(chat_id)

def get_category_for_chat(chat_id):
    category = user_categories.get(chat_id)
    if category == "Апелляция":
        return "Апелляция"
    if category in ("Домовец", "Персонал"):
        return category
    return None

# ============================================================
# ПРИЁМ ТЕКСТА И ФОТОГРАФИЙ АНКЕТЫ
# ============================================================

def _entity_to_dict(entity):
    """Сохраняет Telegram entity, в том числе custom_emoji_id."""
    data = {
        "type": getattr(entity, "type", None),
        "offset": int(getattr(entity, "offset", 0)),
        "length": int(getattr(entity, "length", 0)),
    }
    custom_id = getattr(entity, "custom_emoji_id", None)
    if custom_id is not None:
        data["custom_emoji_id"] = str(custom_id)
    url = getattr(entity, "url", None)
    if url is not None:
        data["url"] = url
    user = getattr(entity, "user", None)
    if user is not None:
        data["user_id"] = getattr(user, "id", None)
    language = getattr(entity, "language", None)
    if language is not None:
        data["language"] = language
    return data

def _entity_from_dict(data):
    entity = types.MessageEntity(
        type=data.get("type"),
        offset=int(data.get("offset", 0)),
        length=int(data.get("length", 0)),
        url=data.get("url"),
        user=None,
        language=data.get("language"),
        custom_emoji_id=data.get("custom_emoji_id"),
    )
    return entity

def _utf16_len(value):
    return len(str(value).encode("utf-16-le")) // 2

def _combined_entities_from_parts(texts, entity_parts):
    """Объединяет entities нескольких текстовых частей, корректируя UTF-16 offsets."""
    result = []
    offset = 0
    for index, entities in enumerate(entity_parts or []):
        for raw in entities or []:
            item = dict(raw)
            item["offset"] = int(item.get("offset", 0)) + offset
            result.append(item)
        if index < len(texts) - 1:
            offset += _utf16_len(texts[index]) + _utf16_len("\n\n")
        elif index < len(texts):
            offset += _utf16_len(texts[index])
    return result

def _combined_entities(chat_id):
    return _combined_entities_from_parts(
        user_buffers.get(chat_id, []),
        user_entity_buffers.get(chat_id, [])
    )

def _append_anketa_message(message):
    chat_id = message.chat.id
    raw_text = message.text or message.caption or ""
    text = raw_text.strip()

    msg_key = (message.message_id, message.content_type, raw_text)
    if user_last_messages.get(chat_id) == msg_key:
        return False
    user_last_messages[chat_id] = msg_key

    if text:
        append_to_buffer(chat_id, text)
        entities = [_entity_to_dict(e) for e in (message.entities or message.caption_entities or [])]
        user_entity_buffers.setdefault(chat_id, []).append(entities)
    elif message.content_type == "photo":
        # Для фото без подписи entities не нужны, но порядок сохраняем отдельно.
        pass

    if message.content_type == "photo" and message.photo:
        # Берём самое качественное доступное фото.
        user_photo_buffers.setdefault(chat_id, []).append(message.photo[-1].file_id)

    schedule_analysis(chat_id)
    return True

@bot.message_handler(
    content_types=["text", "photo"],
    func=lambda msg: (
        msg.chat.id in user_categories
        and get_category_for_chat(msg.chat.id) is not None
        and (msg.text or msg.caption or "") not in [
            "📝 Отправить анкету",
            "📩 Апелляция",
            "❓ Помощь"
        ]
    )
)
def receive_anketa_part(message):
    chat_id = message.chat.id
    if not _append_anketa_message(message):
        return

    total_len = sum(len(x) for x in user_buffers.get(chat_id, []))
    photo_count = len(user_photo_buffers.get(chat_id, []))

    safe_send(
        chat_id,
        f"📨 Часть анкеты получена.\n"
        f"Сейчас собрано примерно {total_len} символов.\n"
        f"📸 Фотографий: {photo_count}.\n\n"
        "Если это ещё не всё — отправляйте следующую часть.\n"
        "Если закончили — нажмите «✅ Закончить отправку анкеты».\n"
        f"Если ничего больше не отправите, бот автоматически начнёт проверку "
        f"через {BUFFER_SECONDS} секунд."
    )

# ============================================================
# ДОСРОЧНОЕ ЗАВЕРШЕНИЕ
# ============================================================

@bot.callback_query_handler(func=lambda call: call.data == "finish_anketa")
def finish_anketa_callback(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id, "Анкета отправлена на проверку.")
    finalize_anketa(chat_id)

# ============================================================
# ФИНАЛИЗАЦИЯ
# ============================================================

def finalize_anketa(chat_id):
    lock = user_locks.setdefault(chat_id, threading.Lock())

    if not lock.acquire(blocking=False):
        return

    try:
        cancel_user_timer(chat_id)

        parts = user_buffers.get(chat_id, [])
        entity_parts = user_entity_buffers.get(chat_id, [])
        photo_ids = list(user_photo_buffers.get(chat_id, []))
        category = get_category_for_chat(chat_id)

        if not parts or not category:
            return

        text = "\n\n".join(parts).strip()
        appeal = category == "Апелляция"

        # Сохраняем данные до очистки.
        user_name = user_last_messages.get(chat_id)
        user_buffers.pop(chat_id, None)
        user_entity_buffers.pop(chat_id, None)
        user_photo_buffers.pop(chat_id, None)

        if len(text) < MIN_ANKETA_LENGTH:
            safe_send(
                chat_id,
                "❌ Анкета слишком короткая для проверки.\n\n"
                f"Минимум для запуска проверки: {MIN_ANKETA_LENGTH} символов.\n"
                "Отправьте недостающую информацию и начните подачу анкеты заново.",
                reply_markup=get_main_keyboard()
            )
            user_categories.pop(chat_id, None)
            return

        safe_send(
            chat_id,
            "⏳ Анкета собрана. Проверяю обязательные пункты, "
            "объём, явные ошибки и логику..."
        )

        passed, report = analyze_anketa_with_ai(
            text,
            category if not appeal else "Домовец",
            appeal=appeal
        )

        if passed is None:
            safe_send(
                chat_id,
                "⚠️ Не удалось выполнить автоматическую проверку.\n\n"
                f"{report}\n\n"
                "Анкета НЕ отправлена администрации. "
                "Попробуйте отправить её ещё раз позже.",
                reply_markup=get_main_keyboard()
            )
            user_categories.pop(chat_id, None)
            return

        if not passed and not appeal:
            appeal_instruction = (
                "\n\n---\n"
                "📌 Не согласны с замечаниями бота?\n"
                "Вы можете подать апелляцию напрямую владельцу. "
                "Для этого снова отправьте анкету через кнопку «📩 Апелляция».\n"
                "Обязательно укажите в тексте:\n"
                "• слово: Апелляция\n"
                "• раздел: Пояснение: (где вы аргументируете свою позицию).\n"
            )

            user_response = (
                "⚠️ Ваша анкета содержит замечания и требует правок:\n\n"
                f"{report}\n\n"
                "📌 Пожалуйста, исправьте указанные недочёты и отправьте "
                "исправленный вариант сюда в чат.\n"
                "💡 Если анкета разделена на несколько частей, отправляйте "
                "их подряд в течение 30 секунд — бот объединит их.\n"
                f"{appeal_instruction}\n"
                "*(Если есть противоречия с замечаниями, напишите админам "
                "или подайте апелляцию, чтобы владелец мог принять это во внимание.)*"
            )

            send_long_message(
                chat_id,
                user_response,
                reply_markup=get_main_keyboard()
            )
            user_categories.pop(chat_id, None)
            return

        # Принято ботом либо это апелляция.
        username = (
            f"@{bot.get_chat(chat_id).username}"
            if getattr(bot.get_chat(chat_id), "username", None)
            else "Отсутствует"
        )

        try:
            chat_info = bot.get_chat(chat_id)
            full_name = getattr(chat_info, "full_name", None) or "Неизвестно"
        except Exception:
            full_name = "Неизвестно"

        admin_title = (
            "📩 АПЕЛЛЯЦИЯ — АНКЕТА НА РАССМОТРЕНИЕ"
            if appeal
            else "📥 НОВАЯ ГОТОВАЯ АНКЕТА!"
        )

        admin_prefix = (
            f"{admin_title}\n\n"
            f"👤 Игрок: {full_name}\n"
            f"🔹 Username: {username}\n"
            f"🆔 ID: {chat_id}\n"
            f"📂 Категория: {'Апелляция' if appeal else category}\n\n"
            "========== АНАЛИЗ ИИ ==========\n\n"
            f"{report}\n\n"
            "========== ПОЛНЫЙ ТЕКСТ АНКЕТЫ ==========\n\n"
        )
        admin_text = admin_prefix + text

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
                "💬 Ответить",
                callback_data=f"reply_{chat_id}"
            )
        )

        # Если анкета помещается в одно сообщение, отправляем её с исходными
        # Telegram entities. Это сохраняет Premium/Custom Emoji и форматирование.
        combined_entities = _combined_entities_from_parts(parts, entity_parts)
        if _utf16_len(admin_text) <= MAX_TELEGRAM_TEXT:
            shifted = []
            prefix_len = _utf16_len(admin_prefix)
            for raw in combined_entities:
                item = dict(raw)
                item["offset"] = int(item.get("offset", 0)) + prefix_len
                shifted.append(_entity_from_dict(item))
            try:
                bot.send_message(
                    ADMIN_ID,
                    admin_text,
                    entities=shifted or None,
                    parse_mode=None,
                    reply_markup=markup
                )
            except Exception as e:
                print(f"[ENTITIES] Fallback: {type(e).__name__}: {e}")
                send_long_message(ADMIN_ID, admin_text, reply_markup=markup)
        else:
            # Для очень длинной анкеты сохраняем привычный служебный блок,
            # а сам текст отправляем отдельными сообщениями с entities.
            send_long_message(ADMIN_ID, admin_prefix, reply_markup=markup)
            for part_text, raw_entities in zip(parts, entity_parts):
                try:
                    bot.send_message(
                        ADMIN_ID,
                        part_text,
                        entities=[_entity_from_dict(x) for x in raw_entities] or None,
                        parse_mode=None
                    )
                except Exception:
                    send_long_message(ADMIN_ID, part_text)

        # Фотографии отправляем одним Telegram-альбомом.
        if photo_ids:
            for start in range(0, len(photo_ids), 10):
                chunk = photo_ids[start:start + 10]
                media = [types.InputMediaPhoto(file_id) for file_id in chunk]
                try:
                    bot.send_media_group(ADMIN_ID, media)
                except Exception as e:
                    print(f"[MEDIA] Ошибка отправки альбома: {type(e).__name__}: {e}")
                    for file_id in chunk:
                        try:
                            bot.send_photo(ADMIN_ID, file_id)
                        except Exception as inner:
                            print(f"[MEDIA] Ошибка фото fallback: {type(inner).__name__}: {inner}")

        if appeal:
            safe_send(
                chat_id,
                "📩 Ваша апелляция вместе с анкетой передана администрации "
                "на окончательное рассмотрение.\n\n"
                "Ожидайте ответа владельца.",
                reply_markup=get_main_keyboard()
            )
        else:
            safe_send(
                chat_id,
                "✅ Автоматическая проверка пройдена.\n\n"
                "Ваша анкета вместе с отчётом отправлена администрации "
                "на окончательное рассмотрение.",
                reply_markup=get_main_keyboard()
            )

        user_categories.pop(chat_id, None)

    finally:
        lock.release()

# ============================================================
# АДМИНСКИЕ КНОПКИ
# ============================================================

@bot.callback_query_handler(
    func=lambda call: call.data.startswith(
        ("accept_", "reject_", "reply_")
    )
)
def handle_admin_action(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id,
            "У вас нет прав администратора."
        )
        return

    parts = call.data.split("_", 1)
    if len(parts) != 2:
        bot.answer_callback_query(call.id, "Некорректная команда.")
        return

    action, target_chat_id = parts

    try:
        target_chat_id = int(target_chat_id)
    except ValueError:
        bot.answer_callback_query(call.id, "Некорректный ID.")
        return

    if action == "accept":
        safe_send(
            target_chat_id,
            "🎉 Ваша анкета успешно принята администрацией! Поздравляем!"
        )
        bot.answer_callback_query(call.id, "Анкета принята.")

    elif action == "reject":
        safe_send(
            target_chat_id,
            "❌ К сожалению, ваша анкета была отклонена администрацией.\n\n"
            "Если хотите узнать причину или не согласны с решением, "
            "обратитесь к @CrazyCrabSalad."
        )
        bot.answer_callback_query(call.id, "Анкета отклонена.")

    elif action == "reply":
        msg = safe_send(
            ADMIN_ID,
            f"Введите ответ для пользователя {target_chat_id}:"
        )
        bot.register_next_step_handler(
            msg,
            send_reply_to_user,
            target_chat_id
        )
        bot.answer_callback_query(call.id)

# ============================================================
# ОТВЕТ АДМИНА
# ============================================================

def send_reply_to_user(message, target_chat_id):
    if message.from_user.id != ADMIN_ID:
        return

    text = message.text or ""
    if not text.strip():
        safe_send(ADMIN_ID, "Пустое сообщение не отправлено.")
        return

    safe_send(
        target_chat_id,
        "💬 Сообщение от администрации:\n\n" + text
    )
    safe_send(
        ADMIN_ID,
        "✅ Сообщение успешно доставлено пользователю."
    )

# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    print("🔥 1. PROGRAM STARTED")

    Thread(target=run_flask, daemon=True).start()
    print("🔥 2. FLASK THREAD STARTED")

    print("🔥 3. TOKEN EXISTS:", bool(BOT_TOKEN))
    print("🔥 4. STARTING POLLING...")

    # Если бот ранее работал через webhook, убираем webhook.
    # Для polling одновременно должен работать только ОДИН экземпляр бота.
    try:
        bot.delete_webhook(drop_pending_updates=False)
        print("🔥 WEBHOOK DELETED")
    except Exception as e:
        print("⚠️ WEBHOOK DELETE ERROR:", e)

    bot.infinity_polling(
        timeout=30,
        long_polling_timeout=30,
        allowed_updates=["message", "callback_query"]
    )
