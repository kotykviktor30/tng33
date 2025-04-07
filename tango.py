from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ChatMemberHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
import os
import sqlite3
from datetime import datetime
import logging
import asyncio
import uuid
import tempfile
from deep_translator import GoogleTranslator
from translations import translations
import requests
from io import BytesIO
from flask import Flask, request, Response  # Добавляем Flask для Webhook
import threading  # Для запуска Flask и job_queue параллельно

# Инициализация переводчика для поддержки
translator = GoogleTranslator(source='auto', target='ru')

# Настраиваем логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Загружаем переменные из .env файла
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
REGISTER_URL = os.getenv("REGISTER_URL", "https://example.com/register")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
OPERATORS_STR = os.getenv("OPERATORS", "")

# Устанавливаем ссылку на регистрацию для всех языков
for lang in translations:
    translations[lang]["register_url"] = REGISTER_URL

# Парсим операторов из переменной окружения
operator_ids = []
operator_names = {}
if OPERATORS_STR:
    for pair in OPERATORS_STR.split(","):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(":")
        if len(parts) == 2:
            try:
                op_id = int(parts[0].strip())
                op_name = parts[1].strip()
                operator_ids.append(op_id)
                operator_names[op_id] = op_name
            except ValueError:
                logger.warning(f"Cannot parse operator id: {parts[0]}")
        else:
            logger.warning(f"Incorrect format for operator pair: {pair}")

# Словари для поддержки
active_requests = {}
active_conversations = {}
operator_active = {}
waiting_for_question = {}
waiting_for_language = {}
user_languages = {}

# Инициализация Flask и Application
app = Flask(__name__)
application = Application.builder().token(BOT_TOKEN).build()

# Инициализация базы данных SQLite (без изменений)
def init_db():
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    phone_number TEXT,
                    first_start TEXT,
                    language TEXT,
                    is_blocked TEXT DEFAULT 'No',
                    last_interaction TEXT
                 )''')
    c.execute('''CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_type TEXT,
                    language TEXT,
                    text TEXT,
                    image_path TEXT
                 )''')
    c.execute('''CREATE TABLE IF NOT EXISTS scheduled_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT,
                    image_path TEXT,
                    button_text TEXT,
                    button_url TEXT,
                    send_time TEXT,
                    target_lang TEXT,
                    target_users TEXT
                 )''')
    c.execute("PRAGMA table_info(scheduled_posts)")
    columns = [info[1] for info in c.fetchall()]
    if 'target_lang' not in columns:
        c.execute("ALTER TABLE scheduled_posts ADD COLUMN target_lang TEXT")
    if 'target_users' not in columns:
        c.execute("ALTER TABLE scheduled_posts ADD COLUMN target_users TEXT")
    c.execute("SELECT COUNT(*) FROM posts")
    if c.fetchone()[0] == 0:
        posts_data = [
            ("about", "ru", "🎉 <b>Познакомьтесь!</b> 🎉\n\nПопулярная платформа для трансляций, активная с 2009 года, где талантливые стримеры зарабатывают щедрые 💵 доходы.\n\n‼️ Если вы любите быть в центре внимания, общаться, знакомиться с новыми людьми и полны энергии, это место для вас! Заработайте больше, чем на вашей текущей работе, за короткое время. Желаем вам удачи! Мы с радостью примем вас в наше сообщество! 🌟", "https://i.postimg.cc/rp2YMCj0/about-ru.jpg"),
            # Остальные данные без изменений
        ]
        c.executemany("INSERT INTO posts (post_type, language, text, image_path) VALUES (?, ?, ?, ?)", posts_data)
    conn.commit()
    conn.close()

# Функции базы данных и утилиты (без изменений)
def get_post(post_type, language):
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT text, image_path FROM posts WHERE post_type = ? AND language = ?", (post_type, language))
    result = c.fetchone()
    conn.close()
    return result if result else ("Post not found.", None)

def save_user(user_id, username=None, language="en", is_blocked="No", last_interaction=None):
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    if c.fetchone() is None:
        c.execute(
            "INSERT INTO users (user_id, username, first_start, language, is_blocked, last_interaction) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), language, is_blocked,
             last_interaction or datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    else:
        c.execute("UPDATE users SET username = ?, language = ?, is_blocked = ?, last_interaction = ? WHERE user_id = ?",
                  (username, language, is_blocked, last_interaction or datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id))
    conn.commit()
    conn.close()

def get_user_language(user_id):
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else "en"

def is_language_set(user_id):
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT language, first_start FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result and result[0] != "en"

def get_user_stats():
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT user_id, username, phone_number, first_start, language, is_blocked, last_interaction FROM users")
    users = c.fetchall()
    conn.close()
    return users

def get_all_users():
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE is_blocked = 'No'")
    users = c.fetchall()
    conn.close()
    return [user[0] for user in users]

def get_users_by_language(language):
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE language = ? AND is_blocked = 'No'", (language,))
    users = c.fetchall()
    conn.close()
    return [user[0] for user in users]

def save_scheduled_post(text, image_path, button_text, button_url, send_time, target_lang=None, target_users=None):
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO scheduled_posts (text, image_path, button_text, button_url, send_time, target_lang, target_users) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (text, image_path, button_text, button_url, send_time, target_lang, target_users))
    conn.commit()
    conn.close()

def get_scheduled_posts():
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("SELECT id, text, image_path, button_text, button_url, target_lang, target_users FROM scheduled_posts WHERE send_time <= ?", (current_time,))
    posts = c.fetchall()
    c.execute("DELETE FROM scheduled_posts WHERE send_time <= ?", (current_time,))
    conn.commit()
    conn.close()
    return posts

# Функции построения меню (без изменений)
def build_menu(lang, user_id=None):
    if user_id == ADMIN_ID:
        keyboard = [[InlineKeyboardButton(f" {translations[lang]['settings']}", callback_data="settings")]]
    else:
        keyboard = [
            [InlineKeyboardButton(f" {translations[lang]['about']}", callback_data="about"),
             InlineKeyboardButton(f" {translations[lang]['earn']}", callback_data="earn")],
            [InlineKeyboardButton(f" {translations[lang]['withdraw']}", callback_data="withdraw"),
             InlineKeyboardButton(f" {translations[lang]['rules']}", callback_data="rules")],
            [InlineKeyboardButton(f" {translations[lang]['settings']}", callback_data="settings")],
            [InlineKeyboardButton(f" {translations[lang]['register']}", url=translations[lang]["register_url"])]
        ]
        keyboard[2].append(InlineKeyboardButton(f" {translations[lang]['support']}", callback_data="support"))
    logger.info(f"Building menu for language {lang} and user {user_id}: {keyboard}")
    return InlineKeyboardMarkup(keyboard)

def build_lang_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇺🇦 Українська", callback_data="lang_uk")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
        [InlineKeyboardButton("🇹🇷 Türkçe", callback_data="lang_tr")],
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")],
        [InlineKeyboardButton("🇪🇸 Español", callback_data="lang_es")]
    ])

def build_post_lang_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇺🇦 Українська", callback_data="post_lang_uk")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="post_lang_en")],
        [InlineKeyboardButton("🇹🇷 Türkçe", callback_data="post_lang_tr")],
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="post_lang_ru")],
        [InlineKeyboardButton("🇪🇸 Español", callback_data="post_lang_es")],
        [InlineKeyboardButton("На языке пользователя", callback_data="post_lang_user")]
    ])

def build_recipient_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Всем пользователям", callback_data="recipients_all")],
        [InlineKeyboardButton("По языку", callback_data="recipients_by_lang")],
        [InlineKeyboardButton("Конкретным пользователям", callback_data="recipients_specific")]
    ])

def build_recipient_lang_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇺🇦 Українська", callback_data="recipient_lang_uk")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="recipient_lang_en")],
        [InlineKeyboardButton("🇹🇷 Türkçe", callback_data="recipient_lang_tr")],
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="recipient_lang_ru")],
        [InlineKeyboardButton("🇪🇸 Español", callback_data="recipient_lang_es")]
    ])

def build_settings_menu(lang, user_id):
    keyboard = [
        [InlineKeyboardButton(f" {translations[lang]['change_language']}", callback_data="change_language")],
        [InlineKeyboardButton(f" {translations[lang]['back']}", callback_data="back")]
    ]
    if user_id == ADMIN_ID:
        keyboard.insert(1, [InlineKeyboardButton("📝 Создать пост", callback_data="create_post")])
    return InlineKeyboardMarkup(keyboard)

def build_send_time_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Отправить сейчас", callback_data="send_now")],
        [InlineKeyboardButton("Запланировать", callback_data="schedule_post")]
    ])

def build_confirm_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Да, отправить", callback_data="confirm_send")],
        [InlineKeyboardButton("Отмена", callback_data="cancel_send")]
    ])

def build_inline_keyboard_status(request_id: str, lang: str, status: str = "initial"):
    button = InlineKeyboardButton(
        translations[lang]["reply_button"] if status == "initial" else
        translations[lang]["accepted_by_operator"] if status == "accepted" else
        translations[lang]["chat_finished"],
        callback_data=f"reply_{request_id}" if status == "initial" else "none"
    )
    return InlineKeyboardMarkup([[button]])

def build_back_menu(lang):
    return InlineKeyboardMarkup([[InlineKeyboardButton(translations[lang]["back"], callback_data="back")]])

def translate_text(text: str, target_lang: str) -> str:
    try:
        return GoogleTranslator(source='auto', target=target_lang).translate(text)
    except Exception as e:
        logger.error(f"Translation error: {e}")
        return text

def create_chat_history_file(conv: dict) -> str:
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.txt', delete=False) as temp_file:
        temp_file.write(f"История чата с пользователем {conv['username']} (ID: {conv['user_id']}):\n\n")
        chat_history = [(ts, s, c) for ts, s, c in conv.get('chat_history', [])]
        for media_type, file_id, caption, sender, original_msg_id, *rest in conv.get('media_files', []):
            chat_history.append((original_msg_id, sender, f"{media_type}: {caption} (ID: {original_msg_id})"))
        chat_history.sort(key=lambda x: x[0])
        temp_file.write("Сообщения чата:\n")
        for timestamp, sender, content in chat_history:
            time_str = datetime.fromtimestamp(timestamp).strftime('%d.%m.%Y %H:%M:%S')
            sender_name = conv['username'] if sender == 'user' else f"Оператор {conv['operator_name']}"
            if sender == 'user' and conv['language'] != 'ru':
                translated_text = translate_text(content, 'ru')
                temp_file.write(f"[{time_str}] {sender_name}: {content}\nПеревод: {translated_text}\n")
            elif sender == 'operator' and conv['language'] != 'ru':
                translated_text = translate_text(content, conv['language'])
                temp_file.write(f"[{time_str}] {sender_name}: {content}\nПеревод: {translated_text}\n")
            else:
                temp_file.write(f"[{time_str}] {sender_name}: {content}\n")
        temp_file_path = temp_file.name
    return temp_file_path

# Обработчики (без изменений)
async def error_handler(update: Update, context):
    logger.error(f"Update {update} caused error: {context.error}")
    if update and update.message:
        lang = get_user_language(update.message.from_user.id)
        await update.message.reply_text(translations[lang]["error_message"])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    username = update.message.from_user.username
    logger.info(f"User {user_id} ({username}) triggered /start")
    lang = get_user_language(user_id)

    if lang == "en" and not is_language_set(user_id):
        waiting_for_language[user_id] = True
        await update.message.reply_text(translations["ru"]["choose_lang"], reply_markup=build_lang_menu())
        return

    save_user(user_id, username, lang)

    if user_id == ADMIN_ID:
        keyboard = [[InlineKeyboardButton("⚙️ Настройки", callback_data="settings")]]
        await update.message.reply_text(translations[lang]["welcome_admin"], reply_markup=InlineKeyboardMarkup(keyboard))
    elif user_id in operator_ids:
        await update.message.reply_text(translations["ru"]["operator_welcome"])
    elif is_language_set(user_id):
        await update.message.reply_text(translations[lang]["hello"], reply_markup=build_menu(lang, user_id))
    else:
        await update.message.reply_text(f"{translations[lang]['hello']}\n{translations[lang]['choose_lang']}", reply_markup=build_menu(lang, user_id))

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    save_user(user_id, query.from_user.username, get_user_language(user_id))
    logger.info(f"User {user_id} clicked button: {data}")

    if data.startswith("reply_"):
        operator_id = query.from_user.id
        request_id = data.split("reply_")[1]
        logger.info(f"Operator {operator_id} processing reply for request_id: {request_id}")

        if operator_id not in operator_ids:
            await query.answer("Вы не оператор!", show_alert=True)
            return

        if request_id not in active_requests:
            await query.answer("Запрос не найден.", show_alert=True)
            return

        conv = active_requests[request_id]
        if conv.get('assigned_operator') is None:
            conv['assigned_operator'] = operator_id
            conv['operator_name'] = operator_names.get(operator_id, f"Оператор {operator_id}")
            user_id = conv['user_id']
            lang = conv['language']
            active_conversations[user_id] = request_id
            operator_active[operator_id] = request_id

            display_text = f"Новый запрос в поддержку от {conv['username']} (ID: {conv['user_id']}):\n" + "\n".join(
                [content for _, _, content in conv['chat_history']])
            if lang != 'ru':
                translated_text = translate_text("\n".join([content for _, _, content in conv['chat_history']]), 'ru')
                display_text += f"\nПеревод: {translated_text}"

            for op_id, msg_id in conv['operator_messages'].items():
                try:
                    await context.bot.edit_message_text(chat_id=op_id, message_id=msg_id, text=display_text,
                                                        reply_markup=build_inline_keyboard_status(request_id, lang,
                                                                                                  status="accepted"))
                    logger.info(f"Обновлено сообщение для оператора {op_id}")
                except Exception as e:
                    logger.error(f"Ошибка обновления сообщения для оператора {op_id}: {e}")

            await context.bot.send_message(chat_id=user_id, text=translations[lang]["operator_joined"].format(
                name=conv['operator_name']))
            msg = await context.bot.send_message(chat_id=operator_id,
                                                 text=translations["ru"]["operator_request_accepted"])
            conv.setdefault("additional_operator_messages", []).append(
                (operator_id, msg.message_id, translations["ru"]["operator_request_accepted"]))
            await query.answer("Вы подключились к чату!")
        else:
            await query.answer(f"Этот запрос уже принял {conv['operator_name']}.", show_alert=True)
        return

    elif data == "none":
        await query.answer(translations[get_user_language(user_id)]["no_active_chat"])
        return

    elif data == "end_chat":
        await finish_conversation(user_id, context, initiator="operator", update=update)
        return

    if data.startswith("lang_"):
        lang = data.split("_")[1]
        user_languages[user_id] = lang
        save_user(user_id, query.from_user.username, lang)
        await query.edit_message_text(translations[lang]["hello"], reply_markup=build_menu(lang, user_id))
        await query.answer()
        return

    lang = get_user_language(user_id)

    if data in ["about", "earn", "withdraw", "rules"]:
        post_text, image_url = get_post(data, lang)
        try:
            if image_url:
                response = requests.get(image_url, timeout=10)
                response.raise_for_status()
                image_data = BytesIO(response.content)

                await query.message.reply_photo(
                    photo=image_data,
                    caption=post_text,
                    reply_markup=build_back_menu(lang),
                    parse_mode="HTML"
                )
            else:
                await query.message.reply_text(
                    f"{post_text}\n\n{translations[lang]['image_not_found']}",
                    reply_markup=build_back_menu(lang),
                    parse_mode="HTML"
                )
            try:
                await query.delete_message()
            except Exception as e:
                logger.warning(f"Failed to delete message: {e}")
        except requests.RequestException as e:
            logger.error(f"Failed to fetch image for post {data} ({lang}) from {image_url}: {e}")
            await query.message.reply_text(
                f"{post_text}\n\n{translations[lang]['image_not_found']}",
                reply_markup=build_back_menu(lang),
                parse_mode="HTML"
            )
            try:
                await query.delete_message()
            except Exception as e:
                logger.warning(f"Failed to delete message: {e}")
        except Exception as e:
            logger.error(f"Failed to send post {data} ({lang}): {e}")
            await query.message.reply_text(
                translations[lang]["error_message"],
                reply_markup=build_back_menu(lang)
            )
            try:
                await query.delete_message()
            except Exception as e:
                logger.warning(f"Failed to delete message: {e}")
        return

    elif data == "settings":
        await query.edit_message_text(translations[lang]["settings"], reply_markup=build_settings_menu(lang, user_id))
        await query.answer()
        return

    elif data == "support":
        if user_id in waiting_for_question:
            await query.message.reply_text(translations[lang]["waiting_question"])
            return
        if user_id in active_conversations:
            await query.message.reply_text(translations[lang]["already_active"])
            return
        waiting_for_question[user_id] = True
        await query.message.reply_text(translations[lang]["waiting_question"])
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"Failed to delete message: {e}")
        return

    elif data == "change_language":
        await query.edit_message_text(translations[lang]["choose_lang"], reply_markup=build_lang_menu())
        await query.answer()
        return

    elif data == "back":
        await query.message.reply_text(translations[lang]["hello"], reply_markup=build_menu(lang, user_id))
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"Failed to delete message: {e}")
        return

    elif data == "create_post":
        if user_id != ADMIN_ID:
            await query.message.reply_text(translations[lang]["admin_only_message"])
            return
        context.user_data["create_post"] = {"step": "text"}
        await query.message.reply_text(translations[lang]["post_media_prompt"])
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"Failed to delete message: {e}")
        return

    if data.startswith("post_lang_"):
        lang_choice = data.split("_")[2]
        context.user_data["create_post"]["post_lang"] = lang_choice if lang_choice != "user" else None
        context.user_data["create_post"]["step"] = "media"
        await query.message.reply_text(translations[lang]["post_media_prompt"], reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(translations[lang]["skip"], callback_data="skip_media")]]))
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"Failed to delete message: {e}")
        return

    elif data == "recipients_all":
        context.user_data["create_post"]["target_users"] = "all"
        context.user_data["create_post"]["target_lang"] = None
        context.user_data["create_post"]["step"] = "confirm"
        post_data = context.user_data["create_post"]
        preview_text = f"{translations[lang]['post_preview']}\n\n{post_data['text']}"
        if post_data.get("button_text") and post_data.get("button_url"):
            preview_text += f"\n\nКнопка: {post_data['button_text']} ({post_data['button_url']})"
        target_text = "Получатели: Все пользователи"
        if post_data["send_time"] == "now":
            await query.message.reply_text(
                f"{preview_text}\n\n{target_text}\n\n{translations[lang]['post_confirm_send_now']}",
                reply_markup=build_confirm_menu())
        else:
            await query.message.reply_text(
                f"{preview_text}\n\n{target_text}\n\n{translations[lang]['post_confirm_schedule'].format(time=post_data['send_time'])}",
                reply_markup=build_confirm_menu())
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"Failed to delete message: {e}")
        return

    elif data == "recipients_by_lang":
        context.user_data["create_post"]["step"] = "recipient_lang"
        await query.message.reply_text(translations[lang]["post_recipients_prompt"],
                                       reply_markup=build_recipient_lang_menu())
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"Failed to delete message: {e}")
        return

    elif data == "recipients_specific":
        context.user_data["create_post"]["step"] = "recipient_ids"
        await query.message.reply_text(translations[lang]["post_recipient_ids_prompt"])
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"Failed to delete message: {e}")
        return

    elif data.startswith("recipient_lang_"):
        lang_choice = data.split("_")[2]
        context.user_data["create_post"]["target_lang"] = lang_choice
        context.user_data["create_post"]["target_users"] = "by_lang"
        context.user_data["create_post"]["step"] = "confirm"
        post_data = context.user_data["create_post"]
        preview_text = f"{translations[lang]['post_preview']}\n\n{post_data['text']}"
        if post_data.get("button_text") and post_data.get("button_url"):
            preview_text += f"\n\nКнопка: {post_data['button_text']} ({post_data['button_url']})"
        target_text = f"Получатели: Пользователи с языком {lang_choice}"
        if post_data["send_time"] == "now":
            await query.message.reply_text(
                f"{preview_text}\n\n{target_text}\n\n{translations[lang]['post_confirm_send_now']}",
                reply_markup=build_confirm_menu())
        else:
            await query.message.reply_text(
                f"{preview_text}\n\n{target_text}\n\n{translations[lang]['post_confirm_schedule'].format(time=post_data['send_time'])}",
                reply_markup=build_confirm_menu())
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"Failed to delete message: {e}")
        return

    elif data == "skip_media":
        context.user_data["create_post"]["image_path"] = None
        context.user_data["create_post"]["step"] = "button"
        await query.message.reply_text(translations[lang]["post_button_prompt"])
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"Failed to delete message: {e}")
        return

    elif data == "skip_button":
        context.user_data["create_post"]["button_text"] = None
        context.user_data["create_post"]["button_url"] = None
        context.user_data["create_post"]["step"] = "send_time"
        await query.message.reply_text(translations[lang]["post_send_time_prompt"], reply_markup=build_send_time_menu())
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"Failed to delete message: {e}")
        return

    elif data == "send_now":
        context.user_data["create_post"]["send_time"] = "now"
        context.user_data["create_post"]["step"] = "recipients"
        await query.message.reply_text(translations[lang]["post_recipients_prompt"],
                                       reply_markup=build_recipient_menu())
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"Failed to delete message: {e}")
        return

    elif data == "schedule_post":
        context.user_data["create_post"]["step"] = "schedule_time"
        await query.message.reply_text(translations[lang]["post_schedule_time_prompt"])
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"Failed to delete message: {e}")
        return

    elif data == "confirm_send":
        post_data = context.user_data["create_post"]
        target_users = []
        if post_data["target_users"] == "all":
            target_users = get_all_users()
        elif post_data["target_users"] == "by_lang":
            target_users = get_users_by_language(post_data["target_lang"])
        elif post_data["target_users"] == "specific":
            target_users = post_data["specific_users"]

        if post_data["send_time"] == "now":
            for user_id in target_users:
                try:
                    user_lang = get_user_language(user_id) if not post_data.get("post_lang") else post_data["post_lang"]
                    if post_data.get("image_path"):
                        response = requests.get(post_data["image_path"], timeout=10)
                        response.raise_for_status()
                        image_data = BytesIO(response.content)
                        if post_data.get("button_text") and post_data.get("button_url"):
                            await context.bot.send_photo(chat_id=user_id, photo=image_data, caption=post_data["text"],
                                                         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                                             post_data["button_text"], url=post_data["button_url"])]]))
                        else:
                            await context.bot.send_photo(chat_id=user_id, photo=image_data, caption=post_data["text"])
                    else:
                        if post_data.get("button_text") and post_data.get("button_url"):
                            await context.bot.send_message(chat_id=user_id, text=post_data["text"],
                                                           reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                                               post_data["button_text"],
                                                               url=post_data["button_url"])]]))
                        else:
                            await context.bot.send_message(chat_id=user_id, text=post_data["text"])
                except Exception as e:
                    logger.error(f"Failed to send post to user {user_id}: {e}")
            await query.message.reply_text(translations[lang]["post_sent"])
        else:
            save_scheduled_post(post_data["text"], post_data.get("image_path"), post_data.get("button_text"),
                                post_data.get("button_url"), post_data["send_time"], post_data.get("target_lang"),
                                ",".join(map(str, target_users)) if post_data["target_users"] == "specific" else
                                post_data["target_users"])
            await query.message.reply_text(translations[lang]["post_scheduled"].format(time=post_data["send_time"]))
        context.user_data.pop("create_post", None)
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"Failed to delete message: {e}")
        return

    elif data == "cancel_send":
        context.user_data.pop("create_post", None)
        await query.message.reply_text(translations[lang]["post_canceled"])
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"Failed to delete message: {e}")
        return

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text.strip()
    lang = get_user_language(user_id)

    if user_id in operator_ids:
        if user_id in operator_active:
            req_id = operator_active[user_id]
            conv = active_requests.get(req_id)
            if conv:
                conv['last_activity'] = asyncio.get_event_loop().time()
                user_id = conv.get('user_id')
                conv.setdefault("chat_history", []).append((datetime.now().timestamp(), 'operator', text))
                conv.setdefault("additional_operator_messages", []).append((user_id, update.message.message_id, text))
                await context.bot.send_message(chat_id=user_id, text=text)
            else:
                await update.message.reply_text(translations["ru"]["operator_error_chat_not_found"], reply_markup=build_inline_keyboard_status("", "ru", "finished"))
        else:
            await update.message.reply_text(translations["ru"]["operator_wait_for_request"], reply_markup=build_inline_keyboard_status("", "ru", "finished"))
        return

    if user_id in waiting_for_language:
        if text in ["🇷🇺 Русский", "🇬🇧 English", "🇹🇷 Türkçe", "🇪🇸 Español", "🇺🇦 Українська"]:
            lang_map = {"🇷🇺 Русский": "ru", "🇬🇧 English": "en", "🇹🇷 Türkçe": "tr", "🇪🇸 Español": "es", "🇺🇦 Українська": "uk"}
            lang = lang_map[text]
            user_languages[user_id] = lang
            save_user(user_id, update.message.from_user.username, lang)
            await update.message.reply_text(translations[lang]["hello"], reply_markup=build_menu(lang, user_id))
            waiting_for_language.pop(user_id, None)
        else:
            await update.message.reply_text(translations["ru"]["choose_lang"], reply_markup=build_lang_menu())
        return

    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    if result is None:
        waiting_for_language[user_id] = True
        await update.message.reply_text(translations["ru"]["choose_lang"], reply_markup=build_lang_menu())
        return

    if "create_post" in context.user_data:
        step = context.user_data["create_post"]["step"]
        if step == "text":
            context.user_data["create_post"]["text"] = text
            context.user_data["create_post"]["step"] = "post_lang"
            await update.message.reply_text(translations[lang]["post_media_prompt"], reply_markup=build_post_lang_menu())
        elif step == "button":
            if text.lower() == "пропустить":
                context.user_data["create_post"]["button_text"] = None
                context.user_data["create_post"]["button_url"] = None
                context.user_data["create_post"]["step"] = "send_time"
                await update.message.reply_text(translations[lang]["post_send_time_prompt"], reply_markup=build_send_time_menu())
            else:
                context.user_data["create_post"]["button_text"] = text
                context.user_data["create_post"]["step"] = "button_url"
                await update.message.reply_text(translations[lang]["post_button_url_prompt"])
        elif step == "button_url":
            context.user_data["create_post"]["button_url"] = text
            context.user_data["create_post"]["step"] = "send_time"
            await update.message.reply_text(translations[lang]["post_send_time_prompt"], reply_markup=build_send_time_menu())
        elif step == "schedule_time":
            try:
                send_time = datetime.strptime(text, "%Y-%m-%d %H:%M")
                if send_time < datetime.now():
                    await update.message.reply_text(translations[lang]["post_time_error"])
                    return
                context.user_data["create_post"]["send_time"] = send_time.strftime("%Y-%m-%d %H:%M:%S")
                context.user_data["create_post"]["step"] = "recipients"
                await update.message.reply_text(translations[lang]["post_recipients_prompt"], reply_markup=build_recipient_menu())
            except ValueError:
                await update.message.reply_text(translations[lang]["post_time_format_error"])
        elif step == "recipient_ids":
            try:
                user_ids = [int(uid.strip()) for uid in text.split(",")]
                context.user_data["create_post"]["specific_users"] = user_ids
                context.user_data["create_post"]["target_users"] = "specific"
                context.user_data["create_post"]["target_lang"] = None
                context.user_data["create_post"]["step"] = "confirm"
                post_data = context.user_data["create_post"]
                preview_text = f"{translations[lang]['post_preview']}\n\n{post_data['text']}"
                if post_data.get("button_text") and post_data.get("button_url"):
                    preview_text += f"\n\nКнопка: {post_data['button_text']} ({post_data['button_url']})"
                target_text = f"Получатели: {', '.join(map(str, user_ids))}"
                if post_data["send_time"] == "now":
                    await update.message.reply_text(f"{preview_text}\n\n{target_text}\n\n{translations[lang]['post_confirm_send_now']}", reply_markup=build_confirm_menu())
                else:
                    await update.message.reply_text(f"{preview_text}\n\n{target_text}\n\n{translations[lang]['post_confirm_schedule'].format(time=post_data['send_time'])}", reply_markup=build_confirm_menu())
            except ValueError:
                await update.message.reply_text(translations[lang]["post_recipient_ids_error"])
        return

    if user_id in waiting_for_question:
        request_id = str(uuid.uuid4())
        active_requests[request_id] = {
            'user_id': user_id,
            'username': update.message.from_user.first_name,
            'question': text,
            'chat_history': [(datetime.now().timestamp(), 'user', text)],
            'language': lang,
            'assigned_operator': None,
            'operator_name': None,
            'operator_messages': {},
            'additional_operator_messages': [],
            'media_files': [],
            'created_at': asyncio.get_event_loop().time(),
            'last_activity': asyncio.get_event_loop().time()
        }
        active_conversations[user_id] = request_id
        waiting_for_question.pop(user_id, None)

        await update.message.reply_text(translations[lang]["request_sent"])

        display_text = f"Новый запрос в поддержку от {update.message.from_user.first_name} (ID: {user_id}):\n{text}"
        if lang != 'ru':
            translated_text = translate_text(text, 'ru')
            display_text += f"\nПеревод: {translated_text}"

        inline_keyboard = build_inline_keyboard_status(request_id, lang, status="initial")
        target_ids = operator_ids if operator_ids else [ADMIN_ID]
        for op_id in target_ids:
            try:
                msg = await context.bot.send_message(chat_id=op_id, text=display_text, reply_markup=inline_keyboard)
                active_requests[request_id]["operator_messages"][op_id] = msg.message_id
                logger.info(f"Запрос в техподдержку {request_id} отправлен оператору {op_id}")
            except Exception as e:
                logger.error(f"Ошибка отправки оператору {op_id}: {e}")
        return

    if user_id in active_conversations:
        req_id = active_conversations[user_id]
        conv = active_requests.get(req_id)
        if conv:
            conv['last_activity'] = asyncio.get_event_loop().time()
            conv.setdefault('chat_history', []).append((datetime.now().timestamp(), 'user', text))

            if conv.get('assigned_operator') is None:
                display_text = f"Новый запрос в поддержку от {update.message.from_user.first_name} (ID: {user_id}):\n" + "\n".join(
                    [content for _, _, content in conv['chat_history']])
                if lang != 'ru':
                    translated_text = translate_text("\n".join([content for _, _, content in conv['chat_history']]), 'ru')
                    display_text += f"\nПеревод: {translated_text}"

                for op_id, msg_id in conv['operator_messages'].items():
                    try:
                        await context.bot.edit_message_text(chat_id=op_id, message_id=msg_id, text=display_text, reply_markup=build_inline_keyboard_status(req_id, lang, status="initial"))
                        logger.info(f"Обновлено сообщение для оператора {op_id} с запросом {req_id}")
                    except Exception as e:
                        logger.error(f"Ошибка редактирования сообщения для оператора {op_id}: {e}")
                        try:
                            msg = await context.bot.send_message(chat_id=op_id, text=display_text, reply_markup=build_inline_keyboard_status(req_id, lang, status="initial"))
                            conv['operator_messages'][op_id] = msg.message_id
                        except Exception as e:
                            logger.error(f"Ошибка отправки нового сообщения оператору {op_id}: {e}")
            else:
                op_id = conv['assigned_operator']
                display_text = text
                if lang != 'ru':
                    translated_text = translate_text(text, 'ru')
                    display_text = f"{text}\nПеревод: {translated_text}"
                try:
                    msg = await context.bot.send_message(chat_id=op_id, text=display_text)
                    conv.setdefault("additional_operator_messages", []).append((op_id, msg.message_id, display_text))
                except Exception as e:
                    logger.error(f"Ошибка отправки дополнительного сообщения оператору {op_id}: {e}")
        else:
            await update.message.reply_text(translations[lang]["wait_operator"])
    else:
        await update.message.reply_text("Пожалуйста, сначала нажмите кнопку '📞 Поддержка' в меню, чтобы начать чат.", reply_markup=build_menu(lang, user_id))

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    lang = get_user_language(user_id)

    if user_id in operator_ids:
        if user_id in operator_active:
            req_id = operator_active[user_id]
            conv = active_requests[req_id]
            conv['last_activity'] = asyncio.get_event_loop().time()
            user_id = conv['user_id']
            lang = conv['language']
            caption = update.message.caption or translations["ru"]["media_sent"]
            if lang != 'ru':
                caption = translate_text(caption, lang)
            try:
                if update.message.photo:
                    file_id = update.message.photo[-1].file_id
                    sent_msg = await context.bot.send_photo(chat_id=user_id, photo=file_id, caption=caption)
                    conv.setdefault('media_files', []).append(('Фото', file_id, caption, 'operator', update.message.message_id, sent_msg.message_id))
                elif update.message.document:
                    file_id = update.message.document.file_id
                    sent_msg = await context.bot.send_document(chat_id=user_id, document=file_id, caption=caption)
                    conv.setdefault('media_files', []).append(('Документ', file_id, caption, 'operator', update.message.message_id, sent_msg.message_id))
                await context.bot.send_message(chat_id=user_id, text=translations["ru"]["media_sent"])
            except Exception as e:
                logger.error(f"Ошибка отправки медиа от оператора {user_id} юзеру {user_id}: {e}")
                await context.bot.send_message(chat_id=user_id, text=translations[lang]["send_media_error"], reply_markup=build_inline_keyboard_status("", "ru", "finished"))
        else:
            await update.message.reply_text(translations["ru"]["operator_wait_for_request"], reply_markup=build_inline_keyboard_status("", "ru", "finished"))
        return

    if user_id in active_conversations and active_requests[active_conversations[user_id]].get('assigned_operator'):
        req_id = active_conversations[user_id]
        conv = active_requests[req_id]
        conv['last_activity'] = asyncio.get_event_loop().time()
        op_id = conv['assigned_operator']
        caption = update.message.caption or "От пользователя"
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
            msg = await context.bot.send_photo(op_id, file_id, caption=caption)
            conv.setdefault('media_files', []).append(('Фото', file_id, caption, 'user', msg.message_id))
        elif update.message.document:
            file_id = update.message.document.file_id
            msg = await context.bot.send_document(op_id, file_id, caption=caption)
            conv.setdefault('media_files', []).append(('Документ', file_id, caption, 'user', msg.message_id))
    else:
        await update.message.reply_text("Пожалуйста, сначала нажмите кнопку '📞 Поддержка' в меню, чтобы начать чат.", reply_markup=build_menu(lang, user_id))

async def finish_conversation(user_id: int, context: ContextTypes.DEFAULT_TYPE, initiator: str, update: Update = None):
    lang = user_languages.get(user_id, 'ru') if initiator == "user" else 'ru'
    if initiator == "operator":
        req_id = operator_active.get(user_id)
    else:
        req_id = active_conversations.get(user_id)

    if not req_id or req_id not in active_requests:
        if update:
            if user_id in operator_ids:
                await update.message.reply_text(translations["ru"]["operator_no_active_chat"], reply_markup=build_inline_keyboard_status("", "ru", "finished"))
            else:
                await update.message.reply_text(translations[lang]["no_chat_to_end"], reply_markup=build_menu(lang, user_id))
        return

    conv = active_requests[req_id]
    usr_id = conv['user_id']
    op_id = conv.get('assigned_operator')
    active_conversations.pop(usr_id, None)
    if op_id:
        operator_active.pop(op_id, None)

    history_file_path = create_chat_history_file(conv)
    for op_id_key, msg_id in conv.get("operator_messages", {}).items():
        try:
            await context.bot.delete_message(chat_id=op_id_key, message_id=msg_id)
        except Exception as e:
            logger.error(f"Error deleting operator message {msg_id} for operator {op_id_key}: {e}")

    for item in conv.get("additional_operator_messages", []):
        op_id_key, msg_id = item[0], item[1]
        try:
            await context.bot.delete_message(chat_id=op_id_key, message_id=msg_id)
        except Exception as e:
            logger.error(f"Error deleting additional message {msg_id} for operator {op_id_key}: {e}")

    for media in conv.get("media_files", []):
        if len(media) >= 5:
            original_msg_id = media[4]
            sender = media[3]
            original_chat_id = op_id if sender == 'user' else op_id
            try:
                await context.bot.delete_message(chat_id=original_chat_id, message_id=original_msg_id)
            except Exception as e:
                logger.error(f"Error deleting media message {original_msg_id} for {original_chat_id}: {e}")
            if len(media) == 6 and sender == 'operator':
                forwarded_msg_id = media[5]
                try:
                    await context.bot.delete_message(chat_id=usr_id, message_id=forwarded_msg_id)
                except Exception as e:
                    logger.error(f"Error deleting forwarded media {forwarded_msg_id} for {usr_id}: {e}")

    new_text = f"Завершённый чат с {conv['username']} (ID: {conv['user_id']})"
    for op_id_key in operator_ids:
        try:
            with open(history_file_path, 'rb') as file:
                msg = await context.bot.send_document(
                    chat_id=op_id_key,
                    document=file,
                    filename=f"chat_history_{conv['user_id']}_{conv.get('operator_name', 'no_operator')}.txt",
                    caption=new_text,
                    reply_markup=build_inline_keyboard_status(req_id, "ru", status="finished")
                )
            if 'media_files' in conv and conv['media_files']:
                for media_type, file_id, caption, sender, original_msg_id, *rest in conv['media_files']:
                    media_caption = f"{caption} (ID: {original_msg_id})"
                    if media_type == 'Фото':
                        await context.bot.send_photo(chat_id=op_id_key, photo=file_id, caption=media_caption, reply_to_message_id=msg.message_id)
                    elif media_type == 'Документ':
                        await context.bot.send_document(chat_id=op_id_key, document=file_id, caption=media_caption, reply_to_message_id=msg.message_id)
        except Exception as e:
            logger.error(f"Error sending final message to operator {op_id_key}: {e}")

    os.unlink(history_file_path)
    if initiator == "user" and update:
        await update.message.reply_text(translations[lang]["chat_ended_by_user"], reply_markup=build_menu(lang))
    elif initiator == "system":
        await context.bot.send_message(usr_id, translations[lang]["chat_timeout"], reply_markup=build_menu(lang))
    elif initiator == "operator" and op_id:
        await context.bot.send_message(
            usr_id,
            translations[lang]["chat_ended_by_operator"].format(name=conv['operator_name']),
            reply_markup=build_menu(lang)
        )

    if initiator == "operator" and update:
        await update.message.reply_text(translations["ru"]["operator_chat_ended"])
    elif initiator == "user" and op_id:
        await context.bot.send_message(op_id, translations["ru"]["operator_chat_ended_by_user"])
    del active_requests[req_id]

async def check_scheduled_posts(context: ContextTypes.DEFAULT_TYPE):
    posts = get_scheduled_posts()
    for post in posts:
        post_id, text, image_path, button_text, button_url, target_lang, target_users = post
        if target_users == "all":
            users = get_all_users()
        elif target_users == "by_lang":
            users = get_users_by_language(target_lang)
        else:
            users = [int(uid) for uid in target_users.split(",")]
        for user_id in users:
            try:
                user_lang = get_user_language(user_id) if not target_lang else target_lang
                if image_path and os.path.exists(image_path):
                    with open(image_path, 'rb') as photo:
                        if button_text and button_url:
                            await context.bot.send_photo(
                                chat_id=user_id,
                                photo=photo,
                                caption=text,
                                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(button_text, url=button_url)]]),
                                parse_mode="HTML"
                            )
                        else:
                            await context.bot.send_photo(
                                chat_id=user_id,
                                photo=photo,
                                caption=text,
                                parse_mode="HTML"
                            )
                else:
                    if button_text and button_url:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=text,
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(button_text, url=button_url)]]),
                            parse_mode="HTML"
                        )
                    else:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=text,
                            parse_mode="HTML"
                        )
            except Exception as e:
                logger.error(f"Failed to send scheduled post to user {user_id}: {e}")

async def check_timeouts(context: ContextTypes.DEFAULT_TYPE):
    current_time = asyncio.get_event_loop().time()
    for req_id, req in list(active_requests.items()):
        if current_time - req['last_activity'] > 1800:
            user_id = req['user_id']
            await finish_conversation(user_id, context, initiator="system")

async def notify_operators(context: ContextTypes.DEFAULT_TYPE):
    current_time = asyncio.get_event_loop().time()
    for req_id, req in list(active_requests.items()):
        if req.get('assigned_operator') is None and current_time - req['created_at'] > 300:
            for op_id in operator_ids:
                await context.bot.send_message(op_id, "Есть необработанный запрос! Проверьте уведомления.")
            req['created_at'] = current_time

async def track_chat_member(update: Update, context):
    user_id = update.chat_member.from_user.id
    new_status = update.chat_member.new_chat_member.status
    old_status = update.chat_member.old_chat_member.status
    if new_status == "kicked" and old_status != "kicked":
        save_user(user_id, update.chat_member.from_user.username, get_user_language(user_id), is_blocked="Yes")
    elif new_status != "kicked" and old_status == "kicked":
        save_user(user_id, update.chat_member.from_user.username, get_user_language(user_id), is_blocked="No")

async def stats(update: Update, context):
    user_id = update.message.from_user.id
    lang = get_user_language(user_id)
    if user_id != ADMIN_ID:
        await update.message.reply_text(translations[lang]["admin_only_message"])
        return
    users = get_user_stats()
    if not users:
        await update.message.reply_text("Пользователей не найдено.")
        return
    message = "Статистика пользователей:\n\n"
    for user in users:
        user_id, username, phone_number, first_start, language, is_blocked, last_interaction = user
        message += f"ID пользователя: {user_id}\n"
        message += f"Имя пользователя: {username or 'Нет'}\n"
        message += f"Номер телефона: {phone_number or 'Нет'}\n"
        message += f"Первый запуск: {first_start}\n"
        message += f"Язык: {language}\n"
        message += f"Заблокирован: {'Да' if is_blocked == 'Yes' else 'Нет'}\n"
        message += f"Последнее взаимодействие: {last_interaction}\n"
        message += "------------------------\n"
    await update.message.reply_text(message)

async def endchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    lang = get_user_language(user_id)
    if user_id in operator_ids:
        if user_id in operator_active:
            await finish_conversation(user_id, context, initiator="operator", update=update)
        else:
            await update.message.reply_text(translations["ru"]["operator_no_active_chat"], reply_markup=build_inline_keyboard_status("", "ru", "finished"))
    elif user_id in active_conversations:
        await finish_conversation(user_id, context, initiator="user", update=update)
    else:
        await update.message.reply_text(translations[lang]["no_chat_to_end"], reply_markup=build_menu(lang, user_id))

async def set_bot_commands(bot):
    commands = [
        BotCommand("start", "Запустить бота и показать главное меню"),
        BotCommand("stats", "Показать статистику пользователей (только для админа)"),
        BotCommand("endchat", "Завершить текущий чат с поддержкой")
    ]
    await bot.set_my_commands(commands)

# Webhook-обработчик для Telegram
@app.route('/webhook', methods=['POST'])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.process_update(update)  # Убираем await
    return Response(status=200)

# Эндпоинт для пинга (чтобы Render не засыпал)
@app.route('/ping')
def ping():
    return "Bot is alive!"

# Функция для запуска асинхронных задач (job_queue)
async def run_jobs():
    application.job_queue.run_repeating(check_scheduled_posts, interval=60)
    application.job_queue.run_repeating(check_timeouts, interval=60)
    application.job_queue.run_repeating(notify_operators, interval=60)
    while True:
        await asyncio.sleep(1)  # Держим цикл живым

def main():
    # Инициализация базы данных
    init_db()

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button))
    application.add_handler(ChatMemberHandler(track_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_media))
    application.add_handler(CommandHandler("endchat", endchat))
    application.add_error_handler(error_handler)

    # Установка команд бота
    loop = asyncio.get_event_loop()
    loop.run_until_complete(set_bot_commands(application.bot))

    # Запуск job_queue в отдельном потоке
    job_thread = threading.Thread(target=lambda: asyncio.run(run_jobs()))
    job_thread.start()

    # Запуск Flask
    port = int(os.getenv("PORT", 8080))  # Render использует переменную PORT
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    main()
