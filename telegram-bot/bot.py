import os
import sqlite3
import logging
import asyncio
import re
from datetime import datetime

import google.generativeai as genai
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

MODEL_FLASH = "gemini-1.5-flash"
MODEL_PRO = "gemini-1.5-pro"
MAX_RETRIES = 3

DB_PATH = os.path.join(os.path.dirname(__file__), "conversations.db")

genai.configure(api_key=GEMINI_API_KEY)


def get_model(model_name: str, system_prompt: str = ""):
    config = genai.GenerationConfig(max_output_tokens=8192)
    if system_prompt:
        return genai.GenerativeModel(
            model_name=model_name,
            generation_config=config,
            system_instruction=system_prompt,
        )
    return genai.GenerativeModel(model_name=model_name, generation_config=config)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            content_type TEXT DEFAULT 'text',
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            model TEXT DEFAULT 'flash',
            system_prompt TEXT DEFAULT '',
            max_history INTEGER DEFAULT 50
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_chat_id
        ON messages(chat_id, timestamp)
    """)
    conn.commit()
    conn.close()


def get_db():
    return sqlite3.connect(DB_PATH)


def save_message(chat_id: int, role: str, content: str, content_type: str = "text"):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO messages (chat_id, role, content, content_type) VALUES (?, ?, ?, ?)",
        (chat_id, role, content, content_type),
    )
    conn.commit()
    conn.close()


def get_history(chat_id: int, limit: int = 50) -> list[dict]:
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY timestamp DESC LIMIT ?",
        (chat_id, limit),
    )
    rows = c.fetchall()
    conn.close()
    rows.reverse()
    return [{"role": r[0], "content": r[1]} for r in rows]


def get_settings(chat_id: int) -> dict:
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT model, system_prompt, max_history FROM settings WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"model": row[0], "system_prompt": row[1], "max_history": row[2]}
    return {"model": "flash", "system_prompt": "", "max_history": 50}


def update_setting(chat_id: int, key: str, value):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO settings (chat_id) VALUES (?) ON CONFLICT(chat_id) DO NOTHING",
        (chat_id,),
    )
    c.execute(f"UPDATE settings SET {key} = ? WHERE chat_id = ?", (value, chat_id))
    conn.commit()
    conn.close()


def clear_history(chat_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def get_model_name(settings: dict) -> str:
    return MODEL_PRO if settings["model"] == "pro" else MODEL_FLASH


def build_chat_history(history: list[dict]) -> list[dict]:
    chat_history = []
    for msg in history[:-1]:
        role = "model" if msg["role"] == "assistant" else "user"
        chat_history.append({"role": role, "parts": [msg["content"]]})
    return chat_history


async def generate_with_retry(model, chat_history, message_parts):
    for attempt in range(MAX_RETRIES):
        try:
            chat = model.start_chat(history=chat_history)
            response = chat.send_message(message_parts)
            return response.text
        except Exception as e:
            error_str = str(e)
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "quota" in error_str.lower():
                wait_match = re.search(r"retry.+?([\d.]+)\s*s", error_str, re.IGNORECASE)
                wait_time = float(wait_match.group(1)) if wait_match else (20 * (attempt + 1))
                wait_time = min(wait_time, 60)
                logger.warning(f"Rate limited, waiting {wait_time:.0f}s (attempt {attempt + 1}/{MAX_RETRIES})")
                await asyncio.sleep(wait_time)
            else:
                raise
    raise Exception("Max retries exceeded for Gemini API")


async def generate_content_with_retry(model, parts):
    for attempt in range(MAX_RETRIES):
        try:
            response = model.generate_content(parts)
            return response.text
        except Exception as e:
            error_str = str(e)
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "quota" in error_str.lower():
                wait_match = re.search(r"retry.+?([\d.]+)\s*s", error_str, re.IGNORECASE)
                wait_time = float(wait_match.group(1)) if wait_match else (20 * (attempt + 1))
                wait_time = min(wait_time, 60)
                logger.warning(f"Rate limited, waiting {wait_time:.0f}s (attempt {attempt + 1}/{MAX_RETRIES})")
                await asyncio.sleep(wait_time)
            else:
                raise
    raise Exception("Max retries exceeded for Gemini API")


async def send_reply(update: Update, reply: str):
    if len(reply) > 4096:
        for i in range(0, len(reply), 4096):
            await update.message.reply_text(reply[i : i + 4096])
    else:
        await update.message.reply_text(reply)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "مرحباً! أنا بوت ذكي مدعوم بـ Gemini AI.\n\n"
        "الأوامر المتاحة:\n"
        "/start - بدء المحادثة\n"
        "/clear - مسح سجل المحادثة\n"
        "/model - تبديل الموديل (flash/pro)\n"
        "/system - تعيين تعليمات النظام\n"
        "/stats - إحصائيات المحادثة\n"
        "/help - المساعدة\n\n"
        "يمكنك:\n"
        "• إرسال نصوص للدردشة\n"
        "• إرسال صور لتحليلها\n"
        "• إرسال رسائل صوتية لتحويلها لنص والرد عليها\n"
        "• إرسال ملفات للتحليل\n"
    )
    await update.message.reply_text(welcome)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = get_settings(update.effective_chat.id)
    model_label = "Pro (تفكير عميق)" if settings["model"] == "pro" else "Flash (سريع)"
    help_text = (
        f"الموديل الحالي: {model_label}\n\n"
        "الميزات:\n"
        "1. دردشة نصية مع ذاكرة محادثة\n"
        "2. تحليل الصور (أرسل صورة مع أو بدون وصف)\n"
        "3. تحليل الرسائل الصوتية\n"
        "4. تحليل الملفات والمستندات\n\n"
        "الأوامر:\n"
        "/model flash - موديل سريع وموفر\n"
        "/model pro - موديل للمهام المعقدة\n"
        "/system <prompt> - تعيين شخصية البوت\n"
        "/clear - مسح الذاكرة\n"
        "/stats - إحصائيات\n"
    )
    await update.message.reply_text(help_text)


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_history(update.effective_chat.id)
    await update.message.reply_text("تم مسح سجل المحادثة بنجاح.")


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if context.args and context.args[0] in ("flash", "pro"):
        model_choice = context.args[0]
        update_setting(chat_id, "model", model_choice)
        label = "Flash (سريع وموفر)" if model_choice == "flash" else "Pro (تفكير عميق)"
        await update.message.reply_text(f"تم تبديل الموديل إلى: {label}")
    else:
        settings = get_settings(chat_id)
        current = "Flash" if settings["model"] == "flash" else "Pro"
        await update.message.reply_text(
            f"الموديل الحالي: {current}\n\n"
            "للتبديل استخدم:\n"
            "/model flash - للردود السريعة\n"
            "/model pro - للمهام المعقدة"
        )


async def system_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if context.args:
        prompt_text = " ".join(context.args)
        if prompt_text.lower() == "reset":
            update_setting(chat_id, "system_prompt", "")
            await update.message.reply_text("تم مسح تعليمات النظام.")
        else:
            update_setting(chat_id, "system_prompt", prompt_text)
            await update.message.reply_text(f"تم تعيين تعليمات النظام:\n{prompt_text}")
    else:
        settings = get_settings(chat_id)
        if settings["system_prompt"]:
            await update.message.reply_text(
                f"التعليمات الحالية:\n{settings['system_prompt']}\n\n"
                "لتغييرها: /system <التعليمات الجديدة>\n"
                "لمسحها: /system reset"
            )
        else:
            await update.message.reply_text(
                "لا توجد تعليمات نظام حالياً.\n"
                "لتعيينها: /system <التعليمات>"
            )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,))
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ? AND role = 'user'", (chat_id,))
    user_msgs = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ? AND role = 'assistant'", (chat_id,))
    bot_msgs = c.fetchone()[0]
    c.execute("SELECT MIN(timestamp), MAX(timestamp) FROM messages WHERE chat_id = ?", (chat_id,))
    times = c.fetchone()
    conn.close()

    settings = get_settings(chat_id)
    model_label = "Pro" if settings["model"] == "pro" else "Flash"

    stats_text = (
        f"إحصائيات المحادثة:\n\n"
        f"إجمالي الرسائل: {total}\n"
        f"رسائلك: {user_msgs}\n"
        f"ردود البوت: {bot_msgs}\n"
        f"الموديل: {model_label}\n"
    )
    if times[0]:
        stats_text += f"أول رسالة: {times[0]}\n"
        stats_text += f"آخر رسالة: {times[1]}\n"

    await update.message.reply_text(stats_text)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_text = update.message.text
    settings = get_settings(chat_id)

    save_message(chat_id, "user", user_text)
    history = get_history(chat_id, settings["max_history"])

    model_name = get_model_name(settings)
    model = get_model(model_name, settings["system_prompt"])

    chat_history = build_chat_history(history)
    last_message = history[-1]["content"] if history else user_text

    await update.message.chat.send_action("typing")

    try:
        reply = await generate_with_retry(model, chat_history, last_message)
        reply = reply or "لم أتمكن من توليد رد."
        save_message(chat_id, "assistant", reply)
        await send_reply(update, reply)
    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        await update.message.reply_text("حدث خطأ أثناء معالجة رسالتك. حاول مرة أخرى.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    settings = get_settings(chat_id)
    caption = update.message.caption or "حلل هذه الصورة بالتفصيل."

    await update.message.chat.send_action("typing")

    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        photo_bytes = await file.download_as_bytearray()

        image_data = {
            "mime_type": "image/jpeg",
            "data": bytes(photo_bytes),
        }

        save_message(chat_id, "user", f"[صورة]: {caption}")

        model_name = get_model_name(settings)
        model = get_model(model_name, settings["system_prompt"])

        reply = await generate_content_with_retry(model, [caption, image_data])
        reply = reply or "لم أتمكن من تحليل الصورة."
        save_message(chat_id, "assistant", reply)
        await send_reply(update, reply)
    except Exception as e:
        logger.error(f"Photo analysis error: {e}")
        await update.message.reply_text("حدث خطأ أثناء تحليل الصورة. حاول مرة أخرى.")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    settings = get_settings(chat_id)

    await update.message.chat.send_action("typing")

    try:
        voice = update.message.voice or update.message.audio
        file = await voice.get_file()
        voice_bytes = await file.download_as_bytearray()

        mime_type = "audio/ogg"
        if update.message.audio:
            mime_type = update.message.audio.mime_type or "audio/mpeg"

        audio_data = {
            "mime_type": mime_type,
            "data": bytes(voice_bytes),
        }

        save_message(chat_id, "user", "[رسالة صوتية]")

        model_name = get_model_name(settings)
        model = get_model(model_name, settings["system_prompt"])

        prompt = (
            "استمع لهذه الرسالة الصوتية، حوّلها لنص، ثم أجب على محتواها. "
            "اكتب أولاً ما قاله المستخدم ثم ردك."
        )

        reply = await generate_content_with_retry(model, [prompt, audio_data])
        reply = reply or "لم أتمكن من معالجة الرسالة الصوتية."
        save_message(chat_id, "assistant", reply)
        await send_reply(update, reply)
    except Exception as e:
        logger.error(f"Voice processing error: {e}")
        await update.message.reply_text("حدث خطأ أثناء معالجة الرسالة الصوتية. حاول مرة أخرى.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    settings = get_settings(chat_id)
    caption = update.message.caption or "حلل هذا الملف بالتفصيل."

    await update.message.chat.send_action("typing")

    try:
        doc = update.message.document
        file = await doc.get_file()
        doc_bytes = await file.download_as_bytearray()

        mime_type = doc.mime_type or "application/octet-stream"
        file_name = doc.file_name or "unknown"

        text_mimes = [
            "text/", "application/json", "application/xml",
            "application/javascript", "application/typescript",
            "application/x-python", "application/x-sh",
        ]
        is_text = any(mime_type.startswith(m) for m in text_mimes)

        text_extensions = [
            ".py", ".js", ".ts", ".html", ".css", ".json", ".xml",
            ".yaml", ".yml", ".md", ".txt", ".csv", ".sh", ".sql",
            ".env", ".ini", ".cfg", ".toml", ".rs", ".go", ".java",
            ".c", ".cpp", ".h", ".hpp", ".rb", ".php", ".swift",
        ]
        is_text_ext = any(file_name.lower().endswith(ext) for ext in text_extensions)

        save_message(chat_id, "user", f"[ملف: {file_name}]: {caption}")

        model_name = get_model_name(settings)
        model = get_model(model_name, settings["system_prompt"])

        if is_text or is_text_ext:
            try:
                file_content = doc_bytes.decode("utf-8")
                prompt = (
                    f"اسم الملف: {file_name}\n"
                    f"نوع الملف: {mime_type}\n\n"
                    f"محتوى الملف:\n```\n{file_content[:50000]}\n```\n\n{caption}"
                )
                reply = await generate_content_with_retry(model, [prompt])
            except UnicodeDecodeError:
                file_data = {"mime_type": mime_type, "data": bytes(doc_bytes)}
                reply = await generate_content_with_retry(
                    model, [f"اسم الملف: {file_name}\n{caption}", file_data]
                )
        else:
            file_data = {"mime_type": mime_type, "data": bytes(doc_bytes)}
            reply = await generate_content_with_retry(
                model, [f"اسم الملف: {file_name}\n{caption}", file_data]
            )

        reply = reply or "لم أتمكن من تحليل الملف."
        save_message(chat_id, "assistant", reply)
        await send_reply(update, reply)
    except Exception as e:
        logger.error(f"Document processing error: {e}")
        await update.message.reply_text("حدث خطأ أثناء معالجة الملف. حاول مرة أخرى.")


async def post_init(application: Application):
    commands = [
        BotCommand("start", "بدء المحادثة"),
        BotCommand("help", "المساعدة"),
        BotCommand("clear", "مسح سجل المحادثة"),
        BotCommand("model", "تبديل الموديل (flash/pro)"),
        BotCommand("system", "تعيين تعليمات النظام"),
        BotCommand("stats", "إحصائيات المحادثة"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands registered successfully")


def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set")
        return
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY is not set")
        return

    init_db()
    logger.info("Database initialized")
    logger.info(f"Using models: Flash={MODEL_FLASH}, Pro={MODEL_PRO}")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("model", model_command))
    app.add_handler(CommandHandler("system", system_command))
    app.add_handler(CommandHandler("stats", stats_command))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    logger.info(f"Bot starting... Owner ID: {OWNER_ID}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
