import os
import time
import random
import asyncio
import sys
from typing import Optional

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import asyncpg

# =========================
# LOAD ENV
# =========================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# BOT_OWNERS="id,id,id"
BOT_OWNERS: set[int] = set()
for part in (os.getenv("BOT_OWNERS", "") or "").split(","):
    part = part.strip()
    if part.isdigit():
        BOT_OWNERS.add(int(part))

# =========================
# CONFIG (тут твои роли)
# =========================

# Реакционные роли (emoji -> role_id)
REACTION_ROLE_MAP = {
    "🎯": 1477939798600712222,  # Спиннинг
    "🐟": 1477940426085367808,  # Донка
    "🪱": 1477940549674602626,  # Поплавок
    "🧊": 1477940631119859773,  # Фидер
    "🪝": 1477940689244520611,  # Кастинг
    "🐠": 1477940771918315701,  # Универсал
}

# Ранговые роли (выдаём только 1 — самую высокую)
LEVEL_ROLE_LADDER = {
    1: 1477952593618534400,    # 🐣 Новичок
    5: 1477952664900997191,    # 🐟 Рыбак
    10: 1477952755644629177,   # 🐠 Профи
    20: 1477952840650592497,   # 🦈 Трофейщик
    30: 1477952930932985997,   # 👑 Легенда
}

# Авто-роль новичку (поставь ID роли "Русская рыбалка")
AUTO_JOIN_ROLE_ID = 0  # <-- сюда ID роли или оставь 0 если не надо

# XP за сообщения
XP_MIN = 8
XP_MAX = 15
XP_COOLDOWN = 90
MIN_MSG_LEN = 8

# XP за войс
VOICE_TICK_SECONDS = 60
VOICE_XP_PER_TICK = 5

# Авто-войс
AUTO_VOICE_BASE_NAME = "🧊 Комната"
AUTO_VOICE_CLEANUP_SECONDS = 60

# =========================
# DISCORD INIT
# =========================
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True
intents.reactions = True

bot = commands.Bot(command_prefix=("!", "."), intents=intents)

db: Optional[asyncpg.Pool] = None

# cooldown xp messages
LAST_XP_TS: dict[tuple[int, int], float] = {}

# авто-войс: 1 комната на юзера
AUTO_VOICE_USER_ROOM: dict[tuple[int, int], int] = {}   # (guild_id, user_id) -> channel_id
AUTO_VOICE_ROOM_OWNER: dict[tuple[int, int], int] = {}  # (guild_id, channel_id) -> user_id

# =========================
# OWNER-ONLY CHECK
# =========================
def owner_only():
    async def predicate(ctx: commands.Context):
        # Админы тоже могут (если хочешь только ты — убери эту строку)
        if ctx.author.guild_permissions.administrator:
            return True
        return ctx.author.id in BOT_OWNERS
    return commands.check(predicate)

# =========================
# DB HELPERS
# =========================
async def db_exec(query: str, *args):
    if not db:
        return
    async with db.acquire() as conn:
        await conn.execute(query, *args)

async def db_fetchrow(query: str, *args):
    if not db:
        return None
    async with db.acquire() as conn:
        return await conn.fetchrow(query, *args)

async def init_db():
    global db
    if not DATABASE_URL:
        print("⚠ DATABASE_URL не задан — бот без БД (XP/настройки не сохраняются).")
        return

    db = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    print("✅ Database connected")

    # Таблицы (минимальный каркас)
    await db_exec("""
    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id BIGINT PRIMARY KEY,
        role_message_id BIGINT NOT NULL DEFAULT 0,
        auto_voice_hub_id BIGINT NOT NULL DEFAULT 0
    );
    """)

    await db_exec("""
    CREATE TABLE IF NOT EXISTS user_stats (
        guild_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        xp BIGINT NOT NULL DEFAULT 0,
        voice_seconds BIGINT NOT NULL DEFAULT 0,
        msg_count BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    );
    """)

    # Авто-миграции (если таблицы были старые)
    await db_exec("""ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS xp BIGINT NOT NULL DEFAULT 0;""")
    await db_exec("""ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS voice_seconds BIGINT NOT NULL DEFAULT 0;""")
    await db_exec("""ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS msg_count BIGINT NOT NULL DEFAULT 0;""")
    await db_exec("""ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS role_message_id BIGINT NOT NULL DEFAULT 0;""")
    await db_exec("""ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS auto_voice_hub_id BIGINT NOT NULL DEFAULT 0;""")

async def get_settings(guild_id: int) -> dict:
    row = await db_fetchrow(
        "SELECT role_message_id, auto_voice_hub_id FROM guild_settings WHERE guild_id=$1",
        guild_id
    )
    if not row:
        await db_exec(
            "INSERT INTO guild_settings(guild_id, role_message_id, auto_voice_hub_id) VALUES($1, 0, 0) ON CONFLICT DO NOTHING",
            guild_id
        )
        return {"role_message_id": 0, "auto_voice_hub_id": 0}
    return {"role_message_id": int(row["role_message_id"]), "auto_voice_hub_id": int(row["auto_voice_hub_id"])}

async def set_role_message_id(guild_id: int, message_id: int):
    await db_exec("""
    INSERT INTO guild_settings(guild_id, role_message_id, auto_voice_hub_id)
    VALUES($1, $2, COALESCE((SELECT auto_voice_hub_id FROM guild_settings WHERE guild_id=$1), 0))
    ON CONFLICT (guild_id) DO UPDATE SET role_message_id=EXCLUDED.role_message_id;
    """, guild_id, message_id)

async def set_auto_voice_hub_id(guild_id: int, hub_id: int):
    await db_exec("""
    INSERT INTO guild_settings(guild_id, role_message_id, auto_voice_hub_id)
    VALUES($1, COALESCE((SELECT role_message_id FROM guild_settings WHERE guild_id=$1), 0), $2)
    ON CONFLICT (guild_id) DO UPDATE SET auto_voice_hub_id=EXCLUDED.auto_voice_hub_id;
    """, guild_id, hub_id)

async def get_user_row(guild_id: int, user_id: int):
    row = await db_fetchrow("""
        SELECT xp, voice_seconds, msg_count
        FROM user_stats
        WHERE guild_id=$1 AND user_id=$2
    """, guild_id, user_id)
    if not row:
        await db_exec("""
            INSERT INTO user_stats(guild_id, user_id, xp, voice_seconds, msg_count)
            VALUES($1, $2, 0, 0, 0)
            ON CONFLICT DO NOTHING
        """, guild_id, user_id)
        return 0, 0, 0
    return int(row["xp"]), int(row["voice_seconds"]), int(row["msg_count"])

async def add_xp_and_msgs(guild_id: int, user_id: int, xp_add: int, msg_add: int):
    await db_exec("""
        INSERT INTO user_stats(guild_id, user_id, xp, voice_seconds, msg_count)
        VALUES($1, $2, $3, 0, $4)
        ON CONFLICT (guild_id, user_id)
        DO UPDATE SET xp=user_stats.xp + EXCLUDED.xp,
                      msg_count=user_stats.msg_count + EXCLUDED.msg_count;
    """, guild_id, user_id, xp_add, msg_add)

async def add_voice_and_xp(guild_id: int, user_id: int, seconds_add: int, xp_add: int):
    await db_exec("""
        INSERT INTO user_stats(guild_id, user_id, xp, voice_seconds, msg_count)
        VALUES($1, $2, $4, $3, 0)
        ON CONFLICT (guild_id, user_id)
        DO UPDATE SET xp=user_stats.xp + EXCLUDED.xp,
                      voice_seconds=user_stats.voice_seconds + EXCLUDED.voice_seconds;
    """, guild_id, user_id, seconds_add, xp_add)

# =========================
# LEVELS / ROLES
# =========================
def level_from_xp(xp: int) -> int:
    if xp <= 0:
        return 1
    return max(1, int((xp / 600) ** 0.5) + 1)

def best_level_role_id(level: int) -> Optional[int]:
    best = None
    for lvl_req, role_id in LEVEL_ROLE_LADDER.items():
        if level >= lvl_req:
            if best is None or lvl_req > best[0]:
                best = (lvl_req, role_id)
    return best[1] if best else None

async def apply_level_role(member: discord.Member, level: int):
    target_role_id = best_level_role_id(level)
    if not target_role_id:
        return
    target_role = member.guild.get_role(target_role_id)
    if not target_role:
        return

    ladder_role_ids = set(LEVEL_ROLE_LADDER.values())
    to_remove = [r for r in member.roles if r.id in ladder_role_ids and r.id != target_role_id]

    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason="Level role cleanup")
        if target_role not in member.roles:
            await member.add_roles(target_role, reason="Level reward")
    except discord.Forbidden:
        print("Forbidden: Manage Roles / позиция роли бота ниже целевых ролей.")
    except Exception as e:
        print("apply_level_role error:", e)

# =========================
# REACTION ROLES
# =========================
async def add_reaction_role(guild_id: int, user_id: int, emoji: str):
    role_id = REACTION_ROLE_MAP.get(emoji)
    if not role_id:
        return
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    member = guild.get_member(user_id)
    if not member:
        return
    role = guild.get_role(role_id)
    if not role:
        return
    try:
        if role not in member.roles:
            await member.add_roles(role, reason="Reaction role")
    except discord.Forbidden:
        print("Forbidden: Manage Roles / позиция роли бота.")
    except Exception as e:
        print("add_reaction_role error:", e)

async def remove_reaction_role(guild_id: int, user_id: int, emoji: str):
    role_id = REACTION_ROLE_MAP.get(emoji)
    if not role_id:
        return
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    member = guild.get_member(user_id)
    if not member:
        return
    role = guild.get_role(role_id)
    if not role:
        return
    try:
        if role in member.roles:
            await member.remove_roles(role, reason="Reaction role removed")
    except discord.Forbidden:
        print("Forbidden: Manage Roles / позиция роли бота.")
    except Exception as e:
        print("remove_reaction_role error:", e)

# =========================
# AUTO VOICE
# =========================
async def is_hub_channel(channel: discord.VoiceChannel) -> bool:
    settings = await get_settings(channel.guild.id)
    hub_id = settings["auto_voice_hub_id"]
    return bool(hub_id) and channel.id == hub_id

async def create_or_move_personal_room(member: discord.Member, hub_channel: discord.VoiceChannel):
    key = (member.guild.id, member.id)
    existing_id = AUTO_VOICE_USER_ROOM.get(key)

    # если уже есть комната — переносим
    if existing_id:
        ch = member.guild.get_channel(existing_id)
        if ch and isinstance(ch, discord.VoiceChannel):
            try:
                await member.move_to(ch, reason="Auto voice: move to existing room")
            except discord.Forbidden:
                print("Forbidden: Move Members")
            return
        else:
            AUTO_VOICE_USER_ROOM.pop(key, None)

    category = hub_channel.category
    new_name = f"{AUTO_VOICE_BASE_NAME} — {member.display_name}"

    try:
        new_channel = await member.guild.create_voice_channel(
            name=new_name,
            category=category,
            reason="Auto voice create"
        )
        AUTO_VOICE_USER_ROOM[key] = new_channel.id
        AUTO_VOICE_ROOM_OWNER[(member.guild.id, new_channel.id)] = member.id
        await member.move_to(new_channel, reason="Auto voice move")
    except discord.Forbidden:
        print("Forbidden: Manage Channels + Move Members")
    except Exception as e:
        print("create_or_move_personal_room error:", e)

@tasks.loop(seconds=AUTO_VOICE_CLEANUP_SECONDS)
async def auto_voice_cleanup_loop():
    for guild in bot.guilds:
        # удаляем пустые автокомнаты и чистим мапы
        for vc in list(guild.voice_channels):
            owner_id = AUTO_VOICE_ROOM_OWNER.get((guild.id, vc.id))
            if owner_id and len(vc.members) == 0:
                try:
                    await vc.delete(reason="Auto voice cleanup (empty)")
                except Exception:
                    pass
                AUTO_VOICE_ROOM_OWNER.pop((guild.id, vc.id), None)
                AUTO_VOICE_USER_ROOM.pop((guild.id, owner_id), None)

# =========================
# VOICE XP
# =========================
@tasks.loop(seconds=VOICE_TICK_SECONDS)
async def voice_xp_tick():
    if not db:
        return
   for guild in bot.guilds:
    for vc in guild.voice_channels:

        members = [m for m in vc.members if not m.bot]

        # минимум 2 человека в войсе для фарма XP
        if len(members) < 2:
            continue

        for m in members:
            try:
                old_xp, _, _ = await get_user_row(guild.id, m.id)
                await add_voice_and_xp(guild.id, m.id, VOICE_TICK_SECONDS, VOICE_XP_PER_TICK)
                new_xp, _, _ = await get_user_row(guild.id, m.id)

                if level_from_xp(new_xp) > level_from_xp(old_xp):
                    await apply_level_role(m, level_from_xp(new_xp))

            except Exception as e:
                print("voice_xp_tick error:", e)

# =========================
# EVENTS
# =========================
@bot.event
async def on_ready():
    print(f"🐻 Berloga Bot запущен как {bot.user}")

    if not db:
        await init_db()

    if not voice_xp_tick.is_running():
        voice_xp_tick.start()
    if not auto_voice_cleanup_loop.is_running():
        auto_voice_cleanup_loop.start()

    # покажем настройки
    for g in bot.guilds:
        s = await get_settings(g.id)
        print(f"✅ Settings: guild={g.id} ROLE_MESSAGE_ID={s['role_message_id']} AUTO_VOICE_HUB_ID={s['auto_voice_hub_id']}")

@bot.event
async def on_member_join(member: discord.Member):
    # авто-роль
    if AUTO_JOIN_ROLE_ID:
        try:
            role = member.guild.get_role(AUTO_JOIN_ROLE_ID)
            if role:
                await member.add_roles(role, reason="Auto role on join")
        except Exception as e:
            print("Auto join role error:", e)

    # приветствие ЛС
    try:
        await member.send(
            f"🐻 Добро пожаловать в Бункер 'Берлога', {member.name}!\n\n"
            "🎯 Выбери стиль ловли в #выбрать-роль\n"
            "📜 Ознакомься с правилами\n"
            "Удачного клёва!"
        )
    except Exception:
        pass

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None or payload.user_id == bot.user.id:
        return
    settings = await get_settings(payload.guild_id)
    role_msg_id = settings["role_message_id"]
    if role_msg_id and payload.message_id != role_msg_id:
        return
    emoji = str(payload.emoji)
    if emoji not in REACTION_ROLE_MAP:
        return
    await add_reaction_role(payload.guild_id, payload.user_id, emoji)

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None:
        return
    settings = await get_settings(payload.guild_id)
    role_msg_id = settings["role_message_id"]
    if role_msg_id and payload.message_id != role_msg_id:
        return
    emoji = str(payload.emoji)
    if emoji not in REACTION_ROLE_MAP:
        return
    await remove_reaction_role(payload.guild_id, payload.user_id, emoji)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    await bot.process_commands(message)

    if not db:
        return

    key = (message.guild.id, message.author.id)
    now = time.time()

    if now - LAST_XP_TS.get(key, 0) < XP_COOLDOWN:
        return

    content = (message.content or "").strip()
    if len(content) < MIN_MSG_LEN:
        return

    LAST_XP_TS[key] = now
    gained = random.randint(XP_MIN, XP_MAX)

    try:
        old_xp, _, _ = await get_user_row(message.guild.id, message.author.id)
        await add_xp_and_msgs(message.guild.id, message.author.id, gained, 1)
        new_xp, _, _ = await get_user_row(message.guild.id, message.author.id)

        if level_from_xp(new_xp) > level_from_xp(old_xp):
            await apply_level_role(message.author, level_from_xp(new_xp))
    except Exception as e:
        print("on_message xp error:", e)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot or not after.channel:
        return
    if isinstance(after.channel, discord.VoiceChannel) and await is_hub_channel(after.channel):
        await create_or_move_personal_room(member, after.channel)

# =========================
# COMMANDS (public)
# =========================
@bot.command()
async def ping(ctx):
    await ctx.send("🏓 Pong!")

@bot.command(name="xp")
async def xp_cmd(ctx, member: Optional[discord.Member] = None):
    member = member or ctx.author
    if not db:
        return await ctx.reply("⚠ База не подключена, XP временно недоступен.")
    xp_val, voice_sec, msg_count = await get_user_row(ctx.guild.id, member.id)
    lvl = level_from_xp(xp_val)
    await ctx.reply(
        f"📈 {member.mention}: XP = **{xp_val}**, уровень = **{lvl}**, "
        f"войс = **{voice_sec//60} мин**, сообщений = **{msg_count}**"
    )

@bot.command()
async def rank(ctx, member: Optional[discord.Member] = None):
    member = member or ctx.author
    if not db:
        return await ctx.reply("⚠ База не подключена.")
    xp_val, _, _ = await get_user_row(ctx.guild.id, member.id)
    lvl = level_from_xp(xp_val)
    role_id = best_level_role_id(lvl)
    role = ctx.guild.get_role(role_id) if role_id else None
    await ctx.reply(f"🏅 {member.mention}: уровень **{lvl}** | роль: **{role.name if role else 'нет'}**")

@bot.command()
@commands.is_owner()
async def recalcall(ctx):
    await ctx.send("🔄 Пересчитываю роли всем участникам...")

    count = 0

    for member in ctx.guild.members:
        if member.bot:
            continue

        try:
            xp_val, _, _ = await get_user_row(ctx.guild.id, member.id)
            lvl = level_from_xp(xp_val)

            await apply_level_role(member, lvl)

            count += 1
            await asyncio.sleep(0.3)

        except Exception as e:
            print("recalcall error:", e)

    await ctx.send(f"✅ Готово. Пересчитано ролей: **{count}**")

# =========================
# COMMANDS (owner/admin)
# =========================
@bot.command()
@owner_only()
async def setrolemsg(ctx, message_id: int):
    await set_role_message_id(ctx.guild.id, int(message_id))
    await ctx.reply(f"✅ ROLE_MESSAGE_ID установлен: `{message_id}`")

@bot.command()
@owner_only()
async def syncroles(ctx):
    settings = await get_settings(ctx.guild.id)
    role_msg_id = settings["role_message_id"]
    if not role_msg_id:
        return await ctx.reply("⚠ Сначала задай ROLE_MESSAGE_ID командой `!setrolemsg <id>`.")

    try:
        msg = await ctx.channel.fetch_message(role_msg_id)
    except Exception:
        return await ctx.reply("❌ Не смог найти сообщение. Запусти `!syncroles` в том же канале, где сообщение.")

    for emoji in REACTION_ROLE_MAP.keys():
        try:
            await msg.add_reaction(emoji)
        except Exception:
            pass

    await ctx.reply("✅ Реакции добавлены на сообщение.")

@bot.command()
@owner_only()
async def sethub(ctx, channel_id: int):
    await set_auto_voice_hub_id(ctx.guild.id, int(channel_id))
    await ctx.reply(f"✅ AUTO_VOICE_HUB_ID установлен: `{channel_id}`")

@bot.command()
@owner_only()
async def giveall(ctx, role: discord.Role):
    await ctx.send(f"⏳ Выдаю роль **{role.name}** всем участникам...")
    count = 0
    for member in ctx.guild.members:
        if member.bot:
            continue
        if role not in member.roles:
            try:
                await member.add_roles(role, reason="Mass give role")
                count += 1
                await asyncio.sleep(0.3)
            except discord.Forbidden:
                pass
    await ctx.send(f"✅ Готово. Роль выдана **{count}** участникам.")

@bot.command()
@owner_only()
async def removeall(ctx, role: discord.Role):
    await ctx.send(f"⏳ Убираю роль **{role.name}** у всех участников...")
    count = 0
    for member in ctx.guild.members:
        if role in member.roles:
            try:
                await member.remove_roles(role, reason="Mass remove role")
                count += 1
                await asyncio.sleep(0.3)
            except discord.Forbidden:
                pass
    await ctx.send(f"✅ Готово. Роль убрана у **{count}** участников.")

@bot.command()
@owner_only()
async def restart(ctx):
    await ctx.send("♻️ Перезапускаю бота...")
    # Закрываем соединения аккуратно
    try:
        if db:
            await db.close()
    except Exception:
        pass

    # Закрываем Discord соединение
    await bot.close()

    # Завершаем процесс — Railway обычно поднимет снова
    os._exit(0)

# =========================
# RUN
# =========================
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN не задан. Добавь его в Railway Variables.")
bot.run(TOKEN)
