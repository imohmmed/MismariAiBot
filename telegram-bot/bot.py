import os
import io
import sqlite3
import logging
import asyncio
import re
import hashlib
from datetime import datetime

from google import genai
from google.genai import types
from PIL import Image
from telegram import Update, BotCommand, ForceReply
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
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

MODEL_LITE = "gemini-2.5-flash-lite"
MODEL_SMART = "gemini-2.5-flash"
MAX_RETRIES = 4
MAX_HISTORY = 10
SUMMARY_THRESHOLD = 20
IMAGE_MAX_SIZE = 720
CACHE_SIMILARITY_CHARS = 60

DB_PATH = os.path.join(os.path.dirname(__file__), "conversations.db")

MISMARI_SYSTEM_INSTRUCTION = """أنت الآن "مسماري" (Mismari).
أنت مساعد ذكاء اصطناعي ذكي، تقني، وودود.
من تطوير المبرمج "@mohmmed".
عندما يسألك شخص عن اسمك، أجب بـ: "أنا مسماري، مساعدك الذكي".
يجب أن تتحدث بلهجة عراقية تقنية مهذبة أو لغة عربية فصحى حسب رغبة المستخدم.
لا تخرج عن هذه الشخصية أبداً.
إذا سُئلت من صنعك أو من طورك، قل: "أنا من تطوير المبرمج محمد (@mohmmed)".
كن مفيداً، دقيقاً، ومختصراً في إجاباتك مع الحفاظ على الجودة.

قواعد التنسيق (مهمة جداً - يجب اتباعها دائماً):
- استخدم HTML فقط للتنسيق. لا تستخدم Markdown أبداً.
- للخط العريض: <b>نص</b>
- للخط المائل: <i>نص</i>
- للكود السطري: <code>كود</code>
- لبلوكات الكود متعددة الأسطر: <pre>الكود هنا</pre>
- لا تستخدم ** أو ``` أبداً.
- لا تستخدم # للعناوين.
- استخدم الأسطر الفارغة للفصل بين الفقرات."""

COMPLEX_KEYWORDS = [
    "حلل", "تحليل", "كود", "برمج", "برمجة", "code", "program",
    "اشرح بالتفصيل", "شرح مفصل", "explain", "debug", "خطأ",
    "رياضي", "معادلة", "math", "equation", "algorithm", "خوارزمية",
    "ملخص", "لخص", "summarize", "PDF", "مستند",
    "قارن", "compare", "مقارنة", "تصميم", "design", "architecture",
    "اكتب مقال", "write essay", "بحث", "research",
    "ترجم", "translate", "ترجمة",
]

AWAITING_SYSTEM_PROMPT = 1

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
            system_prompt TEXT DEFAULT '',
            summary TEXT DEFAULT ''
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS response_cache (
            question_hash TEXT PRIMARY KEY,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
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


def get_history(chat_id: int, limit: int = MAX_HISTORY) -> list[dict]:
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


def get_message_count(chat_id: int) -> int:
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,))
    count = c.fetchone()[0]
    conn.close()
    return count


def get_settings(chat_id: int) -> dict:
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT system_prompt, summary FROM settings WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"system_prompt": row[0] or "", "summary": row[1] or ""}
    return {"system_prompt": "", "summary": ""}


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
    update_setting(chat_id, "summary", "")


def get_cached_response(question: str) -> str | None:
    normalized = question.strip().lower()[:CACHE_SIMILARITY_CHARS]
    q_hash = hashlib.md5(normalized.encode()).hexdigest()
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT answer FROM response_cache WHERE question_hash = ?", (q_hash,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def cache_response(question: str, answer: str):
    normalized = question.strip().lower()[:CACHE_SIMILARITY_CHARS]
    q_hash = hashlib.md5(normalized.encode()).hexdigest()
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO response_cache (question_hash, question, answer) VALUES (?, ?, ?)",
        (q_hash, question.strip()[:200], answer),
    )
    conn.commit()
    conn.close()


IDENTITY_PATTERNS = [
    "من أنت", "منو أنت", "شنو اسمك", "ما اسمك", "عرفني بنفسك",
    "من صنعك", "من طورك", "من برمجك", "من سواك",
    "شنو تسوي", "ماذا تفعل", "شلون تشتغل",
]


def is_identity_question(text: str) -> bool:
    text_lower = text.strip().lower()
    return any(p in text_lower for p in IDENTITY_PATTERNS)


def is_complex_query(text: str) -> bool:
    text_lower = text.strip().lower()
    if len(text_lower) > 300:
        return True
    return any(kw in text_lower for kw in COMPLEX_KEYWORDS)


def choose_model(user_text: str, has_media: bool = False) -> str:
    if has_media:
        return MODEL_SMART
    if is_complex_query(user_text):
        return MODEL_SMART
    return MODEL_LITE


def get_full_system_instruction(custom_prompt: str = "") -> str:
    if custom_prompt:
        return MISMARI_SYSTEM_INSTRUCTION + "\n\nتعليمات إضافية من المستخدم:\n" + custom_prompt
    return MISMARI_SYSTEM_INSTRUCTION


def build_contents(history: list[dict], summary: str = "") -> list[types.Content]:
    contents = []
    if summary:
        contents.append(types.Content(
            role="user",
            parts=[types.Part.from_text(text=f"[ملخص المحادثة السابقة]: {summary}")]
        ))
        contents.append(types.Content(
            role="model",
            parts=[types.Part.from_text(text="فهمت، أتذكر سياق محادثتنا السابقة.")]
        ))
    for msg in history:
        role = "model" if msg["role"] == "assistant" else "user"
        contents.append(types.Content(
            role=role,
            parts=[types.Part.from_text(text=msg["content"])]
        ))
    return contents


def make_config(system_instruction: str, max_tokens: int = 8192) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        system_instruction=system_instruction,
        max_output_tokens=max_tokens,
    )


def compress_image(photo_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(photo_bytes))
    if img.mode == "RGBA":
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > IMAGE_MAX_SIZE:
        ratio = IMAGE_MAX_SIZE / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80, optimize=True)
    original_size = len(photo_bytes)
    compressed_size = buf.tell()
    logger.info(f"Image compressed: {original_size // 1024}KB -> {compressed_size // 1024}KB")
    return buf.getvalue()


async def maybe_summarize(chat_id: int, settings: dict):
    msg_count = get_message_count(chat_id)
    if msg_count < SUMMARY_THRESHOLD:
        return

    old_messages = []
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY timestamp ASC LIMIT ?",
        (chat_id, msg_count - MAX_HISTORY),
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        return

    old_text = "\n".join([f"{'المستخدم' if r[0] == 'user' else 'مسماري'}: {r[1][:200]}" for r in rows[:30]])
    existing_summary = settings.get("summary", "")
    summary_prompt = f"لخّص هذه المحادثة في فقرة واحدة مختصرة (3-4 جمل كحد أقصى):\n\n"
    if existing_summary:
        summary_prompt += f"الملخص السابق: {existing_summary}\n\nالرسائل الجديدة:\n"
    summary_prompt += old_text

    try:
        config = make_config(
            "أنت ملخّص محادثات. لخّص المحادثة بإيجاز شديد محافظاً على النقاط المهمة فقط.",
            max_tokens=300,
        )
        summary_contents = [types.Content(
            role="user",
            parts=[types.Part.from_text(text=summary_prompt)]
        )]
        response = client.models.generate_content(
            model=MODEL_LITE,
            contents=summary_contents,
            config=config,
        )
        new_summary = response.text or ""
        if new_summary:
            update_setting(chat_id, "summary", new_summary)
            conn = get_db()
            c = conn.cursor()
            keep_ids = []
            c.execute(
                "SELECT id FROM messages WHERE chat_id = ? ORDER BY timestamp DESC LIMIT ?",
                (chat_id, MAX_HISTORY),
            )
            keep_ids = [row[0] for row in c.fetchall()]
            if keep_ids:
                placeholders = ",".join("?" * len(keep_ids))
                c.execute(
                    f"DELETE FROM messages WHERE chat_id = ? AND id NOT IN ({placeholders})",
                    [chat_id] + keep_ids,
                )
            conn.commit()
            conn.close()
            logger.info(f"Chat {chat_id}: Summarized and pruned old messages")
    except Exception as e:
        logger.warning(f"Summary failed: {e}")


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


def sanitize_html(text: str) -> str:
    text = re.sub(r'```(\w*)\n?(.*?)```', lambda m: f'<pre>{m.group(2)}</pre>', text, flags=re.DOTALL)
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\w)\*(.+?)\*(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    return text


async def send_reply(update: Update, reply: str):
    reply = sanitize_html(reply)
    chunks = []
    if len(reply) > 4096:
        while reply:
            if len(reply) <= 4096:
                chunks.append(reply)
                break
            cut = reply[:4096].rfind('\n')
            if cut == -1 or cut < 100:
                cut = 4096
            chunks.append(reply[:cut])
            reply = reply[cut:].lstrip('\n')
    else:
        chunks = [reply]

    for chunk in chunks:
        try:
            await update.message.reply_text(chunk, parse_mode="HTML")
        except Exception:
            await update.message.reply_text(chunk)


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
        "• إرسال نصوص للدردشة 💬\n"
        "• إرسال صور لتحليلها 📷\n"
        "• إرسال رسائل صوتية 🎤\n"
        "• إرسال ملفات للتحليل 📄\n\n"
        "أكتب /help لعرض جميع الأوامر."
    )
    await update.message.reply_text(welcome)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = get_settings(update.effective_chat.id)
    has_custom = "✅ فعّالة" if settings["system_prompt"] else "❌ غير مضافة"

    help_text = (
        "🤖 أنا مسماري، مساعدك الذكي\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📌 ما أكدر أسويه:\n"
        "• دردشة ذكية مع ذاكرة محادثة\n"
        "• تحليل الصور والملفات\n"
        "• فهم الرسائل الصوتية والرد عليها\n"
        "• أختار الموديل المناسب تلقائياً حسب سؤالك\n\n"
        "⚡ الأوامر المتاحة:\n"
        "/start - بدء المحادثة\n"
        "/system - ضبط تعليمات إضافية لمسماري\n"
        "/clear - مسح سجل المحادثة\n"
        "/stats - إحصائيات المحادثة\n"
        "/help - عرض هذه الرسالة\n\n"
        f"📋 التعليمات الإضافية: {has_custom}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 أرسل أي رسالة وأنا أرد عليك!"
    )
    await update.message.reply_text(help_text)


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_history(update.effective_chat.id)
    await update.message.reply_text("تم مسح سجل المحادثة بنجاح. نبدي من جديد! 🔄")


async def system_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    settings = get_settings(chat_id)

    if context.args:
        prompt_text = " ".join(context.args)
        if prompt_text.lower() == "reset":
            update_setting(chat_id, "system_prompt", "")
            await update.message.reply_text("✅ تم مسح التعليمات الإضافية.\nشخصية مسماري الأساسية لا تزال فعّالة.")
            return ConversationHandler.END

    if settings["system_prompt"]:
        await update.message.reply_text(
            f"📋 التعليمات الإضافية الحالية:\n\n{settings['system_prompt']}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "✏️ اكتب التعليمات الجديدة الآن لتحديثها.\n"
            "أو أرسل /system reset لمسحها.\n"
            "أو أرسل /cancel للإلغاء.",
            reply_markup=ForceReply(selective=True),
        )
    else:
        await update.message.reply_text(
            "📋 لا توجد تعليمات إضافية حالياً.\n\n"
            "✏️ اكتب التعليمات الآن وأرسلها:\n"
            "(مثال: أجب دائماً باللغة الإنجليزية)\n\n"
            "أرسل /cancel للإلغاء.",
            reply_markup=ForceReply(selective=True),
        )
    return AWAITING_SYSTEM_PROMPT


async def receive_system_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    prompt_text = update.message.text.strip()

    if prompt_text.startswith("/"):
        return ConversationHandler.END

    update_setting(chat_id, "system_prompt", prompt_text)
    await update.message.reply_text(
        f"✅ تم حفظ التعليمات الإضافية:\n\n{prompt_text}\n\n"
        "مسماري سيتبع هذه التعليمات من الآن."
    )
    return ConversationHandler.END


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("تم الإلغاء.")
    return ConversationHandler.END


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
    has_summary = "✅" if settings["summary"] else "❌"
    has_custom = "✅" if settings["system_prompt"] else "❌"

    stats_text = (
        f"📊 إحصائيات مسماري:\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💬 إجمالي الرسائل: {total}\n"
        f"👤 رسائلك: {user_msgs}\n"
        f"🤖 ردود مسماري: {bot_msgs}\n"
        f"📋 تعليمات إضافية: {has_custom}\n"
        f"📝 ملخص محادثة: {has_summary}\n"
    )
    if times[0]:
        stats_text += f"\n🕐 أول رسالة: {times[0]}\n"
        stats_text += f"🕐 آخر رسالة: {times[1]}\n"

    await update.message.reply_text(stats_text)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_text = update.message.text
    settings = get_settings(chat_id)

    if is_identity_question(user_text):
        cached = get_cached_response(user_text)
        if cached:
            await update.message.reply_text(cached)
            save_message(chat_id, "user", user_text)
            save_message(chat_id, "assistant", cached)
            return

    save_message(chat_id, "user", user_text)

    await maybe_summarize(chat_id, settings)
    settings = get_settings(chat_id)

    history = get_history(chat_id, MAX_HISTORY)
    contents = build_contents(history, settings["summary"])
    model_name = choose_model(user_text)
    is_simple = (model_name == MODEL_LITE)
    max_tokens = 1024 if (is_simple and len(user_text) < 100) else 8192
    config = make_config(get_full_system_instruction(settings["system_prompt"]), max_tokens)

    await update.message.chat.send_action("typing")

    try:
        reply = await generate_with_retry(model_name, contents, config)
        reply = reply or "لم أتمكن من توليد رد."
        save_message(chat_id, "assistant", reply)

        if is_identity_question(user_text):
            cache_response(user_text, reply)

        await send_reply(update, reply)
        logger.info(f"Chat {chat_id}: model={model_name}, tokens_limit={max_tokens}")
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

        compressed = compress_image(bytes(photo_bytes))

        image_part = types.Part.from_bytes(data=compressed, mime_type="image/jpeg")
        text_part = types.Part.from_text(text=caption)

        save_message(chat_id, "user", f"[صورة]: {caption}")

        history = get_history(chat_id, MAX_HISTORY - 1)
        contents = build_contents(history[:-1], settings["summary"])
        contents.append(types.Content(role="user", parts=[image_part, text_part]))

        model_name = MODEL_SMART
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

        history = get_history(chat_id, MAX_HISTORY - 1)
        contents = build_contents(history[:-1], settings["summary"])
        contents.append(types.Content(role="user", parts=[audio_part, text_part]))

        model_name = MODEL_SMART
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

        history = get_history(chat_id, MAX_HISTORY - 1)
        contents = build_contents(history[:-1], settings["summary"])

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

        model_name = MODEL_SMART
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
        BotCommand("help", "المساعدة والأوامر"),
        BotCommand("system", "ضبط تعليمات إضافية"),
        BotCommand("clear", "مسح سجل المحادثة"),
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
    logger.info(f"Models: Lite={MODEL_LITE}, Smart={MODEL_SMART}")
    logger.info("Mismari bot with smart model routing")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    system_conv = ConversationHandler(
        entry_points=[CommandHandler("system", system_command)],
        states={
            AWAITING_SYSTEM_PROMPT: [
                CommandHandler("cancel", cancel_command),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_system_prompt),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(system_conv)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    logger.info(f"Bot starting... Owner ID: {OWNER_ID}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
