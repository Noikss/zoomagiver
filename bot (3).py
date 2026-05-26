import asyncio
import logging
import re
import os
from datetime import datetime

import aiohttp
from bs4 import BeautifulSoup
from telethon import TelegramClient, events
from telethon.tl.types import MessageEntityBold, MessageEntityCode
from telegram import Bot
from telegram.constants import ParseMode

# ============================================================
#  НАСТРОЙКИ — заполни перед запуском
# ============================================================

# --- Telegram Bot (для отправки уведомлений в чат) ---
BOT_TOKEN = "ВАШ_BOT_TOKEN"          # от @BotFather
TARGET_CHAT_ID = -100123456789        # ID чата/группы куда слать (число со знаком минус)

# --- Telethon (userbot для чтения каналов) ---
API_ID   = 12345678                   # my.telegram.org → App api_id
API_HASH = "ваш_api_hash"            # my.telegram.org → App api_hash
SESSION  = "zooma_userbot"           # имя файла сессии

# --- Каналы для мониторинга промокодов ---
PROMO_CHANNELS = [
    "@zooma_reserve",       # официальный резервный канал Zooma
    "@djabyslots",          # пример стримера
    "@l1way_casino",        # пример стримера
    # добавляй свои каналы сюда
]

# --- Прокси (Cloudflare WARP SOCKS5 или любой другой) ---
# Если WARP установлен локально — он слушает 127.0.0.1:40000
# Либо вставь внешний SOCKS5: socks5://user:pass@host:port
PROXY = {
    "proxy_type": "socks5",
    "addr": "127.0.0.1",
    "port": 40000,
    # "username": "",  # раскомментируй если прокси с паролем
    # "password": "",
}
# Для aiohttp (парсинг сайта)
AIOHTTP_PROXY = "socks5://127.0.0.1:40000"

# --- Интервал проверки поездов (секунды) ---
TRAINS_INTERVAL = 30

# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Regex: ZM_ + буквы/цифры, минимум 4 символа после ZM_
# Не должно быть: *, _, пробелов, точек внутри кода
PROMO_RE = re.compile(r'\bZM_[A-Z0-9]{4,20}\b')

# Хранилище уже виденных поездов и промокодов
seen_trains: set[str] = set()
sent_promos: set[str] = set()


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

async def fetch_active_trains(session: aiohttp.ClientSession) -> list[dict]:
    """Парсит /giveaways и возвращает активные поезда."""
    try:
        async with session.get(
            TRAINS_URL,
            headers=HEADERS,
            proxy=AIOHTTP_PROXY,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            html = await resp.text()
    except Exception as e:
        log.warning(f"Ошибка при загрузке trains: {e}")
        return []

    soup = BeautifulSoup(html, "html.parser")
    trains = []

    # Ищем ссылки на поезда в разделе DEPARTURES (активные, не архив)
    # Активные поезда — ссылки НЕ содержащие DEPARTED/ARCHIVED
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/giveaways/" not in href:
            continue
        text = a.get_text(" ", strip=True)
        # Пропускаем архивные
        if "DEPARTED" in text or "ARCHIVED" in text:
            continue

        train_id = href.split("/giveaways/")[-1]
        if not train_id or len(train_id) < 4:
            continue

        trains.append({
            "id": train_id,
            "url": f"https://zma11.casino/giveaways/{train_id}",
            "raw": text,
        })

    return trains


async def fetch_train_details(session: aiohttp.ClientSession, train_id: str) -> dict | None:
    """Загружает страницу поезда и парсит детали."""
    url = f"https://zma11.casino/giveaways/{train_id}"
    try:
        async with session.get(
            url,
            headers=HEADERS,
            proxy=AIOHTTP_PROXY,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            html = await resp.text()
    except Exception as e:
        log.warning(f"Ошибка при загрузке поезда {train_id}: {e}")
        return None

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    info = {
        "id": train_id,
        "url": url,
        "for_all": False,
        "ref_only": False,
        "streamer": "",
        "prize": "",
        "seats": "",
        "number": "",
    }

    # Определяем тип доступа
    if "Открыт для всех" in text or "for all" in text.lower():
        info["for_all"] = True
    if "Только рефералам" in text or "ref" in text.lower():
        info["ref_only"] = True

    # Стример (Бортпроводник)
    m = re.search(r'Бортпроводник[:\s]+([A-Za-z0-9_]+)', text)
    if m:
        info["streamer"] = m.group(1)

    # Призовой фонд
    m = re.search(r'([\d\s]+)\s*₽\s*ПРИЗОВОЙ', text)
    if m:
        info["prize"] = m.group(1).replace(" ", "") + " ₽"

    # Номер розыгрыша
    m = re.search(r'Розыгрыш\s*#(\d+)', text)
    if m:
        info["number"] = "#" + m.group(1)

    # Мест в поезде
    m = re.search(r'(\d+)\s*МЕСТ', text)
    if m:
        info["seats"] = m.group(1)

    return info


def format_train_message(info: dict) -> str:
    """Формирует красивое сообщение о поезде."""
    if info["ref_only"]:
        access = "🔒 <b>Только для рефералов</b>"
    else:
        access = "🟢 <b>Открыт для всех</b>"

    lines = [
        "🚂 <b>НОВЫЙ ПОЕЗД НА ZOOMA!</b>",
        "",
        f"🎪 Розыгрыш <b>{info['number']}</b> от <b>{info['streamer']}</b>",
        access,
        f"💰 Призовой фонд: <b>{info['prize']}</b>",
        f"💺 Мест: <b>{info['seats']}</b>",
        "",
        f"🎫 <a href=\"{info['url']}\">Сесть в поезд</a>",
    ]
    return "\n".join(lines)


# ============================================================
#  МОНИТОРИНГ ПОЕЗДОВ (polling)
# ============================================================

async def trains_monitor(bot: Bot):
    """Каждые TRAINS_INTERVAL секунд проверяет новые поезда."""
    log.info("🚂 Мониторинг поездов запущен")

    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                active = await fetch_active_trains(session)

                for t in active:
                    tid = t["id"]
                    if tid in seen_trains:
                        continue

                    # Новый поезд — грузим детали
                    info = await fetch_train_details(session, tid)
                    if not info:
                        continue

                    seen_trains.add(tid)
                    msg = format_train_message(info)

                    await bot.send_message(
                        chat_id=TARGET_CHAT_ID,
                        text=msg,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                    log.info(f"✅ Отправлен поезд {info['number']} от {info['streamer']}")

            except Exception as e:
                log.error(f"Ошибка мониторинга поездов: {e}")

            await asyncio.sleep(TRAINS_INTERVAL)


# ============================================================
#  МОНИТОРИНГ ПРОМОКОДОВ (Telethon)
# ============================================================

def is_clean_promo(code: str) -> bool:
    """
    Проверяет что промокод чистый:
    - начинается с ZM_
    - содержит только заглавные буквы и цифры после ZM_
    - нет звёздочек, точек, пробелов, скобок и т.д.
    """
    return bool(re.fullmatch(r'ZM_[A-Z0-9]{4,20}', code))


def extract_promos(text: str) -> list[str]:
    """Извлекает все чистые промокоды из текста сообщения."""
    # Приводим к верхнему регистру для поиска
    upper = text.upper()
    found = PROMO_RE.findall(upper)
    # Фильтруем только чистые
    return [code for code in found if is_clean_promo(code)]


async def start_telethon(bot: Bot):
    """Запускает Telethon userbot и слушает каналы."""
    log.info("📡 Telethon userbot запускается...")

    client = TelegramClient(
        SESSION,
        API_ID,
        API_HASH,
        proxy=PROXY,
    )

    await client.start()
    log.info("✅ Telethon авторизован")

    @client.on(events.NewMessage(chats=PROMO_CHANNELS))
    async def handle_channel_message(event):
        msg_text = event.message.message or ""
        if not msg_text:
            return

        promos = extract_promos(msg_text)
        if not promos:
            return

        for promo in promos:
            if promo in sent_promos:
                log.info(f"Промокод {promo} уже отправлялся, пропускаем")
                continue

            sent_promos.add(promo)

            # Определяем из какого канала
            try:
                chat = await event.get_chat()
                source = f"@{chat.username}" if chat.username else chat.title
            except Exception:
                source = "канал"

            text = (
                f"🎟️ <b>НОВЫЙ ПРОМОКОД ZOOMA!</b>\n"
                f"\n"
                f"<code>{promo}</code>\n"
                f"\n"
                f"📢 Источник: {source}\n"
                f"🔗 <a href=\"https://zma11.casino/affilates\">Активировать</a>"
            )

            await bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            log.info(f"✅ Промокод {promo} из {source} отправлен в чат")

    log.info(f"👂 Слушаю каналы: {', '.join(PROMO_CHANNELS)}")
    await client.run_until_disconnected()


# ============================================================
#  ТОЧКА ВХОДА
# ============================================================

async def main():
    bot = Bot(token=BOT_TOKEN)

    # Проверка подключения бота
    me = await bot.get_me()
    log.info(f"🤖 Бот запущен: @{me.username}")

    # Запускаем оба модуля параллельно
    await asyncio.gather(
        trains_monitor(bot),
        start_telethon(bot),
    )


if __name__ == "__main__":
    asyncio.run(main())
