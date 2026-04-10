import os
import io
import json
import sqlite3
import logging
import asyncio
import re
import hashlib
from datetime import datetime

from google import genai
from google.genai import types
from PIL import Image
from telegram import Update, BotCommand, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
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
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "")

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

قواعد التنسيق (مهمة جداً جداً - يجب اتباعها دائماً بدون استثناء):
- تيليكرام يدعم فقط هذه التاگات: <b> <i> <code> <pre> <u> <s> <a>
- للخط العريض: <b>نص</b>
- للخط المائل: <i>نص</i>
- للكود السطري: <code>كود</code>
- لبلوكات الكود متعددة الأسطر: <pre>الكود هنا</pre>
- للعناوين: استخدم <b>العنوان</b> ثم سطر جديد
- للقوائم: استخدم • أو - أو أرقام عادية في بداية كل سطر
- ممنوع تماماً استخدام: <p> <h1> <h2> <h3> <h4> <h5> <h6> <ul> <ol> <li> <div> <span> <br> <table> <tr> <td> <th>
- ممنوع تماماً استخدام Markdown: ** ``` #
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
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            last_name TEXT DEFAULT '',
            first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_active DATETIME DEFAULT CURRENT_TIMESTAMP,
            message_count INTEGER DEFAULT 0,
            is_blocked INTEGER DEFAULT 0
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


def track_user(user):
    if not user or user.is_bot:
        return
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """INSERT INTO users (user_id, username, first_name, last_name)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
               username = excluded.username,
               first_name = excluded.first_name,
               last_name = excluded.last_name,
               last_active = CURRENT_TIMESTAMP,
               message_count = users.message_count + 1""",
        (user.id, user.username or "", user.first_name or "", user.last_name or ""),
    )
    conn.commit()
    conn.close()


async def check_subscription(update: Update, from_callback: bool = False) -> bool:
    if not REQUIRED_CHANNEL:
        return True
    if update.effective_user and update.effective_user.id == OWNER_ID:
        return True
    try:
        channel = REQUIRED_CHANNEL if REQUIRED_CHANNEL.startswith("@") else f"@{REQUIRED_CHANNEL}"
        bot = update.get_bot()
        member = await bot.get_chat_member(
            chat_id=channel,
            user_id=update.effective_user.id,
        )
        logger.info(f"Subscription check for {update.effective_user.id}: status={member.status}")
        if member.status in ("member", "administrator", "creator"):
            return True
    except Exception as e:
        logger.error(f"Subscription check error: {e}")
        pass

    channel_name = REQUIRED_CHANNEL.lstrip('@')
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("اشترك بالقناة", url=f"https://t.me/{channel_name}", api_kwargs={"style": "primary"})],
        [InlineKeyboardButton("تحققت، اشتركت", callback_data="check_sub", api_kwargs={"style": "success"})],
    ])
    sub_text = (
        "⚠️ يجب عليك الاشتراك في القناة أولاً للاستخدام.\n\n"
        "بعد الاشتراك، اضغط الزر بالأسفل للتحقق."
    )

    if from_callback and update.callback_query:
        await update.callback_query.edit_message_text(sub_text, reply_markup=keyboard)
    elif update.message:
        await update.message.reply_text(sub_text, reply_markup=keyboard)
    elif update.callback_query:
        await update.callback_query.answer("⚠️ اشترك بالقناة أولاً!", show_alert=True)
    return False


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
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=MODEL_LITE,
                contents=summary_contents,
                config=config,
            ),
            timeout=30,
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


def get_fallback_model(current_model: str) -> str | None:
    if current_model == MODEL_SMART:
        return MODEL_LITE
    elif current_model == MODEL_LITE:
        return MODEL_SMART
    return None


GEMINI_TIMEOUT = 55
_gemini_semaphore = asyncio.Semaphore(5)


async def generate_with_retry(model_name: str, contents, config):
    current_model = model_name
    tried_fallback = False

    for attempt in range(MAX_RETRIES):
        try:
            async with _gemini_semaphore:
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=current_model,
                        contents=contents,
                        config=config,
                    ),
                    timeout=GEMINI_TIMEOUT,
                )
            return response.text or ""
        except asyncio.TimeoutError:
            logger.warning(f"Gemini timeout after {GEMINI_TIMEOUT}s on {current_model} (attempt {attempt + 1}/{MAX_RETRIES})")
            if not tried_fallback:
                fallback = get_fallback_model(current_model)
                if fallback:
                    logger.warning(f"Timeout on {current_model}, switching to {fallback}")
                    current_model = fallback
                    tried_fallback = True
                    continue
            raise Exception("انتهت مهلة الاستجابة من الذكاء الاصطناعي. حاول مرة أخرى.")
        except Exception as e:
            error_str = str(e)
            if "503" in error_str or "UNAVAILABLE" in error_str:
                if not tried_fallback:
                    fallback = get_fallback_model(current_model)
                    if fallback:
                        logger.warning(f"503 on {current_model}, switching to {fallback}")
                        current_model = fallback
                        tried_fallback = True
                        continue
                wait_time = min(5 * (attempt + 1), 15)
                logger.warning(f"Service unavailable on both models, waiting {wait_time}s")
                await asyncio.sleep(wait_time)
            elif "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "quota" in error_str.lower():
                is_daily_limit = "limit: 0" in error_str or "per_day" in error_str.lower() or "PerDay" in error_str
                if is_daily_limit and not tried_fallback:
                    fallback = get_fallback_model(current_model)
                    if fallback:
                        logger.warning(f"Daily quota exhausted for {current_model}, switching to {fallback}")
                        current_model = fallback
                        tried_fallback = True
                        continue
                if is_daily_limit and tried_fallback:
                    raise Exception("QUOTA_EXHAUSTED_ALL")
                wait_match = re.search(r"([\d.]+)\s*s", error_str)
                wait_time = float(wait_match.group(1)) if wait_match else (15 * (attempt + 1))
                wait_time = min(max(wait_time, 5), 30)
                logger.warning(f"Rate limited on {current_model}, waiting {wait_time:.0f}s (attempt {attempt + 1}/{MAX_RETRIES})")
                await asyncio.sleep(wait_time)
            else:
                if not tried_fallback:
                    fallback = get_fallback_model(current_model)
                    if fallback:
                        logger.warning(f"Error on {current_model}, trying fallback {fallback}: {error_str}")
                        current_model = fallback
                        tried_fallback = True
                        continue
                logger.error(f"Gemini API error: {error_str}")
                raise
    raise Exception("Max retries exceeded for Gemini API")


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_code_blocks(text: str) -> str:
    def escape_pre(m):
        lang = m.group(1) or ""
        code = m.group(2)
        code = escape_html(code)
        return f'<pre>{code}</pre>'

    text = re.sub(r'<pre(?:\s+[^>]*)?>(.*?)</pre>', lambda m: f'<pre>{escape_html(m.group(1))}</pre>', text, flags=re.DOTALL)
    text = re.sub(r'<code>(.*?)</code>', lambda m: f'<code>{escape_html(m.group(1))}</code>', text, flags=re.DOTALL)
    return text


def sanitize_html(text: str) -> str:
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'</?p\s*/?>', '\n', text)
    for tag in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
        text = re.sub(rf'<{tag}[^>]*>(.*?)</{tag}>', r'<b>\1</b>', text, flags=re.DOTALL)
    text = re.sub(r'<li[^>]*>\s*', '• ', text)
    text = re.sub(r'</li>', '\n', text)
    text = re.sub(r'</?(?:ul|ol|div|span|table|tr|td|th|thead|tbody)[^>]*>', '', text)
    text = re.sub(r'```(\w*)\n?(.*?)```', lambda m: f'<pre>{escape_html(m.group(2))}</pre>', text, flags=re.DOTALL)
    text = re.sub(r'`([^`]+)`', lambda m: f'<code>{escape_html(m.group(1))}</code>', text)
    text = escape_code_blocks(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\w)\*(.+?)\*(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


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
    if "QUOTA_EXHAUSTED_ALL" in error_str or "QUOTA_EXHAUSTED_DAILY" in error_str:
        return (
            "⚠️ نفدت الحصة اليومية لجميع الموديلات.\n"
            "الحصة تتجدد تلقائياً كل يوم.\n"
            "يمكنك المحاولة لاحقاً أو ترقية خطتك في Google AI Studio."
        )
    if "503" in error_str or "UNAVAILABLE" in error_str:
        return "⚠️ سيرفر الذكاء الاصطناعي مشغول حالياً. حاول مرة ثانية بعد شوي."
    if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
        return "⚠️ تم تجاوز حد الطلبات. انتظر دقيقة ثم حاول مرة أخرى."
    if "API_KEY_INVALID" in error_str or "401" in error_str:
        return "⚠️ مفتاح API غير صالح. تحقق من المفتاح في الإعدادات."
    return "حدث خطأ أثناء معالجة رسالتك. حاول مرة أخرى."


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user)
    if not await check_subscription(update):
        return
    welcome = (
        'أهلاً بك! أنا مسماري.. رفيقك في عالم التقنية والخدمات الرقمية <tg-emoji emoji-id="5287684458881756303">🤖</tg-emoji>\n'
        "شلون أكدر أساعدك اليوم؟\n\n"
        '• إرسال نصوص للدردشة <tg-emoji emoji-id="5891243564309942507">💬</tg-emoji>\n'
        '• إرسال صور لتحليلها <tg-emoji emoji-id="5775949822993371030">📷</tg-emoji>\n'
        '• إرسال رسائل صوتية <tg-emoji emoji-id="5897554554894946515">🎤</tg-emoji>\n'
        '• إرسال ملفات للتحليل <tg-emoji emoji-id="5877332341331857066">📄</tg-emoji>\n\n'
        "أكتب /help لعرض جميع الأوامر."
    )
    await update.message.reply_text(welcome, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = get_settings(update.effective_chat.id)
    has_custom = "✅ فعّالة" if settings["system_prompt"] else "❌ غير مضافة"

    help_text = (
        "🤖 أنا مسماري، مساعدك الذكي\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🤖 ذكاء اصطناعي:\n"
        "• دردشة ذكية مع ذاكرة محادثة\n"
        "• تحليل الصور والملفات\n"
        "• فهم الرسائل الصوتية والرد عليها\n\n"
        "⚡ الأوامر المتاحة:\n"
        "/start - بدء المحادثة\n"
        "/system - ضبط تعليمات إضافية لمسماري\n"
        "/clear - مسح سجل المحادثة\n"
        "/stats - إحصائيات المحادثة\n"
        "/help - عرض هذه الرسالة\n\n"
        f"📋 التعليمات الإضافية: {has_custom}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 أرسل أي رسالة للبدء بالدردشة!"
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


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ هذا الأمر متاح فقط لمالك البوت.")
        return

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM users WHERE last_active >= datetime('now', '-1 day')")
    active_today = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM users WHERE last_active >= datetime('now', '-7 days')")
    active_week = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM messages")
    total_messages = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM messages WHERE role = 'user'")
    user_messages = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM messages WHERE role = 'assistant'")
    bot_messages = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM messages WHERE content_type = 'image'")
    image_count = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM messages WHERE content_type = 'voice'")
    voice_count = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM messages WHERE content_type = 'document'")
    doc_count = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM response_cache")
    cache_count = c.fetchone()[0]

    c.execute("SELECT COUNT(DISTINCT chat_id) FROM messages")
    active_chats = c.fetchone()[0]

    c.execute("""
        SELECT user_id, username, first_name, message_count, last_active
        FROM users ORDER BY message_count DESC LIMIT 10
    """)
    top_users = c.fetchall()

    c.execute("""
        SELECT user_id, username, first_name, first_seen
        FROM users ORDER BY first_seen DESC LIMIT 5
    """)
    new_users = c.fetchall()

    c.execute("SELECT MIN(first_seen) FROM users")
    oldest = c.fetchone()[0] or "غير متوفر"

    conn.close()

    channel_status = f"✅ {REQUIRED_CHANNEL}" if REQUIRED_CHANNEL else "❌ غير مفعّل"

    admin_text = (
        "🔐 <b>لوحة تحكم مسماري - إحصائيات كاملة</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👥 <b>المستخدمين:</b>\n"
        f"  • إجمالي المستخدمين: <b>{total_users}</b>\n"
        f"  • نشطين اليوم: <b>{active_today}</b>\n"
        f"  • نشطين هالأسبوع: <b>{active_week}</b>\n"
        f"  • محادثات فعّالة: <b>{active_chats}</b>\n\n"
        "💬 <b>الرسائل:</b>\n"
        f"  • إجمالي الرسائل: <b>{total_messages}</b>\n"
        f"  • رسائل المستخدمين: <b>{user_messages}</b>\n"
        f"  • ردود مسماري: <b>{bot_messages}</b>\n\n"
        "📎 <b>الوسائط:</b>\n"
        f"  • صور: <b>{image_count}</b>\n"
        f"  • صوتيات: <b>{voice_count}</b>\n"
        f"  • ملفات: <b>{doc_count}</b>\n\n"
        "⚙️ <b>النظام:</b>\n"
        f"  • ردود مخزنة (كاش): <b>{cache_count}</b>\n"
        f"  • اشتراك إجباري: {channel_status}\n"
        f"  • أول مستخدم: {oldest}\n\n"
    )

    if top_users:
        admin_text += "🏆 <b>أكثر المستخدمين نشاطاً:</b>\n"
        for i, (uid, uname, fname, count, last) in enumerate(top_users, 1):
            name = f"@{uname}" if uname else fname or str(uid)
            admin_text += f"  {i}. {name} — <b>{count}</b> رسالة\n"
        admin_text += "\n"

    if new_users:
        admin_text += "🆕 <b>أحدث المستخدمين:</b>\n"
        for uid, uname, fname, joined in new_users:
            name = f"@{uname}" if uname else fname or str(uid)
            admin_text += f"  • {name} — {joined}\n"

    await send_reply(update, admin_text)


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


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "check_sub":
        await query.answer()
        is_subscribed = await check_subscription(update, from_callback=True)
        if is_subscribed:
            welcome = (
                "✅ تم التحقق! أهلاً بك في مسماري 🤖\n\n"
                "أرسل رابط للتحميل أو أي رسالة للدردشة مع الذكاء الاصطناعي."
            )
            await query.edit_message_text(welcome)
        return

    await query.answer()


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user)
    if not await check_subscription(update):
        return

    user_text = update.message.text

    chat_id = update.effective_chat.id
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


MAX_FILE_SIZE_MB = 20
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user)
    if not await check_subscription(update):
        return
    chat_id = update.effective_chat.id
    settings = get_settings(chat_id)
    caption = update.message.caption or "حلل هذه الصورة بالتفصيل."

    await update.message.chat.send_action("typing")

    try:
        photo = update.message.photo[-1]
        if photo.file_size and photo.file_size > MAX_FILE_SIZE_BYTES:
            await update.message.reply_text(f"❌ حجم الصورة كبير جداً. الحد الأقصى {MAX_FILE_SIZE_MB}MB.")
            return
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
    track_user(update.effective_user)
    if not await check_subscription(update):
        return
    chat_id = update.effective_chat.id
    settings = get_settings(chat_id)

    await update.message.chat.send_action("typing")

    try:
        voice = update.message.voice or update.message.audio
        if voice.file_size and voice.file_size > MAX_FILE_SIZE_BYTES:
            await update.message.reply_text(f"❌ حجم الملف الصوتي كبير جداً. الحد الأقصى {MAX_FILE_SIZE_MB}MB.")
            return
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
    track_user(update.effective_user)
    if not await check_subscription(update):
        return
    chat_id = update.effective_chat.id
    settings = get_settings(chat_id)
    caption = update.message.caption or "حلل هذا الملف بالتفصيل."

    await update.message.chat.send_action("typing")

    try:
        doc = update.message.document
        if doc.file_size and doc.file_size > MAX_FILE_SIZE_BYTES:
            await update.message.reply_text(f"❌ حجم الملف كبير جداً. الحد الأقصى {MAX_FILE_SIZE_MB}MB.")
            return
        file = await doc.get_file()
        doc_bytes = await file.download_as_bytearray()

        mime_type = doc.mime_type or "application/octet-stream"
        file_name = doc.file_name or "unknown"

        GEMINI_SUPPORTED_MIMES = [
            "text/", "application/json", "application/xml",
            "application/javascript", "application/typescript",
            "application/x-python", "application/x-sh",
            "application/pdf",
            "image/png", "image/jpeg", "image/webp", "image/gif",
            "audio/mp3", "audio/wav", "audio/mpeg", "audio/ogg",
            "video/mp4", "video/webm", "video/mpeg",
        ]
        is_gemini_supported = any(mime_type.startswith(m) for m in GEMINI_SUPPORTED_MIMES)

        text_extensions = [
            ".py", ".js", ".ts", ".html", ".css", ".json", ".xml",
            ".yaml", ".yml", ".md", ".txt", ".csv", ".sh", ".sql",
            ".env", ".ini", ".cfg", ".toml", ".rs", ".go", ".java",
            ".c", ".cpp", ".h", ".hpp", ".rb", ".php", ".swift",
            ".kt", ".dart", ".lua", ".r", ".m", ".pl", ".log",
        ]
        is_text_ext = any(file_name.lower().endswith(ext) for ext in text_extensions)

        save_message(chat_id, "user", f"[ملف: {file_name}]: {caption}")

        history = get_history(chat_id, MAX_HISTORY - 1)
        contents = build_contents(history[:-1], settings["summary"])

        sent_as_text = False
        if is_text_ext or not is_gemini_supported:
            try:
                file_content = doc_bytes.decode("utf-8")
                prompt = (
                    f"اسم الملف: {file_name}\n"
                    f"نوع الملف: {mime_type}\n"
                    f"حجم الملف: {len(doc_bytes) // 1024} KB\n\n"
                    f"محتوى الملف:\n<pre>\n{file_content[:50000]}\n</pre>\n\n{caption}"
                )
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=prompt)]
                ))
                sent_as_text = True
            except UnicodeDecodeError:
                pass

        if not sent_as_text:
            if is_gemini_supported:
                file_part = types.Part.from_bytes(data=bytes(doc_bytes), mime_type=mime_type)
                text_part = types.Part.from_text(text=f"اسم الملف: {file_name}\n{caption}")
                contents.append(types.Content(role="user", parts=[file_part, text_part]))
            else:
                file_size = len(doc_bytes) // 1024
                prompt = (
                    f"المستخدم أرسل ملف اسمه: {file_name}\n"
                    f"نوع الملف: {mime_type}\n"
                    f"حجم الملف: {file_size} KB\n\n"
                    f"هذا الملف من نوع غير مدعوم للقراءة المباشرة. "
                    f"أخبر المستخدم بنوع الملف وأنك لا تستطيع قراءة محتواه مباشرة، "
                    f"لكن قدّم له معلومات مفيدة عن هذا النوع من الملفات إذا أمكن.\n\n{caption}"
                )
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=prompt)]
                ))

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
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(system_conv)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    logger.info(f"Bot starting... Owner ID: {OWNER_ID}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
