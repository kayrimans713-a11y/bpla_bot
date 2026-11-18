import asyncio, hashlib, logging, os, secrets, sys, time
from datetime import datetime, timedelta

import aiosqlite
from telethon import TelegramClient, events
from vkbottle.bot import Bot, Message
from vkbottle import Keyboard, KeyboardButtonColor, Text

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("radar")

DB_PATH = os.getenv("DB_PATH", "radar.db")
DEDUP_WINDOW = timedelta(minutes=10)

# ============ БД ============
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS users(vk_id INTEGER PRIMARY KEY, region TEXT, active INTEGER DEFAULT 1)")
        await db.execute("CREATE TABLE IF NOT EXISTS sent(vk_id INTEGER, event_hash TEXT, PRIMARY KEY(vk_id, event_hash))")
        await db.commit()

async def set_region(vk_id, region):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO users(vk_id, region, active) VALUES(?,?,1) ON CONFLICT(vk_id) DO UPDATE SET region=excluded.region, active=1", (vk_id, region))
        await db.commit()

async def stop_user(vk_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET active=0 WHERE vk_id=?", (vk_id,))
        await db.commit()

async def get_user(vk_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT region, active FROM users WHERE vk_id=?", (vk_id,))
        return await cur.fetchone()

async def users_by_region(region):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT vk_id FROM users WHERE region=? AND active=1", (region,))
        return [r[0] for r in await cur.fetchall()]

async def mark_sent(vk_id, h) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM sent WHERE vk_id=? AND event_hash=?", (vk_id, h))
        if await cur.fetchone():
            return True
        await db.execute("INSERT INTO sent(vk_id, event_hash) VALUES(?,?)", (vk_id, h))
        await db.commit()
        return False

# ============ Telegram ============
_recent = {}

def detect_regions(text):
    low = text.lower()
    return [rid for rid, (_, al) in config.REGIONS.items() if any(a in low for a in al)]

def make_hash(region, kind):
    return hashlib.md5(f"{region}:{kind}".encode()).hexdigest()

def should_dedup(region, kind):
    now = datetime.utcnow(); key = (region, kind)
    last = _recent.get(key); _recent[key] = now
    return last is not None and (now - last) < DEDUP_WINDOW

async def tg_loop(queue: asyncio.Queue):
    client = TelegramClient(config.TG_SESSION, config.TG_API_ID, config.TG_API_HASH)

    @client.on(events.NewMessage(chats=config.TG_CHANNELS))
    async def handler(event):
        text = event.message.message or ""
        if not text: return
        regions = detect_regions(text)
        if not regions: return
        low = text.lower()
        if any(w in low for w in config.CLEAR_WORDS):
            kind, prefix = "clear", "✅ ОТБОЙ"
        elif any(w in low for w in config.THREAT_WORDS):
            kind, prefix = "alert", "⚠️ УГРОЗА"
        else: return
        for r in regions:
            if should_dedup(r, kind): continue
            await queue.put({"region": r, "kind": kind, "hash": make_hash(r, kind), "text": f"{prefix}\n\n{text[:900]}"})

    while True:
        try:
            await client.start()
            log.info("Telethon запущен")
            await client.run_until_disconnected()
            log.warning("Telethon отключился, рестарт 5с")
            await asyncio.sleep(5)
        except Exception as e:
            log.exception(f"Telethon crash: {e}, рестарт 10с")
            await asyncio.sleep(10)

# ============ VK ============
bot = Bot(config.VK_TOKEN)
REGION_ITEMS = list(config.REGIONS.items())

def region_keyboard(page=0):
    kb = Keyboard(one_time=False, inline=False)
    chunk = REGION_ITEMS[page*9:(page+1)*9]
    for i, (rid, (title, _)) in enumerate(chunk):
        kb.add(Text(title, payload={"cmd": "set", "r": rid}))
        if (i + 1) % 3 == 0: kb.row()
    if page > 0:
        kb.add(Text("⬅️ Назад", payload={"cmd": "page", "p": page-1}, color=KeyboardButtonColor.SECONDARY))
    if (page+1)*9 < len(REGION_ITEMS):
        kb.add(Text("➡️ Ещё", payload={"cmd": "page", "p": page+1}, color=KeyboardButtonColor.SECONDARY))
    return kb

def main_keyboard():
    kb = Keyboard(one_time=False, inline=False)
    kb.add(Text("🌍 Выбрать регион", payload={"cmd": "choose"}), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("🔕 Отключить", payload={"cmd": "stop"}), color=KeyboardButtonColor.SECONDARY)
    return kb

@bot.on.message(text=["начать", "start", "/start", "Начать"])
async def start_h(m: Message):
    row = await get_user(m.from_id)
    if row and row[0] and row[1]:
        await m.answer(f"Ты подписан на: {config.REGIONS[row[0]][0]}", keyboard=main_keyboard())
    else:
        await m.answer("Привет! Пришлю уведомления об угрозах только по твоему региону.", keyboard=main_keyboard())

@bot.on.message(payload={"cmd": "choose"})
async def choose_h(m: Message):
    await m.answer("Выбери регион:", keyboard=region_keyboard(0))

@bot.on.message(payload={"cmd": "page"})
async def page_h(m: Message):
    await m.answer("Выбери регион:", keyboard=region_keyboard(int(m.payload["p"])))

@bot.on.message(payload={"cmd": "set"})
async def set_h(m: Message):
    rid = m.payload["r"]
    await set_region(m.from_id, rid)
    await m.answer(f"✅ Готово: {config.REGIONS[rid][0]}", keyboard=main_keyboard())

@bot.on.message(payload={"cmd": "stop"})
async def stop_h(m: Message):
    await stop_user(m.from_id)
    await m.answer("🔕 Отключено. Включить — /start", keyboard=main_keyboard())

# ============ Рассылка ============
async def dispatch_loop(queue: asyncio.Queue):
    while True:
        ev = await queue.get()
        try:
            for uid in await users_by_region(ev["region"]):
                if await mark_sent(uid, ev["hash"]): continue
                try:
                    await bot.api.messages.send(
                        user_id=uid,
                        message=ev["text"],
                        random_id=secrets.randbelow(2**31),
                        group_id=config.VK_GROUP_ID,
                    )
                    await asyncio.sleep(0.06)
                except Exception as e:
                    log.warning(f"send {uid}: {e}")
        finally:
            queue.task_done()

# ============ Автозапуск ============
async def main():
    log.info("Старт")
    await init_db()
    queue = asyncio.Queue()
    await asyncio.gather(tg_loop(queue), dispatch_loop(queue), bot.run_polling())

def run():
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.exception(f"crash: {e}, рестарт 15с")
            time.sleep(15)

if __name__ == "__main__":
    run()