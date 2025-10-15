import os
import asyncio
import random
import threading
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional

from flask import Flask  # маленький веб-сервер для Render Web Service

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    AIORateLimiter,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ----------------- ЛЁГКИЙ ВЕБ-СЕРВЕР (для Render Web Service) -----------------
app_web = Flask(__name__)

@app_web.route("/")
def home():
    return "💧 Water Reminder Bot is running."

def run_web():
    app_web.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

# ----------------- ХРАНИЛИЩЕ (Redis или in-memory) -----------------
USE_REDIS = bool(os.getenv("REDIS_URL"))
if USE_REDIS:
    import redis
    r = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
else:
    r = None
    _store: Dict[str, str] = {}

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

# ----------------- КОНСТАНТЫ / ВРЕМЯ (всегда МСК, UTC+3) -----------------
def now_msk() -> datetime:
    """Текущее московское время без внешних библиотек."""
    return datetime.utcnow() + timedelta(hours=3)

DAY_START = time(7, 30)     # 07:30
DAY_END = time(23, 59)      # до 00:00
DEFAULT_INTERVAL_MIN = 90   # каждые 90 минут
RETRY_MINUTES = 10          # повтор через 10 минут, если не нажала кнопку

# Ключи в хранилище
def k_enabled(chat_id): return f"user:{chat_id}:enabled"
def k_interval(chat_id): return f"user:{chat_id}:interval"
def k_times(chat_id): return f"user:{chat_id}:times"   # CSV "HH:MM,HH:MM"
def k_ack(chat_id, stamp): return f"ack:{chat_id}:{stamp}"  # подтверждение на конкретное напоминание

# ----------------- УТИЛИТЫ ВРЕМЕНИ -----------------
def parse_times_csv(csv_text: str) -> List[time]:
    items = [x.strip() for x in csv_text.split(",") if x.strip()]
    out = []
    for item in items:
        hh, mm = item.split(":")
        out.append(time(int(hh), int(mm)))
    return out

def build_default_times(interval_min: int) -> List[time]:
    times = []
    # используем произвольную дату, важны только часы/минуты
    cur = datetime.combine(datetime(2000, 1, 1).date(), DAY_START)
    end_dt = datetime.combine(datetime(2000, 1, 1).date(), DAY_END)
    while cur <= end_dt:
        times.append(cur.time())
        cur += timedelta(minutes=interval_min)
    return times

def user_times(chat_id: int) -> List[time]:
    csv = kv_get(k_times(chat_id))
    if csv:
        return parse_times_csv(csv)
    interval = kv_get(k_interval(chat_id))
    interval = int(interval) if interval else DEFAULT_INTERVAL_MIN
    return build_default_times(interval)

def is_enabled(chat_id: int) -> bool:
    return (kv_get(k_enabled(chat_id)) == "1")

# ----------------- ЛОГИКА НАПОМИНАНИЙ -----------------
async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Основное напоминание + планирование повтора через 10 минут, если нет ack."""
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

    # повтор через 10 минут, если не нажала
    context.application.job_queue.run_once(
        callback=retry_if_not_ack,
        when=RETRY_MINUTES * 60,
        chat_id=chat_id,
        name=f"retry:{chat_id}:{stamp}",
        kwargs={"stamp": stamp},
    )

async def retry_if_not_ack(context: ContextTypes.DEFAULT_TYPE):
    """Повторное мягкое напоминание, если кнопка не нажата."""
    chat_id = context.job.chat_id
    stamp = context.job.kwargs["stamp"]
    if kv_get(k_ack(chat_id, stamp)) == "1":
        return  # подтверждено — повтор не нужен

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

async def schedule_today(application: Application, chat_id: int):
    """Расписывает на сегодня все оставшиеся слоты и ставит перепланировку на полночь (МСК)."""
    if not is_enabled(chat_id):
        return

    times = user_times(chat_id)

    # Перестраховка: удалим старые daily-задачи этого чата
    for job in application.job_queue.get_jobs_by_name(f"daily:{chat_id}"):
        job.remove()

    now_local = now_msk()
    today = now_local.date()

    # Запланируем все оставшиеся на сегодня слоты
    for t in times:
        dt_local = datetime.combine(today, t)  # «московская» дата-время
        if dt_local >= now_local:
            stamp = dt_local.isoformat()  # уникальный идентификатор слота
            kv_del(k_ack(chat_id, stamp))
            delay_sec = (dt_local - now_local).total_seconds()
            application.job_queue.run_once(
                callback=send_reminder,
                when=max(0, delay_sec),
                chat_id=chat_id,
                name=f"remind:{chat_id}:{stamp}",
                kwargs={"stamp": stamp},
            )

    # Перепланировка на «московскую полночь» (+1 сек)
    midnight_next = datetime.combine(today, time(23, 59, 59)) + timedelta(seconds=1)
    delay_midnight = (midnight_next - now_local).total_seconds()
    application.job_queue.run_once(
        callback=midnight_reschedule,
        when=max(1, int(delay_midnight)),
        chat_id=chat_id,
        name=f"daily:{chat_id}",
    )

async def midnight_reschedule(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    await schedule_today(context.application, chat_id)

# ----------------- КОМАНДЫ -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    kv_set(k_enabled(chat_id), "1")
    if not kv_get(k_interval(chat_id)) and not kv_get(k_times(chat_id)):
        kv_set(k_interval(chat_id), str(DEFAULT_INTERVAL_MIN))

    await schedule_today(context.application, chat_id)
    await update.message.reply_text(
        "Готово! Напоминаю о водичке 💧 каждые 90 минут с 07:30 до 00:00 (по Москве).\n\n"
        "Команды:\n"
        "/stop — выключить\n"
        "/interval <минуты> — изменить интервал (например, 90)\n"
        "/times HH:MM,HH:MM,… — задать конкретные времена (по Москве)"
    )

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    kv_set(k_enabled(chat_id), "0")
    # Удалим все job этого чата
    for job in context.application.job_queue.jobs():
        if job.name and (f":{chat_id}" in job.name):
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
    kv_del(k_times(chat_id))  # если был список времен — убираем
    kv_set(k_enabled(chat_id), "1")
    await schedule_today(context.application, chat_id)
    await update.message.reply_text(f"Интервал изменён на {minutes} мин. Напоминания активны (по Москве).")

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
    await schedule_today(context.application, chat_id)
    await update.message.reply_text(f"Заданы точные времена: {csv} (по Москве). Напоминания активны.")

# ----------------- КНОПКИ -----------------
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
    else:
        await query.answer("Неизвестное действие", show_alert=False)

# ----------------- MAIN -----------------
async def main():
    bot_token = os.environ["BOT_TOKEN"]
    app: Application = (
        ApplicationBuilder()
        .token(bot_token)
        .rate_limiter(AIORateLimiter())
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("interval", set_interval))
    app.add_handler(CommandHandler("times", set_times))
    app.add_handler(CallbackQueryHandler(on_button))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    # Запускаем лёгкий веб-сервер в отдельном потоке,
    # чтобы Render Web Service видел открытый порт.
    threading.Thread(target=run_web, daemon=True).start()
    asyncio.run(main())
