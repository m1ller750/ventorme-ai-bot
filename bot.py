"""
Ventorme — Telegram-бот ежедневных напоминаний о серии.

Бот работает с той же базой Supabase, что и сайт (index.html):
  * telegram_subscriptions — username, время, часовой пояс и chat_id пользователя;
  * profiles               — серия (current_streak), рекорд, XP и дата последней активности;
  * app_config             — бот записывает сюда свой @username, чтобы сайт показал кнопку
                             «Открыть бота» со ссылкой t.me/<bot>?start=<link_token>.

Как пользователь подключается:
  1. На сайте: Кабинет → Настройки → «Напоминания в Telegram» → вводит @username и время.
  2. Нажимает «Открыть бота» → Telegram отправляет боту /start <link_token> → бот сохраняет chat_id.
     (Если пользователь просто написал /start, бот попробует найти подписку по его username.)
  3. Каждую минуту бот проверяет, у кого наступило время напоминания, и шлёт сообщение раз в день.

Запуск:  python bot.py   (переменные окружения — в .env, см. .env.example)
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import re
from datetime import date, datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import Client, create_client
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
SITE_URL = os.getenv("SITE_URL", "").strip()
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
DEFAULT_TZ = "Asia/Almaty"

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("ventorme-bot")

# service_role ключ обходит RLS — храните его только на сервере, никогда не кладите в index.html
db: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

TIPS = [
    "Реши 5 вопросов мини-теста — это 5 минут и +XP.",
    "Отметь одну задачу в плане поступления выполненной.",
    "Допиши 50 слов в эссе — маленькие шаги складываются в сильный текст.",
    "Выучи 10 новых академических слов для IELTS.",
    "Проверь дедлайны стипендий на этот месяц.",
    "Добавь в свой список ещё один вуз и посмотри шансы.",
    "Спроси Venti, что подтянуть в первую очередь.",
]
SUB_COLUMNS = "user_id,username,chat_id,link_token,remind_time,timezone,enabled,last_sent_on"
PROFILE_COLUMNS = "id,full_name,current_streak,longest_streak,last_active_date,xp,timezone"


# ---------------------------------------------------------------- helpers
def plural_days(n: int) -> str:
    a, b = abs(n) % 100, abs(n) % 10
    if 10 < a < 20:
        return f"{n} дней"
    if b == 1:
        return f"{n} день"
    if 1 < b < 5:
        return f"{n} дня"
    return f"{n} дней"


def tz_of(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except Exception:  # неизвестный пояс — не роняем бота
        return ZoneInfo(DEFAULT_TZ)


def effective_streak(profile: dict) -> int:
    """Серия «жива», если последняя активность была сегодня или вчера (в поясе профиля)."""
    last = profile.get("last_active_date")
    if not last:
        return 0
    today = datetime.now(tz_of(profile.get("timezone"))).date()
    return int(profile.get("current_streak") or 0) if (today - date.fromisoformat(last)).days <= 1 else 0


def active_today(profile: dict) -> bool:
    last = profile.get("last_active_date")
    return bool(last) and date.fromisoformat(last) == datetime.now(tz_of(profile.get("timezone"))).date()


def first_name(profile: dict) -> str:
    name = (profile.get("full_name") or "").strip().split(" ")[0]
    return html.escape(name) if name else "Привет"


def open_site_markup() -> InlineKeyboardMarkup | None:
    # Telegram принимает только публичные http(s)-ссылки: localhost и file:// кнопкой не открыть
    if re.match(r"^https?://", SITE_URL) and "localhost" not in SITE_URL and "127.0.0.1" not in SITE_URL:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Открыть Ventorme", url=SITE_URL.rstrip("/") + "/#/app")]])
    return None


def reminder_text(profile: dict) -> str:
    name, streak, tip = first_name(profile), effective_streak(profile), random.choice(TIPS)
    if active_today(profile):
        return (f"🔥 {name}, сегодняшний день уже засчитан — серия <b>{plural_days(streak)}</b> сохранена!\n"
                f"Завтра продолжаем.\n\n💡 {tip}")
    if streak > 0:
        return (f"⚠️ {name}, серия <b>{plural_days(streak)}</b> под угрозой!\n"
                f"Зайди в Ventorme сегодня и реши мини-тест — это займёт 5 минут.\n\n💡 {tip}")
    return (f"👋 {name}, пора вернуться к подготовке!\n"
            f"Начни новую серию сегодня — один мини-тест, и день засчитан.\n\n💡 {tip}")


async def run(fn, *args, **kwargs):
    """supabase-py синхронный — выполняем запросы в отдельном потоке, чтобы не блокировать бота."""
    return await asyncio.to_thread(fn, *args, **kwargs)


# ---------------------------------------------------------------- database
def db_sub_by_token(token: str) -> dict | None:
    r = db.table("telegram_subscriptions").select(SUB_COLUMNS).eq("link_token", token).limit(1).execute()
    return r.data[0] if r.data else None


def db_unlinked_subs_by_username(username: str) -> list[dict]:
    r = (db.table("telegram_subscriptions").select(SUB_COLUMNS)
         .eq("username", username.lower()).is_("chat_id", "null").execute())
    return r.data or []


def db_sub_by_chat(chat_id: int) -> dict | None:
    r = db.table("telegram_subscriptions").select(SUB_COLUMNS).eq("chat_id", chat_id).limit(1).execute()
    return r.data[0] if r.data else None


def db_link(sub: dict, chat_id: int, tg_username: str | None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    # chat_id уникален: отвязываем этот чат от других аккаунтов, если он был привязан раньше
    (db.table("telegram_subscriptions").update({"chat_id": None, "linked_at": None})
     .eq("chat_id", chat_id).neq("user_id", sub["user_id"]).execute())
    # если в Telegram другой username — сохраняем настоящий (триггер в БД при смене username сбрасывает chat_id,
    # поэтому username обновляем отдельным запросом ДО записи chat_id)
    if tg_username and re.fullmatch(r"[A-Za-z0-9_]{4,32}", tg_username) and tg_username.lower() != sub["username"]:
        db.table("telegram_subscriptions").update({"username": tg_username.lower()}).eq("user_id", sub["user_id"]).execute()
    (db.table("telegram_subscriptions").update({"chat_id": chat_id, "linked_at": now, "enabled": True})
     .eq("user_id", sub["user_id"]).execute())


def db_profile(user_id: str) -> dict:
    r = db.table("profiles").select(PROFILE_COLUMNS).eq("id", user_id).limit(1).execute()
    return r.data[0] if r.data else {}


def db_profiles(user_ids: list[str]) -> dict[str, dict]:
    if not user_ids:
        return {}
    r = db.table("profiles").select(PROFILE_COLUMNS).in_("id", user_ids).execute()
    return {p["id"]: p for p in (r.data or [])}


def db_active_subs() -> list[dict]:
    r = (db.table("telegram_subscriptions").select(SUB_COLUMNS)
         .eq("enabled", True).not_.is_("chat_id", "null").execute())
    return r.data or []


def db_update_sub(user_id: str, patch: dict) -> None:
    db.table("telegram_subscriptions").update(patch).eq("user_id", user_id).execute()


def db_set_config(key: str, value: str) -> None:
    db.table("app_config").upsert({"key": key, "value": value, "updated_at": datetime.now(timezone.utc).isoformat()}).execute()


# ---------------------------------------------------------------- commands
HELP = ("Команды:\n"
        "/status — твоя серия и настройки\n"
        "/time 20:00 — изменить время напоминания\n"
        "/stop — выключить напоминания\n"
        "/resume — включить снова")


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user = update.effective_chat, update.effective_user
    token = ctx.args[0] if ctx.args else None
    sub = await run(db_sub_by_token, token) if token else None
    if not sub and user and user.username:
        candidates = await run(db_unlinked_subs_by_username, user.username)
        if len(candidates) == 1:
            sub = candidates[0]
    if not sub:
        if await run(db_sub_by_chat, chat.id):
            await update.message.reply_text("Ты уже подключён ✅\n\n" + HELP)
        else:
            await update.message.reply_text(
                "Привет! Я присылаю напоминания Ventorme о твоей серии 🔥\n\n"
                "Чтобы подключиться: открой сайт → Кабинет → Настройки → «Напоминания в Telegram», "
                "укажи свой username и нажми «Открыть бота».")
        return
    await run(db_link, sub, chat.id, user.username if user else None)
    profile = await run(db_profile, sub["user_id"])
    await update.message.reply_text(
        f"Готово, {first_name(profile)}! Напоминания подключены ✅\n"
        f"Буду писать каждый день в <b>{sub['remind_time'][:5]}</b> ({html.escape(sub['timezone'])}).\n"
        f"Твоя серия сейчас: <b>{plural_days(effective_streak(profile))}</b> 🔥\n\n{HELP}",
        parse_mode=ParseMode.HTML, reply_markup=open_site_markup())
    log.info("linked user=%s chat=%s", sub["user_id"], chat.id)


async def _require_sub(update: Update) -> dict | None:
    sub = await run(db_sub_by_chat, update.effective_chat.id)
    if not sub:
        await update.message.reply_text("Этот чат ещё не привязан. Подключись через сайт: Кабинет → Настройки → Telegram.")
    return sub


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    sub = await _require_sub(update)
    if not sub:
        return
    p = await run(db_profile, sub["user_id"])
    xp = int(p.get("xp") or 0)
    await update.message.reply_text(
        f"🔥 Серия: <b>{plural_days(effective_streak(p))}</b> (рекорд — {plural_days(int(p.get('longest_streak') or 0))})\n"
        f"⚡ Уровень {xp // 100 + 1}, всего {xp} XP\n"
        f"{'✅ Сегодня уже засчитано' if active_today(p) else '⏳ Сегодня ещё не засчитано'}\n\n"
        f"Напоминания: {'включены' if sub['enabled'] else 'выключены'}, в {sub['remind_time'][:5]} ({html.escape(sub['timezone'])})",
        parse_mode=ParseMode.HTML, reply_markup=open_site_markup())


async def cmd_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    sub = await _require_sub(update)
    if not sub:
        return
    m = re.fullmatch(r"([01]?\d|2[0-3])[:.]([0-5]\d)", (ctx.args[0] if ctx.args else "").strip())
    if not m:
        await update.message.reply_text("Укажи время в формате ЧЧ:ММ, например: /time 20:30")
        return
    value = f"{int(m.group(1)):02d}:{m.group(2)}"
    await run(db_update_sub, sub["user_id"], {"remind_time": value})
    await update.message.reply_text(f"Готово! Теперь напоминание в {value} ⏰")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    sub = await _require_sub(update)
    if sub:
        await run(db_update_sub, sub["user_id"], {"enabled": False})
        await update.message.reply_text("Напоминания выключены. Включить снова — /resume")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    sub = await _require_sub(update)
    if sub:
        await run(db_update_sub, sub["user_id"], {"enabled": True})
        await update.message.reply_text(f"Напоминания включены ✅ Жду тебя в {sub['remind_time'][:5]}")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP)


# ---------------------------------------------------------------- daily reminders
async def reminder_tick(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        subs = await run(db_active_subs)
    except Exception:
        log.exception("could not load subscriptions")
        return
    due = []
    for s in subs:
        now = datetime.now(tz_of(s["timezone"]))
        today = now.date()
        if s.get("last_sent_on") and date.fromisoformat(s["last_sent_on"]) >= today:
            continue  # сегодня уже писали
        hh, mm = (int(x) for x in s["remind_time"][:5].split(":"))
        if now.time() >= dtime(hh, mm):
            due.append((s, today))
    if not due:
        return
    profiles = await run(db_profiles, [s["user_id"] for s, _ in due])
    for s, today in due:
        try:
            await ctx.bot.send_message(chat_id=s["chat_id"], text=reminder_text(profiles.get(s["user_id"], {})),
                                       parse_mode=ParseMode.HTML, reply_markup=open_site_markup())
        except (Forbidden, BadRequest) as e:
            # пользователь заблокировал бота или чат удалён — отвязываем, на сайте статус станет «Не подключено»
            log.warning("unlink user=%s: %s", s["user_id"], e)
            await run(db_update_sub, s["user_id"], {"chat_id": None, "linked_at": None})
            continue
        except Exception:
            log.exception("send failed user=%s", s["user_id"])
            continue
        await run(db_update_sub, s["user_id"], {"last_sent_on": today.isoformat()})
        log.info("reminder sent user=%s", s["user_id"])


# ---------------------------------------------------------------- app
async def on_startup(app: Application) -> None:
    me = await app.bot.get_me()
    await run(db_set_config, "telegram_bot_username", me.username)
    await app.bot.set_my_commands([
        BotCommand("status", "Моя серия и настройки"),
        BotCommand("time", "Изменить время напоминания"),
        BotCommand("stop", "Выключить напоминания"),
        BotCommand("resume", "Включить напоминания"),
        BotCommand("help", "Помощь"),
    ])
    log.info("bot @%s started, reminders checked every %ss", me.username, CHECK_INTERVAL)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("handler error", exc_info=ctx.error)


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("streak", cmd_status))
    app.add_handler(CommandHandler("time", cmd_time))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_help))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(reminder_tick, interval=CHECK_INTERVAL, first=5)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
