# دليل تنصيب بوت مسماري 🤖

بوت تيليكرام بالذكاء الاصطناعي — تطوير @mohmmed

---

## المتطلبات

- Python 3.10 أو أحدث
- pip
- خادم Linux (Ubuntu/Debian مستحسن)

---

## الخطوة 1 — تنزيل الملفات

```bash
# نسخ المستودع
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO/telegram-bot

# أو نسخ الملف مباشرة إلى السيرفر
scp bot.py root@YOUR_SERVER_IP:/var/www/AiBot/bot.py
```

---

## الخطوة 2 — تنصيب المكتبات

```bash
pip install python-telegram-bot==22.7 google-genai pillow
```

---

## الخطوة 3 — إعداد المتغيرات (Environment Variables)

### الطريقة الأولى: ملف `.env` (للتطوير)

أنشئ ملف `.env` في نفس مجلد `bot.py`:

```env
TELEGRAM_BOT_TOKEN=ضع_توكن_البوت_هنا
OWNER_ID=ضع_ايدي_الادمن_هنا
GEMINI_API_KEY=ضع_مفتاح_جيميني_هنا
REQUIRED_CHANNEL=@اسم_القناة
```

### الطريقة الثانية: systemd service (للسيرفر)

```ini
# /etc/systemd/system/mismari-bot.service

[Unit]
Description=Mismari AI Telegram Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/var/www/AiBot
ExecStart=/usr/bin/python3 /var/www/AiBot/bot.py
Restart=always
RestartSec=5
Environment=TELEGRAM_BOT_TOKEN=ضع_توكن_البوت_هنا
Environment=OWNER_ID=ضع_ايدي_الادمن_هنا
Environment=GEMINI_API_KEY=ضع_مفتاح_جيميني_هنا
Environment=REQUIRED_CHANNEL=@اسم_القناة

[Install]
WantedBy=multi-user.target
```

---

## شرح كل متغير

| المتغير | الوصف | كيف تحصل عليه |
|---------|-------|---------------|
| `TELEGRAM_BOT_TOKEN` | توكن البوت من تيليكرام | راسل [@BotFather](https://t.me/BotFather) ← `/newbot` ← انسخ التوكن |
| `OWNER_ID` | الـ ID الخاص بك كأدمن | راسل [@userinfobot](https://t.me/userinfobot) ← سيرسل لك رقمك |
| `GEMINI_API_KEY` | مفتاح Gemini AI | افتح [aistudio.google.com](https://aistudio.google.com) ← Get API Key |
| `REQUIRED_CHANNEL` | قناة الاشتراك الإجباري | اسم قناتك مثل `@mychannel` (اختياري) |

---

## الخطوة 4 — تشغيل البوت

### تشغيل مباشر (للتجربة)

```bash
cd /var/www/AiBot
python3 bot.py
```

### تشغيل كـ service (للسيرفر الدائم)

```bash
# نسخ ملف الـ service
cp mismari-bot.service /etc/systemd/system/

# تفعيل وتشغيل
systemctl daemon-reload
systemctl enable mismari-bot
systemctl start mismari-bot

# التحقق من الحالة
systemctl status mismari-bot
```

---

## الأوامر المفيدة بعد التنصيب

```bash
# إعادة تشغيل البوت
systemctl restart mismari-bot

# عرض الـ logs المباشرة
journalctl -u mismari-bot -f

# آخر 50 سطر من اللوج
journalctl -u mismari-bot -n 50

# إيقاف البوت
systemctl stop mismari-bot
```

---

## أوامر البوت

| الأمر | الوصف |
|-------|-------|
| `/start` | رسالة الترحيب |
| `/help` | عرض المساعدة |
| `/system` | ضبط تعليمات إضافية للذكاء الاصطناعي |
| `/clear` | مسح سجل المحادثة |
| `/stats` | إحصائياتك الشخصية |
| `/admin` | لوحة الأدمن (الأدمن فقط) |

---

## ملاحظات

- الحد الأقصى لحجم الملفات المرسلة: **20MB**
- يدعم: نصوص، صور، رسائل صوتية، ملفات PDF ونصية
- قاعدة البيانات محفوظة في `conversations.db` بنفس المجلد
- لا تشارك ملف الـ service أو التوكن مع أحد

---

تطوير: [@mohmmed](https://t.me/mohmmed)
