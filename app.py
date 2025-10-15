import os
import sys
import asyncio
import random
import threading
import logging
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional

from flask import Flask
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    JobQueue,          # <— вручную создаём и запускаем
    filters,
)

# ------------------ ЛОГИ ------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("water-bot")

# ------------------ ГЛОБАЛЬНЫЕ ССЫЛКИ ------------------
APP: Optional[Application] = None   # установим в main()
JQ: Optional[JobQueue] = None       # установим в main()

# ------------------ KEEPALIVE WEB (для Render Web Service) ------------------
app_web = Flask(__name__)

@app_web.route("/")
def home():
    return "💧 Water Reminder Bot is running."

def run_web():
    try:
        port = int(os.environ.get("PORT", 5000))
        log.info(f"Starting Flask keepalive on 0.0.0.0:{port}")
        app_web.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    except Exception as e:
        log.exception(f"Flask server failed: {e}")

# ------------------ ХРАНИЛИЩЕ (Redis или in-memory) ------------------
USE_REDIS = bool(os.getenv("REDIS_URL"))
if USE_REDIS:
    import redis
    r = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    log.info("Redis enabled via REDIS_URL")
else:
    r = None
    _store: Dict[str, str] = {}
    log.info("Redis not configured; using in-memory storage")

def kv_get(key: str) -> Optional[str]:
    return r.get(key) if r else _store.get(key)

def kv_set(key: str, value: str):
    if r:
        r.set(key, value)
    else:
        _store[key] = value

def kv_del(key: str):
    if r:
        r.delete(key)
    else:
        _store.pop(key, None)

# ------------------ КОНСТАНТЫ ------------------
def now_msk() -> datetime:
    """Текущее московское время без внешних библиотек (UTC+3)."""
    return datetime.utcnow() + timedelta(hours=3)

DAY_START = time(7, 30)   # 07:30
DAY_END   = time(23, 59)  # до 00:00
DEFAULT_INTERVAL_MIN = 90
RETRY_MINUTES = 10

def k_enabled(cid): return f"user:{cid}:enabled"
def k_interval(cid): return f"user:{cid}:interval"
def k_times(cid):    return f"user:{cid}:times"   # CSV "HH:MM,HH:MM"
def k_ack(cid, stamp): return f"ack:{cid}:{stamp}"

# ------------------ УТИЛИТЫ ------------------
def parse_times_csv(csv_text: str) -> List[time]:
    items = [x.strip() for x in csv_text.split(",") if x.strip()]
    out = []
    for item in items:
        hh, mm = item.split(":")
        out.append(time(int(hh), int(mm)))
    return out

def build_default_times(interval_min: int) -> List[time]:
    times = []
    cur = datetime.combine(datetime(2000, 1, 1).date(), DAY_START)
    end_dt = datetime.combine(datetime(2000, 1, 1).date(), DAY_END)
    while cur <= end_dt:
        times.append(cur.time())
        cur += timedelta(minutes=interval_min)
    return times

def user_times(chat_id: int) -> List[time]:
    csv = kv_get(k_times(chat_id))
    if csv:
        try:
            return parse_times_csv(csv)
        except Exception:
            log.warning(f"Invalid /times format for {chat_id}, fallback to interval")
    interval = kv_get(k_interval(chat_id))
    interval = int(interval) if interval else DEFAULT_INTERVAL_MIN
    return build_default_times(interval)

def is_enabled(chat_id: int) -> bool:
    return kv_get(k_enabled(chat_id)) == "1"

# ------------------ НАПОМИНАНИЯ ------------------
async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = context.job.chat_id
        stamp = context.job.kwargs["stamp"]
        main_msgs = [
            "Солнышко ☀️, попей водички",
            "Настюша 💖, пора попить водички. Люблю)",
            "Бусинка, пора пить воду.",
            "Любимая, напоминаю выпей воды 💦",
        ]
        text = random.choice(main_msgs)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Я попила 💧", callback_data=f"ack:{stamp}")]])
        await context.bot.send_message(chat_id, text, reply_markup=kb)

        if JQ:
            JQ.run_once(
                retry_if_not_ack,
                when=RETRY_MINUTES * 60,
                chat_id=chat_id,
                name=f"retry:{chat_id}:{stamp}",
                kwargs={"stamp": stamp},
            )
    except Exception as e:
        log.exception(f"send_reminder failed: {e}")

async def retry_if_not_ack(context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = context.job.chat_id
        stamp = context.job.kwargs["stamp"]
        if kv_get(k_ack(chat_id, stamp)) == "1":
            return
        retry_msgs = [
            "Настюша, ты забыла про воду? 💧",
            "Милая ☀️, снова напоминаю — попей водички)",
            "Солнышко, не забывай пить)",
        ]
        text = random.choice(retry_msgs)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Я попила 💧", callback_data=f"ack:{stamp}")]])
        await context.bot.send_message(chat_id, text, reply_markup=kb)
    except Exception as e:
        log.exception(f"retry_if_not_ack failed: {e}")

async def schedule_today(chat_id: int):
    """Расписывает слоты на сегодня и ставит перепланировку на «полночь» МСК."""
    try:
        if not is_enabled(chat_id):
            return

        if JQ is None:
            log.error("Global JobQueue is None; skip schedule_today")
            return

        times = user_times(chat_id)

        # удалить старые daily-задачи
        for job in JQ.get_jobs_by_name(f"daily:{chat_id}"):
            job.remove()

        now_local = now_msk()
        today = now_local.date()
        count = 0

        # создать слоты на сегодня
        for t in times:
            dt_local = datetime.combine(today, t)
            if dt_local >= now_local:
                stamp = dt_local.isoformat()
                kv_del(k_ack(chat_id, stamp))
                JQ.run_once(
                    send_reminder,
                    when=max(0, (dt_local - now_local).total_seconds()),
                    chat_id=chat_id,
                    name=f"remind:{chat_id}:{stamp}",
                    kwargs={"stamp": stamp},
                )
                count += 1

        # перепланировка на «полночь»
        midnight_next = datetime.combine(today, time(23, 59, 59)) + timedelta(seconds=1)
        JQ.run_once(
            midnight_reschedule,
            when=max(1, int((midnight_next - now_local).total_seconds())),
            chat_id=chat_id,
            name=f"daily:{chat_id}",
        )

        log.info(f"Scheduled {count} reminders for chat {chat_id}")
    except Exception as e:
        log.exception(f"schedule_today failed for chat {chat_id}: {e}")

async def midnight_reschedule(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    log.info(f"Midnight reschedule for chat {chat_id}")
    await schedule_today(chat_id)

# ------------------ КОМАНДЫ ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    kv_set(k_enabled(chat_id), "1")
    if not kv_get(k_interval(chat_id)) and not kv_get(k_times(chat_id)):
        kv_set(k_interval(chat_id), str(DEFAULT_INTERVAL_MIN))
    await schedule_today(chat_id)
    await update.message.reply_text(
        "Привет бусинка. Создал тебе бота который напоминает пить воду 💧 каждые 1,5 часа с 07:30 до 00:00.\n\n"
            "Есть команды для удобства:\n"
            "/stop — выключить\n"
            "/interval <минуты> — изменить интервал (например, 90)\n"
            "/times HH:MM,HH:MM,… — задать конкретные времена (по Москве)"
    )

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    kv_set(k_enabled(chat_id), "0")
    if JQ:
        for job in JQ.jobs():
            if job.name and f":{chat_id}" in job.name:
                job.remove()
    await update.message.reply_text("Напоминания отключены. Я рядом, если что ❤️")

async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Укажите интервал в минутах: /interval 90")
        return
    try:
        minutes = int(context.args[0])
        if minutes < 10 or minutes > 360:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Интервал должен быть числом от 10 до 360.")
        return
    kv_set(k_interval(chat_id), str(minutes))
    kv_del(k_times(chat_id))
    kv_set(k_enabled(chat_id), "1")
    await schedule_today(chat_id)
    await update.message.reply_text(f"Интервал изменён на {minutes} мин. (по Москве)")

async def set_times(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Пример: /times 07:30,09:00,12:15,20:00")
        return
    csv = " ".join(context.args).replace(" ", "")
    try:
        parse_times_csv(csv)
    except Exception:
        await update.message.reply_text("Неверный формат. Пример: 07:30,09:00,12:15,20:00")
        return
    kv_set(k_times(chat_id), csv)
    kv_del(k_interval(chat_id))
    kv_set(k_enabled(chat_id), "1")
    await schedule_today(chat_id)
    await update.message.reply_text(f"Заданы точные времена: {csv} (по Москве)")

# ------------------ КНОПКИ ------------------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("ack:"):
        stamp = data.split(":", 1)[1]
        chat_id = query.message.chat_id
        kv_set(k_ack(chat_id, stamp), "1")
        responses = [
            "Вот умничка! 💖 Я горжусь тобой 🌷",
            "Так держать, солнышко ☀️💦",
            "Бусинка, продолжай в том же духе)",
            "Оес, умничка 💧💋",
        ]
        text = random.choice(responses)
        try:
            await query.edit_message_text(text)
        except Exception:
            await context.bot.send_message(chat_id, text)

# ------------------ ОБРАБОТКА ОШИБОК ------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled exception", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Ой, я на секунду споткнулся и уже в порядке 💙",
            )
    except Exception:
        pass

# ------------------ ЭХО ДЛЯ ДИАГНОСТИКИ ------------------
async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Я на связи 💧 Напиши /start")

# ------------------ MAIN ------------------
async def main():
    global APP, JQ
    token = os.getenv("BOT_TOKEN")
    if not token:
        log.error("BOT_TOKEN missing in environment.")
        sys.exit(1)

    try:
        # 1) создаём приложение
        app: Application = ApplicationBuilder().token(token).build()
        APP = app

        # 2) создаём и запускаем собственный JobQueue
        JQ = JobQueue()
        JQ.set_application(app)     # привязываем к приложению
        await JQ.start()            # явно запускаем JobQueue

        # 3) регистрируем хендлеры
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("stop", stop))
        app.add_handler(CommandHandler("interval", set_interval))
        app.add_handler(CommandHandler("times", set_times))
        app.add_handler(CallbackQueryHandler(on_button))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
        app.add_error_handler(on_error)

        # 4) стартуем бота (long polling)
        await app.initialize()
        await app.start()
        log.info("Telegram bot started (long polling)")
        await app.updater.start_polling(drop_pending_updates=True)

        try:
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.stop()
            if JQ:
                await JQ.stop()
            await app.shutdown()
    except Exception as e:
        log.exception(f"Bot crashed during startup: {e}")
        sys.exit(1)

if __name__ == "__main__":
    try:
        threading.Thread(target=run_web, daemon=True).start()
        asyncio.run(main())
    except Exception as e:
        log.exception(f"Fatal error: {e}")
        sys.exit(1)
