import os
import sys
import asyncio
import random
import threading
import logging
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional

from flask import Flask  # мини-веб для Render Web Service

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ------------------ ЛОГИ ------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("water-bot")

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
        # не роняем весь процесс — бот продолжит работать

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
    if r:
        return r.get(key)
    return _store.get(key)

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

# ------------------ КОНСТАНТЫ / ВРЕМЯ (всегда МСК, UTC+3) ------------------
def now_msk() -> datetime:
    """Текущее московское время без внешних библиотек (UTC+3)."""
    return datetime.utcnow() + timedelta(hours=3)

DAY_START = time(7, 30)     # 07:30
DAY_END   = time(23, 59)    # до 00:00
DEFAULT_INTERVAL_MIN = 90   # каждые 90 минут
RETRY_MINUTES        = 10   # повтор через 10 минут, если не нажата кнопка

# ключи
def k_enabled(chat_id): return f"user:{chat_id}:enabled"
def k_interval(chat_id): return f"user:{chat_id}:interval"
def k_times(chat_id):    return f"user:{chat_id}:times"   # CSV "HH:MM,HH:MM"
def k_ack(chat_id, stamp): return f"ack:{chat_id}:{stamp}"

# ------------------ УТИЛИТЫ ВРЕМЕНИ ------------------
def parse_times_csv(csv_text: str) -> List[time]:
    items = [x.strip() for x in csv_text.split(",") if x.strip()]
    out = []
    for item in items:
        hh, mm = item.split(":")
        out.append(time(int(hh), int(mm)))
    return out

def build_default_times(interval_min: int) -> List[time]:
    times = []
    base_date = datetime(2000, 1, 1).date()
    cur = datetime.combine(base_date, DAY_START)
    end_dt = datetime.combine(base_date, DAY_END)
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
            log.warning(f"Invalid /times format for chat {chat_id}: {csv}. Fallback to interval.")
    interval = kv_get(k_interval(chat_id))
    interval = int(interval) if interval else DEFAULT_INTERVAL_MIN
    return build_default_times(interval)

def is_enabled(chat_id: int) -> bool:
    return (kv_get(k_enabled(chat_id)) == "1")

# ------------------ ЛОГИКА НАПОМИНАНИЙ ------------------
async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Основное напоминание + планирование повтора через 10 минут, если нет ack."""
    try:
        chat_id = context.job.chat_id
        stamp = context.job.kwargs["stamp"]
        main_messages = [
            "Солнышко ☀️, попей водички",
            "Настюша 💖, пора попить водички. Люблю)",
            "Бусинка, пора пить воду.",
            "Любимая, напоминаю выпей воды 💦",
        ]
        text = random.choice(main_messages)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Я попила 💧", callback_data=f"ack:{stamp}")
        ]])
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        log.info(f"Sent reminder to {chat_id} at {stamp}")

        # повтор через 10 минут, если не нажата кнопка
        context.application.job_queue.run_once(
            callback=retry_if_not_ack,
            when=RETRY_MINUTES * 60,
            chat_id=chat_id,
            name=f"retry:{chat_id}:{stamp}",
            kwargs={"stamp": stamp},
        )
    except Exception as e:
        log.exception(f"send_reminder failed: {e}")

async def retry_if_not_ack(context: ContextTypes.DEFAULT_TYPE):
    """Повторное мягкое напоминание, если кнопка не нажата."""
    try:
        chat_id = context.job.chat_id
        stamp = context.job.kwargs["stamp"]
        if kv_get(k_ack(chat_id, stamp)) == "1":
            log.info(f"Ack found for {chat_id} {stamp}; skip retry")
            return

        retry_messages = [
            "Настюша, ты забыла про воду? 💧",
            "Милая ☀️, снова напоминаю — попей водички)",
            "Солнышко, не забывай пить)",
        ]
        text = random.choice(retry_messages)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Я попила 💧", callback_data=f"ack:{stamp}")
        ]])
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        log.info(f"Sent retry to {chat_id} for {stamp}")
    except Exception as e:
        log.exception(f"retry_if_not_ack failed: {e}")

async def schedule_today(application: Application, chat_id: int):
    """Расписывает на сегодня все оставшиеся слоты и ставит перепланировку на полночь (МСК)."""
    try:
        if not is_enabled(chat_id):
            log.info(f"schedule_today: disabled for chat {chat_id}")
            return

        times = user_times(chat_id)

        # удалим старые daily-задачи этого чата
        for job in application.job_queue.get_jobs_by_name(f"daily:{chat_id}"):
            job.remove()

        now_local = now_msk()
        today = now_local.date()
        count = 0

        # Запланируем все оставшиеся на сегодня слоты
        for t in times:
            dt_local = datetime.combine(today, t)  # московское локальное время
            if dt_local >= now_local:
                stamp = dt_local.isoformat()
                kv_del(k_ack(chat_id, stamp))
                delay_sec = (dt_local - now_local).total_seconds()
                application.job_queue.run_once(
                    callback=send_reminder,
                    when=max(0, delay_sec),
                    chat_id=chat_id,
                    name=f"remind:{chat_id}:{stamp}",
                    kwargs={"stamp": stamp},
                )
                count += 1

        log.info(f"Scheduled {count} reminders for chat {chat_id} today")

        # Перепланировка на «полночь» (+1 сек)
        midnight_next = datetime.combine(today, time(23, 59, 59)) + timedelta(seconds=1)
        delay_midnight = (midnight_next - now_local).total_seconds()
        application.job_queue.run_once(
            callback=midnight_reschedule,
            when=max(1, int(delay_midnight)),
            chat_id=chat_id,
            name=f"daily:{chat_id}",
        )
    except Exception as e:
        log.exception(f"schedule_today failed for chat {chat_id}: {e}")

async def midnight_reschedule(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    log.info(f"Midnight reschedule for chat {chat_id}")
    await schedule_today(context.application, chat_id)

# ------------------ КОМАНДЫ ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.effective_chat.id
        kv_set(k_enabled(chat_id), "1")
        if not kv_get(k_interval(chat_id)) and not kv_get(k_times(chat_id)):
            kv_set(k_interval(chat_id), str(DEFAULT_INTERVAL_MIN))

        await schedule_today(context.application, chat_id)
        await update.message.reply_text(
            "Привет бусинка. Создал тебе бота который напоминает пить воду 💧 каждые 1,5 часа с 07:30 до 00:00.\n\n"
            "Есть команды для удобства:\n"
            "/stop — выключить\n"
            "/interval <минуты> — изменить интервал (например, 90)\n"
            "/times HH:MM,HH:MM,… — задать конкретные времена (по Москве)"
        )
        log.info(f"/start from chat {chat_id}")
    except Exception as e:
        log.exception(f"/start failed: {e}")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.effective_chat.id
        kv_set(k_enabled(chat_id), "0")
        # Удалим все job этого чата
        for job in context.application.job_queue.jobs():
            if job.name and (f":{chat_id}" in job.name):
                job.remove()
        await update.message.reply_text("Напоминания отключены. Я рядом, если что ❤️")
        log.info(f"/stop from chat {chat_id}")
    except Exception as e:
        log.exception(f"/stop failed: {e}")

async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
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
        await schedule_today(context.application, chat_id)
        await update.message.reply_text(f"Интервал изменён на {minutes} мин. Напоминания активны (по Москве).")
        log.info(f"/interval {minutes} for chat {chat_id}")
    except Exception as e:
        log.exception(f"/interval failed: {e}")

async def set_times(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
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
        await schedule_today(context.application, chat_id)
        await update.message.reply_text(f"Заданы точные времена: {csv} (по Москве). Напоминания активны.")
        log.info(f"/times {csv} for chat {chat_id}")
    except Exception as e:
        log.exception(f"/times failed: {e}")

# ------------------ КНОПКИ ------------------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
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
            log.info(f"ACK from chat {chat_id} for {stamp}")
        else:
            await query.answer("Неизвестное действие", show_alert=False)
    except Exception as e:
        log.exception(f"on_button failed: {e}")

# ------------------ ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК ------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    # Ловит любые непойманные исключения из хендлеров
    try:
        log.exception("Unhandled exception in handler", exc_info=context.error)
        if isinstance(update, Update) and update.effective_chat:
            # Мягко сообщим пользователю, но не спамим
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="Ой, я на секунду споткнулся и уже в порядке 💙"
                )
            except Exception:
                pass
    except Exception:
        pass

# ------------------ MAIN ------------------
async def main():
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        log.error("ENV BOT_TOKEN is missing. Set it in Render → Environment.")
        sys.exit(1)

    try:
        app: Application = (
            ApplicationBuilder()
            .token(bot_token)
            .build()
        )

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("stop", stop))
        app.add_handler(CommandHandler("interval", set_interval))
        app.add_handler(CommandHandler("times", set_times))
        app.add_handler(CallbackQueryHandler(on_button))
        app.add_error_handler(on_error)  # <— регистрируем глобальный обработчик ошибок

        await app.initialize()
        await app.start()
        log.info("Telegram bot started (long polling)")
        await app.updater.start_polling(drop_pending_updates=True)

        try:
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.stop()
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
