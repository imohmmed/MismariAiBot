import os
import sqlite3
import logging
import asyncio
import re
from datetime import datetime

from google import genai
from google.genai import types
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

MODEL_FLASH = "gemini-2.5-flash-lite"
MODEL_PRO = "gemini-2.5-flash"
MAX_RETRIES = 4

DB_PATH = os.path.join(os.path.dirname(__file__), "conversations.db")

MISMARI_SYSTEM_INSTRUCTION = """أنت الآن "مسماري" (Mismari).
أنت مساعد ذكاء اصطناعي ذكي، تقني، وودود.
من تطوير المبرمج "@mohmmed".
عندما يسألك شخص عن اسمك، أجب بـ: "أنا مسماري، مساعدك الذكي".
يجب أن تتحدث بلهجة عراقية تقنية مهذبة أو لغة عربية فصحى حسب رغبة المستخدم.
لا تخرج عن هذه الشخصية أبداً.
إذا سُئلت من صنعك أو من طورك، قل: "أنا من تطوير المبرمج محمد (@mohmmed)".
كن مفيداً، دقيقاً، ومختصراً في إجاباتك مع الحفاظ على الجودة."""

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


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


def get_full_system_instruction(custom_prompt: str = "") -> str:
    if custom_prompt:
        return MISMARI_SYSTEM_INSTRUCTION + "\n\nتعليمات إضافية من المستخدم:\n" + custom_prompt
    return MISMARI_SYSTEM_INSTRUCTION


def build_contents(history: list[dict]) -> list[types.Content]:
    contents = []
    for msg in history:
        role = "model" if msg["role"] == "assistant" else "user"
        contents.append(types.Content(
            role=role,
            parts=[types.Part.from_text(text=msg["content"])]
        ))
    return contents


def make_config(system_instruction: str) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        system_instruction=system_instruction,
        max_output_tokens=8192,
    )


async def generate_with_retry(model_name: str, contents, config):
    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
            return response.text or ""
        except Exception as e:
            error_str = str(e)
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "quota" in error_str.lower():
                if "limit: 0" in error_str or "per_day" in error_str.lower() or "PerDay" in error_str:
                    raise Exception("QUOTA_EXHAUSTED_DAILY")
                wait_match = re.search(r"([\d.]+)\s*s", error_str)
                wait_time = float(wait_match.group(1)) if wait_match else (15 * (attempt + 1))
                wait_time = min(max(wait_time, 5), 60)
                logger.warning(f"Rate limited, waiting {wait_time:.0f}s (attempt {attempt + 1}/{MAX_RETRIES})")
                await asyncio.sleep(wait_time)
            else:
                logger.error(f"Gemini API error: {error_str}")
                raise
    raise Exception("Max retries exceeded for Gemini API")


async def send_reply(update: Update, reply: str):
    if len(reply) > 4096:
        for i in range(0, len(reply), 4096):
            await update.message.reply_text(reply[i : i + 4096])
    else:
        await update.message.reply_text(reply)


def get_error_message(e: Exception) -> str:
    error_str = str(e)
    if "QUOTA_EXHAUSTED_DAILY" in error_str:
        return (
            "⚠️ نفدت الحصة اليومية المجانية لمفتاح Gemini API.\n"
            "الحصة تتجدد تلقائياً كل يوم.\n"
            "يمكنك المحاولة لاحقاً أو ترقية خطتك في Google AI Studio."
        )
    if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
        return "⚠️ تم تجاوز حد الطلبات. انتظر دقيقة ثم حاول مرة أخرى."
    if "API_KEY_INVALID" in error_str or "401" in error_str:
        return "⚠️ مفتاح API غير صالح. تحقق من المفتاح في الإعدادات."
    return "حدث خطأ أثناء معالجة رسالتك. حاول مرة أخرى."


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "أهلاً بك! أنا مسماري.. رفيقك في عالم التقنية والخدمات الرقمية 🤖\n\n"
        "أنا مساعدك الذكي من تطوير المبرمج محمد (@mohmmed).\n\n"
        "شلون أكدر أساعدك اليوم؟\n\n"
        "يمكنك:\n"
        "• إرسال نصوص للدردشة\n"
        "• إرسال صور لتحليلها\n"
        "• إرسال رسائل صوتية لتحويلها لنص والرد عليها\n"
        "• إرسال ملفات للتحليل\n\n"
        "الأوامر المتاحة:\n"
        "/help - المساعدة\n"
        "/model - تبديل الموديل\n"
        "/system - تعليمات إضافية\n"
        "/clear - مسح سجل المحادثة\n"
        "/stats - إحصائيات المحادثة"
    )
    await update.message.reply_text(welcome)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = get_settings(update.effective_chat.id)
    model_label = "Flash 2.5 (ذكي ومتوازن)" if settings["model"] == "pro" else "Flash Lite 2.5 (سريع جداً)"
    help_text = (
        "أنا مسماري، مساعدك الذكي 🤖\n\n"
        f"الموديل الحالي: {model_label}\n\n"
        "الميزات:\n"
        "1. دردشة نصية مع ذاكرة محادثة\n"
        "2. تحليل الصور (أرسل صورة مع أو بدون وصف)\n"
        "3. تحليل الرسائل الصوتية\n"
        "4. تحليل الملفات والمستندات\n\n"
        "الأوامر:\n"
        "/model flash - Flash Lite سريع جداً (1000 طلب/يوم)\n"
        "/model pro - Flash 2.5 ذكي ومتوازن (250 طلب/يوم)\n"
        "/system <prompt> - تعليمات إضافية للبوت\n"
        "/clear - مسح الذاكرة\n"
        "/stats - إحصائيات\n"
    )
    await update.message.reply_text(help_text)


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_history(update.effective_chat.id)
    await update.message.reply_text("تم مسح سجل المحادثة بنجاح. نبدي من جديد! 🔄")


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if context.args and context.args[0] in ("flash", "pro"):
        model_choice = context.args[0]
        update_setting(chat_id, "model", model_choice)
        label = "Flash Lite 2.5 (سريع جداً)" if model_choice == "flash" else "Flash 2.5 (ذكي ومتوازن)"
        await update.message.reply_text(f"تم تبديل الموديل إلى: {label}")
    else:
        settings = get_settings(chat_id)
        current = "Flash Lite 2.5" if settings["model"] == "flash" else "Flash 2.5"
        await update.message.reply_text(
            f"الموديل الحالي: {current}\n\n"
            "للتبديل استخدم:\n"
            "/model flash - Flash Lite سريع جداً (1000 طلب/يوم)\n"
            "/model pro - Flash 2.5 ذكي ومتوازن (250 طلب/يوم)"
        )


async def system_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if context.args:
        prompt_text = " ".join(context.args)
        if prompt_text.lower() == "reset":
            update_setting(chat_id, "system_prompt", "")
            await update.message.reply_text("تم مسح التعليمات الإضافية. شخصية مسماري الأساسية لا تزال فعّالة.")
        else:
            update_setting(chat_id, "system_prompt", prompt_text)
            await update.message.reply_text(f"تم تعيين تعليمات إضافية:\n{prompt_text}")
    else:
        settings = get_settings(chat_id)
        if settings["system_prompt"]:
            await update.message.reply_text(
                f"التعليمات الإضافية الحالية:\n{settings['system_prompt']}\n\n"
                "لتغييرها: /system <التعليمات الجديدة>\n"
                "لمسحها: /system reset"
            )
        else:
            await update.message.reply_text(
                "لا توجد تعليمات إضافية حالياً (شخصية مسماري الأساسية فعّالة).\n"
                "لإضافة تعليمات: /system <التعليمات>"
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
    model_label = "Flash 2.5" if settings["model"] == "pro" else "Flash Lite 2.5"

    stats_text = (
        f"📊 إحصائيات مسماري:\n\n"
        f"إجمالي الرسائل: {total}\n"
        f"رسائلك: {user_msgs}\n"
        f"ردود مسماري: {bot_msgs}\n"
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
    contents = build_contents(history)
    model_name = get_model_name(settings)
    config = make_config(get_full_system_instruction(settings["system_prompt"]))

    await update.message.chat.send_action("typing")

    try:
        reply = await generate_with_retry(model_name, contents, config)
        reply = reply or "لم أتمكن من توليد رد."
        save_message(chat_id, "assistant", reply)
        await send_reply(update, reply)
    except Exception as e:
        logger.error(f"Text handler error: {e}")
        await update.message.reply_text(get_error_message(e))


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    settings = get_settings(chat_id)
    caption = update.message.caption or "حلل هذه الصورة بالتفصيل."

    await update.message.chat.send_action("typing")

    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        photo_bytes = await file.download_as_bytearray()

        image_part = types.Part.from_bytes(data=bytes(photo_bytes), mime_type="image/jpeg")
        text_part = types.Part.from_text(text=caption)

        save_message(chat_id, "user", f"[صورة]: {caption}")

        history = get_history(chat_id, settings["max_history"] - 1)
        contents = build_contents(history[:-1])
        contents.append(types.Content(role="user", parts=[image_part, text_part]))

        model_name = get_model_name(settings)
        config = make_config(get_full_system_instruction(settings["system_prompt"]))
        reply = await generate_with_retry(model_name, contents, config)
        reply = reply or "لم أتمكن من تحليل الصورة."
        save_message(chat_id, "assistant", reply)
        await send_reply(update, reply)
    except Exception as e:
        logger.error(f"Photo handler error: {e}")
        await update.message.reply_text(get_error_message(e))


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

        audio_part = types.Part.from_bytes(data=bytes(voice_bytes), mime_type=mime_type)
        text_part = types.Part.from_text(
            text="استمع لهذه الرسالة الصوتية، حوّلها لنص، ثم أجب على محتواها. "
            "اكتب أولاً ما قاله المستخدم ثم ردك."
        )

        save_message(chat_id, "user", "[رسالة صوتية]")

        history = get_history(chat_id, settings["max_history"] - 1)
        contents = build_contents(history[:-1])
        contents.append(types.Content(role="user", parts=[audio_part, text_part]))

        model_name = get_model_name(settings)
        config = make_config(get_full_system_instruction(settings["system_prompt"]))
        reply = await generate_with_retry(model_name, contents, config)
        reply = reply or "لم أتمكن من معالجة الرسالة الصوتية."
        save_message(chat_id, "assistant", reply)
        await send_reply(update, reply)
    except Exception as e:
        logger.error(f"Voice handler error: {e}")
        await update.message.reply_text(get_error_message(e))


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

        history = get_history(chat_id, settings["max_history"] - 1)
        contents = build_contents(history[:-1])

        if is_text or is_text_ext:
            try:
                file_content = doc_bytes.decode("utf-8")
                prompt = (
                    f"اسم الملف: {file_name}\n"
                    f"نوع الملف: {mime_type}\n\n"
                    f"محتوى الملف:\n```\n{file_content[:50000]}\n```\n\n{caption}"
                )
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=prompt)]
                ))
            except UnicodeDecodeError:
                file_part = types.Part.from_bytes(data=bytes(doc_bytes), mime_type=mime_type)
                text_part = types.Part.from_text(text=f"اسم الملف: {file_name}\n{caption}")
                contents.append(types.Content(role="user", parts=[file_part, text_part]))
        else:
            file_part = types.Part.from_bytes(data=bytes(doc_bytes), mime_type=mime_type)
            text_part = types.Part.from_text(text=f"اسم الملف: {file_name}\n{caption}")
            contents.append(types.Content(role="user", parts=[file_part, text_part]))

        model_name = get_model_name(settings)
        config = make_config(get_full_system_instruction(settings["system_prompt"]))
        reply = await generate_with_retry(model_name, contents, config)
        reply = reply or "لم أتمكن من تحليل الملف."
        save_message(chat_id, "assistant", reply)
        await send_reply(update, reply)
    except Exception as e:
        logger.error(f"Document handler error: {e}")
        await update.message.reply_text(get_error_message(e))


async def post_init(application: Application):
    commands = [
        BotCommand("start", "بدء المحادثة مع مسماري"),
        BotCommand("help", "المساعدة"),
        BotCommand("clear", "مسح سجل المحادثة"),
        BotCommand("model", "تبديل الموديل (flash/pro)"),
        BotCommand("system", "تعليمات إضافية"),
        BotCommand("stats", "إحصائيات المحادثة"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands registered successfully")


def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set")
        return
    if client is None:
        logger.error("GEMINI_API_KEY is not set")
        return

    init_db()
    logger.info("Database initialized")
    logger.info(f"Models: Flash={MODEL_FLASH}, Pro={MODEL_PRO}")
    logger.info("Mismari bot using Google AI Studio API key")

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
