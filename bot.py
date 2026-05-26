"""
Zooma Casino Bot
- Авторизация Telethon через чат бота (номер → код → 2FA)
- Мониторинг Trains на zma11.casino
- Мониторинг промокодов ZM_ из Telegram каналов
"""

import asyncio
import logging
import re
import os
import json
from pathlib import Path

import aiohttp
from bs4 import BeautifulSoup
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters, ConversationHandler,
)
from telegram.constants import ParseMode

# ============================================================
#  НАСТРОЙКИ
# ============================================================

BOT_TOKEN      = "8760963074:AAFt6XNi7SAzX2iYJI6Iau3lf8wnBeAKfvU"       # @BotFather
TARGET_CHAT_ID = -5180193107          # ID чата куда слать уведомления
ADMIN_IDS      = [7360025537]            # Telegram ID кто может управлять ботом

# Telethon — API от любого аккаунта (my.telegram.org)
API_ID   = 37443553
API_HASH = "a9a89f77413936f88b395a27ff956102"

# Каналы для мониторинга промокодов
PROMO_CHANNELS = [
    "@aztrash2",
    "@zooma1",
    "@azartlimon",
    "@zooma_reserve",
    # добавляй свои
]

# Прокси SOCKS5 (Cloudflare WARP или любой другой)
# Если прокси не нужен — поставь USE_PROXY = False
USE_PROXY = False
PROXY_SOCKS5 = {
    "proxy_type": "socks5",
    "addr": "127.0.0.1",
    "port": 40000,
    # "username": "",
    # "password": "",
}
AIOHTTP_PROXY = "socks5://127.0.0.1:40000"  # для парсинга сайта

# Интервал проверки поездов (сек)
TRAINS_INTERVAL = 30

# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SESSION_FILE = "zooma_userbot.session"
STATE_FILE   = "bot_state.json"

# Состояния ConversationHandler
WAIT_PHONE, WAIT_CODE, WAIT_2FA = range(3)

# Глобальные объекты
telethon_client: TelegramClient | None = None
trains_task: asyncio.Task | None = None
promo_task: asyncio.Task | None = None

seen_trains: set[str] = set()
sent_promos: set[str] = set()

PROMO_RE = re.compile(r'\bZM_[A-Z0-9]{4,20}\b')


# ============================================================
#  СОСТОЯНИЕ (persist seen_trains/promos между рестартами)
# ============================================================

def load_state():
    global seen_trains, sent_promos
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            data = json.load(f)
            seen_trains = set(data.get("seen_trains", []))
            sent_promos = set(data.get("sent_promos", []))
        log.info(f"Состояние загружено: {len(seen_trains)} поездов, {len(sent_promos)} промокодов")


def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump({
            "seen_trains": list(seen_trains),
            "sent_promos": list(sent_promos),
        }, f)


# ============================================================
#  ПАРСИНГ ПОЕЗДОВ
# ============================================================

TRAINS_URL = "https://zma11.casino/giveaways"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


async def fetch_page(session: aiohttp.ClientSession, url: str) -> str | None:
    proxy = AIOHTTP_PROXY if USE_PROXY else None
    try:
        async with session.get(
            url, headers=HEADERS, proxy=proxy,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            return await resp.text()
    except Exception as e:
        log.warning(f"Ошибка загрузки {url}: {e}")
        return None


async def fetch_train_details(session: aiohttp.ClientSession, train_id: str) -> dict | None:
    html = await fetch_page(session, f"https://zma11.casino/giveaways/{train_id}")
    if not html:
        return None

    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)

    info = {
        "id": train_id,
        "url": f"https://zma11.casino/giveaways/{train_id}",
        "for_all": False,
        "ref_only": False,
        "streamer": "—",
        "prize": "—",
        "seats": "—",
        "number": train_id,
    }

    if "Открыт для всех" in text:
        info["for_all"] = True
    if "Только рефералам" in text:
        info["ref_only"] = True

    m = re.search(r'Бортпроводник[:\s]+([A-Za-z0-9_]+)', text)
    if m:
        info["streamer"] = m.group(1)

    m = re.search(r'Розыгрыш\s*#(\d+)', text)
    if m:
        info["number"] = "#" + m.group(1)

    m = re.search(r'([\d\s]{3,})\s*[₽P]\s*ПРИЗОВОЙ', text)
    if m:
        info["prize"] = m.group(1).strip().replace(" ", "") + " ₽"

    m = re.search(r'(\d+)\s*МЕСТ', text)
    if m:
        info["seats"] = m.group(1)

    return info


async def trains_monitor(bot: Bot):
    log.info("🚂 Мониторинг поездов запущен")
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                html = await fetch_page(session, TRAINS_URL)
                if html:
                    soup = BeautifulSoup(html, "lxml")
                    new_count = 0

                    for a in soup.find_all("a", href=True):
                        href = a["href"]
                        if "/giveaways/" not in href:
                            continue
                        text = a.get_text(" ", strip=True)
                        if "DEPARTED" in text or "ARCHIVED" in text:
                            continue

                        train_id = href.split("/giveaways/")[-1].strip("/")
                        if not train_id or len(train_id) < 4:
                            continue
                        if train_id in seen_trains:
                            continue

                        info = await fetch_train_details(session, train_id)
                        if not info:
                            continue

                        seen_trains.add(train_id)
                        save_state()
                        new_count += 1

                        # Формируем сообщение
                        if info["ref_only"]:
                            access_line = "🔒 <b>Только для рефералов</b>"
                        else:
                            access_line = "🟢 <b>Открыт для всех</b>"

                        msg = (
                            f"🚂 <b>НОВЫЙ ПОЕЗД НА ZOOMA!</b>\n\n"
                            f"🎪 Розыгрыш <b>{info['number']}</b> · <b>{info['streamer']}</b>\n"
                            f"{access_line}\n"
                            f"💰 Призовой фонд: <b>{info['prize']}</b>\n"
                            f"💺 Мест: <b>{info['seats']}</b>\n\n"
                            f"🎫 <a href=\"{info['url']}\">Сесть в поезд →</a>"
                        )

                        await bot.send_message(
                            chat_id=TARGET_CHAT_ID,
                            text=msg,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                        )
                        log.info(f"✅ Поезд {info['number']} от {info['streamer']} отправлен")

            except Exception as e:
                log.error(f"Ошибка мониторинга поездов: {e}")

            await asyncio.sleep(TRAINS_INTERVAL)


# ============================================================
#  МОНИТОРИНГ ПРОМОКОДОВ
# ============================================================

def is_clean_promo(code: str) -> bool:
    """Только ZM_ + заглавные буквы/цифры, без мусора."""
    return bool(re.fullmatch(r'ZM_[A-Z0-9]{4,20}', code))


def extract_promos(text: str) -> list[str]:
    found = PROMO_RE.findall(text.upper())
    return [c for c in found if is_clean_promo(c)]


async def start_promo_listener(bot: Bot):
    global telethon_client
    if not telethon_client:
        return

    log.info(f"👂 Слушаю каналы: {', '.join(PROMO_CHANNELS)}")

    @telethon_client.on(events.NewMessage(chats=PROMO_CHANNELS))
    async def handle_msg(event):
        text = event.message.message or ""
        promos = extract_promos(text)
        if not promos:
            return

        for promo in promos:
            if promo in sent_promos:
                continue
            sent_promos.add(promo)
            save_state()

            try:
                chat = await event.get_chat()
                source = f"@{chat.username}" if getattr(chat, 'username', None) else getattr(chat, 'title', 'канал')
            except Exception:
                source = "канал"

            msg = (
                f"🎟️ <b>ПРОМОКОД ZOOMA!</b>\n\n"
                f"<code>{promo}</code>\n\n"
                f"📢 Из: {source}\n"
                f"🔗 <a href=\"https://zma11.casino/affilates\">Активировать на сайте →</a>"
            )
            await bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text=msg,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            log.info(f"✅ Промокод {promo} из {source}")

    await telethon_client.run_until_disconnected()


# ============================================================
#  АВТОРИЗАЦИЯ TELETHON ЧЕРЕЗ ЧАТ
# ============================================================

# Временное хранилище для процесса авторизации
auth_data: dict = {}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    
    status = "✅ авторизован" if (telethon_client and telethon_client.is_connected()) else "❌ не авторизован"
    trains_status = "✅ работает" if (trains_task and not trains_task.done()) else "⏸ остановлен"

    await update.message.reply_text(
        f"🤖 <b>Zooma Casino Bot</b>\n\n"
        f"👤 Аккаунт: {status}\n"
        f"🚂 Поезда: {trains_status}\n\n"
        f"<b>Команды:</b>\n"
        f"/login — авторизовать Telegram аккаунт\n"
        f"/logout — выйти из аккаунта\n"
        f"/status — текущий статус\n"
        f"/stop — остановить мониторинг\n"
        f"/resume — возобновить мониторинг",
        parse_mode=ParseMode.HTML,
    )


async def cmd_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    global telethon_client
    if telethon_client and await telethon_client.is_user_authorized():
        await update.message.reply_text("✅ Аккаунт уже авторизован. Используй /logout для смены.")
        return

    await update.message.reply_text(
        "📱 Введи номер телефона в международном формате:\n"
        "Пример: <code>+79991234567</code>",
        parse_mode=ParseMode.HTML,
    )
    return WAIT_PHONE


async def got_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    phone = update.message.text.strip()
    if not re.match(r'^\+\d{10,15}$', phone):
        await update.message.reply_text("❌ Неверный формат. Пример: <code>+79991234567</code>", parse_mode=ParseMode.HTML)
        return WAIT_PHONE

    proxy = PROXY_SOCKS5 if USE_PROXY else None
    client = TelegramClient(SESSION_FILE, API_ID, API_HASH, proxy=proxy)
    await client.connect()

    try:
        result = await client.send_code_request(phone)
        auth_data["client"] = client
        auth_data["phone"] = phone
        auth_data["phone_code_hash"] = result.phone_code_hash

        await update.message.reply_text(
            f"📨 Код отправлен на <code>{phone}</code>\n\n"
            f"Введи код из Telegram (формат: <code>12345</code>):",
            parse_mode=ParseMode.HTML,
        )
        return WAIT_CODE

    except Exception as e:
        await client.disconnect()
        await update.message.reply_text(f"❌ Ошибка: {e}")
        return ConversationHandler.END


async def got_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    code = update.message.text.strip().replace(" ", "").replace("-", "")
    client: TelegramClient = auth_data.get("client")
    phone = auth_data.get("phone")
    phone_code_hash = auth_data.get("phone_code_hash")

    if not client:
        await update.message.reply_text("❌ Сессия авторизации истекла. Начни заново: /login")
        return ConversationHandler.END

    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        await _finish_auth(update, ctx, client)
        return ConversationHandler.END

    except PhoneCodeInvalidError:
        await update.message.reply_text("❌ Неверный код. Попробуй ещё раз:")
        return WAIT_CODE

    except SessionPasswordNeededError:
        await update.message.reply_text(
            "🔐 Включена двухфакторная аутентификация.\n"
            "Введи пароль облачного 2FA:"
        )
        return WAIT_2FA

    except Exception as e:
        await client.disconnect()
        await update.message.reply_text(f"❌ Ошибка: {e}")
        return ConversationHandler.END


async def got_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    password = update.message.text.strip()
    client: TelegramClient = auth_data.get("client")

    try:
        await client.sign_in(password=password)
        await _finish_auth(update, ctx, client)
        return ConversationHandler.END

    except Exception as e:
        await client.disconnect()
        await update.message.reply_text(f"❌ Неверный пароль: {e}")
        return ConversationHandler.END


async def _finish_auth(update: Update, ctx: ContextTypes.DEFAULT_TYPE, client: TelegramClient):
    """Завершает авторизацию и запускает мониторинг."""
    global telethon_client, trains_task, promo_task

    telethon_client = client
    me = await client.get_me()
    auth_data.clear()

    await update.message.reply_text(
        f"✅ <b>Авторизован!</b>\n\n"
        f"👤 Аккаунт: <b>{me.first_name}</b> (@{me.username})\n\n"
        f"🚀 Запускаю мониторинг поездов и промокодов...",
        parse_mode=ParseMode.HTML,
    )

    bot = ctx.bot

    # Останавливаем старые задачи если были
    if trains_task and not trains_task.done():
        trains_task.cancel()
    if promo_task and not promo_task.done():
        promo_task.cancel()

    # Запускаем мониторинг
    trains_task = asyncio.create_task(trains_monitor(bot))
    promo_task  = asyncio.create_task(start_promo_listener(bot))

    await update.message.reply_text("✅ Мониторинг запущен! Уведомления идут в чат.")


async def cmd_logout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    global telethon_client, trains_task, promo_task

    if trains_task and not trains_task.done():
        trains_task.cancel()
    if promo_task and not promo_task.done():
        promo_task.cancel()

    if telethon_client:
        await telethon_client.log_out()
        telethon_client = None

    if Path(SESSION_FILE).exists():
        Path(SESSION_FILE).unlink()

    await update.message.reply_text("🚪 Аккаунт отключён. Для повторной авторизации: /login")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if telethon_client and await telethon_client.is_user_authorized():
        me = await telethon_client.get_me()
        acc = f"✅ {me.first_name} (@{me.username})"
    else:
        acc = "❌ не авторизован"

    t_status = "✅ работает" if (trains_task and not trains_task.done()) else "⏸ остановлен"
    p_status = "✅ работает" if (promo_task  and not promo_task.done())  else "⏸ остановлен"

    await update.message.reply_text(
        f"📊 <b>Статус бота</b>\n\n"
        f"👤 Аккаунт: {acc}\n"
        f"🚂 Поезда: {t_status}\n"
        f"🎟️ Промокоды: {p_status}\n\n"
        f"📌 Каналы: {', '.join(PROMO_CHANNELS)}\n"
        f"⏱ Интервал проверки: {TRAINS_INTERVAL} сек\n"
        f"🚂 Увиденных поездов: {len(seen_trains)}\n"
        f"🎟️ Отправленных промо: {len(sent_promos)}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    global trains_task, promo_task
    if trains_task and not trains_task.done():
        trains_task.cancel()
    if promo_task and not promo_task.done():
        promo_task.cancel()
    await update.message.reply_text("⏸ Мониторинг остановлен. /resume — возобновить")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    global trains_task, promo_task

    if not telethon_client or not await telethon_client.is_user_authorized():
        await update.message.reply_text("❌ Сначала авторизуйся: /login")
        return

    bot = ctx.bot
    trains_task = asyncio.create_task(trains_monitor(bot))
    promo_task  = asyncio.create_task(start_promo_listener(bot))
    await update.message.reply_text("▶️ Мониторинг возобновлён!")


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Авторизация отменена.")
    return ConversationHandler.END


# ============================================================
#  ЗАПУСК
# ============================================================

async def on_startup(app: Application):
    """При старте — если сессия уже есть, сразу запускаем мониторинг."""
    global telethon_client, trains_task, promo_task

    load_state()

    if Path(SESSION_FILE).exists():
        log.info("Найдена сохранённая сессия, подключаюсь...")
        proxy = PROXY_SOCKS5 if USE_PROXY else None
        client = TelegramClient(SESSION_FILE, API_ID, API_HASH, proxy=proxy)
        await client.connect()

        if await client.is_user_authorized():
            me = await client.get_me()
            telethon_client = client
            log.info(f"✅ Авторизован как {me.first_name} (@{me.username})")

            bot = app.bot
            trains_task = asyncio.create_task(trains_monitor(bot))
            promo_task  = asyncio.create_task(start_promo_listener(bot))
            log.info("🚀 Мониторинг запущен автоматически")
        else:
            await client.disconnect()
            log.info("Сессия устарела, нужна повторная авторизация (/login)")


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    # ConversationHandler для авторизации
    auth_conv = ConversationHandler(
        entry_points=[CommandHandler("login", cmd_login)],
        states={
            WAIT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_phone)],
            WAIT_CODE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_code)],
            WAIT_2FA:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_2fa)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CommandHandler("stop",   cmd_stop))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(auth_conv)

    log.info("🤖 Бот запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
