import os
import random
import time
import asyncio
from typing import Optional

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import asyncpg

# =========================================
# CONFIG (настрой тут)
# =========================================

# emoji -> role_id (замени role_id на реальные)
REACTION_ROLE_MAP = {
    "🎯": 1477939798600712222,  # Спиннинг
    "🐟": 1477940426085367808,  # Донка
    "🪱": 1477940549674602626,  # Поплавок
    "🧊": 1477940631119859773,  # Фидер
    "🪝": 1477940689244520611,  # Кастинг
    "🐠": 1477940771918315701,  # Универсал
}

# Лестница ранговых ролей (выдаём ТОЛЬКО одну самую высокую)
LEVEL_ROLE_LADDER = {
    1: 1477952593618534400,   # 🐣 Новичок
    5: 1477952664900997191,   # 🐟 Рыбак
    10: 1477952755644629177,  # 🐠 Профи
    20: 1477952840650592497,  # 🦈 Трофейщик
    30: 1477952930932985997,  # 👑 Легенда
}

# XP за сообщения
XP_MIN = 8
XP_MAX = 15
XP_COOLDOWN = 90   # сек
MIN_MSG_LEN = 8

# XP за войс (в минуту)
VOICE_XP_PER_MIN = 2
VOICE_TICK_SECONDS = 60

# =========================================
# INIT
# =========================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# Эти переменные можем хранить как "настройки" в БД, но также читаем env по умолчанию
ENV_ROLE_MESSAGE_ID = int(os.getenv("ROLE_MESSAGE_ID", "0") or "0")
ENV_AUTO_VOICE_HUB_ID = int(os.getenv("AUTO_VOICE_HUB_ID", "0") or "0")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)

db: Optional[asyncpg.Pool] = None

# анти-спам по XP за сообщения
LAST_XP_TS: dict[tuple[int, int], float] = {}

# авто-войс: 1 человек = 1 комната
AUTO_VOICE_USER_ROOM: dict[int, int] = {}   # user_id -> channel_id
AUTO_VOICE_ROOM_OWNER: dict[int, int] = {}  # channel_id -> user_id

# =========================================
# DB HELPERS
# =========================================

async def db_exec(query: str, *args):
    if not db:
        return
    try:
        async with db.acquire() as conn:
            await conn.execute(query, *args)
    except Exception as e:
        print("DB execute error:", e)

async def db_fetchrow(query: str, *args):
    if not db:
        return None
    try:
        async with db.acquire() as conn:
            return await conn.fetchrow(query, *args)
    except Exception as e:
        print("DB fetchrow error:", e)
        return None

async def db_fetch(query: str, *args):
    if not db:
        return []
    try:
        async with db.acquire() as conn:
            return await conn.fetch(query, *args)
    except Exception as e:
        print("DB fetch error:", e)
        return []

async def init_db():
    global db
    if not DATABASE_URL:
        print("⚠ DATABASE_URL не задан, бот запустится без БД (XP не сохранится).")
        return

    db = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    print("✅ Database connected")

    # Таблица user_stats
    await db_exec("""
    CREATE TABLE IF NOT EXISTS user_stats (
        guild_id BIGINT NOT NULL,
        user_id  BIGINT NOT NULL,
        xp       BIGINT NOT NULL DEFAULT 0,
        voice_seconds BIGINT NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (guild_id, user_id)
    );
    """)

    # Таблица settings (настройки на гильдию)
    await db_exec("""
    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id BIGINT PRIMARY KEY,
        role_message_id BIGINT NOT NULL DEFAULT 0,
        auto_voice_hub_id BIGINT NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)

async def get_settings(guild_id: int) -> dict:
    # если нет записи — создадим из env дефолтов
    row = await db_fetchrow("SELECT * FROM guild_settings WHERE guild_id=$1", guild_id)
    if not row:
        await db_exec("""
            INSERT INTO guild_settings (guild_id, role_message_id, auto_voice_hub_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id) DO NOTHING
        """, guild_id, ENV_ROLE_MESSAGE_ID, ENV_AUTO_VOICE_HUB_ID)
        return {
            "role_message_id": ENV_ROLE_MESSAGE_ID,
            "auto_voice_hub_id": ENV_AUTO_VOICE_HUB_ID,
        }
    return {
        "role_message_id": int(row["role_message_id"]),
        "auto_voice_hub_id": int(row["auto_voice_hub_id"]),
    }

async def set_role_message_id(guild_id: int, message_id: int):
    await db_exec("""
        INSERT INTO guild_settings (guild_id, role_message_id)
        VALUES ($1, $2)
        ON CONFLICT (guild_id)
        DO UPDATE SET role_message_id=EXCLUDED.role_message_id, updated_at=NOW()
    """, guild_id, message_id)

async def set_auto_voice_hub_id(guild_id: int, channel_id: int):
    await db_exec("""
        INSERT INTO guild_settings (guild_id, auto_voice_hub_id)
        VALUES ($1, $2)
        ON CONFLICT (guild_id)
        DO UPDATE SET auto_voice_hub_id=EXCLUDED.auto_voice_hub_id, updated_at=NOW()
    """, guild_id, channel_id)

async def add_xp(guild_id: int, user_id: int, delta: int) -> int:
    await db_exec("""
        INSERT INTO user_stats (guild_id, user_id, xp)
        VALUES ($1, $2, $3)
        ON CONFLICT (guild_id, user_id)
        DO UPDATE SET xp = user_stats.xp + $3, updated_at=NOW()
    """, guild_id, user_id, delta)

    row = await db_fetchrow("SELECT xp FROM user_stats WHERE guild_id=$1 AND user_id=$2", guild_id, user_id)
    return int(row["xp"]) if row else 0

async def add_voice_seconds(guild_id: int, user_id: int, delta_seconds: int) -> tuple[int, int]:
    await db_exec("""
        INSERT INTO user_stats (guild_id, user_id, voice_seconds)
        VALUES ($1, $2, $3)
        ON CONFLICT (guild_id, user_id)
        DO UPDATE SET voice_seconds = user_stats.voice_seconds + $3, updated_at=NOW()
    """, guild_id, user_id, delta_seconds)

    row = await db_fetchrow("""
        SELECT xp, voice_seconds FROM user_stats
        WHERE guild_id=$1 AND user_id=$2
    """, guild_id, user_id)

    if not row:
        return 0, 0
    return int(row["xp"]), int(row["voice_seconds"])

async def get_user_stats(guild_id: int, user_id: int) -> tuple[int, int]:
    row = await db_fetchrow("""
        SELECT xp, voice_seconds FROM user_stats
        WHERE guild_id=$1 AND user_id=$2
    """, guild_id, user_id)
    if not row:
        return 0, 0
    return int(row["xp"]), int(row["voice_seconds"])

# =========================================
# LEVELS / ROLES
# =========================================

def level_from_xp(xp: int) -> int:
    # медленный рост
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

    guild = member.guild
    target_role = guild.get_role(target_role_id)
    if not target_role:
        return

    ladder_role_ids = set(LEVEL_ROLE_LADDER.values())
    member_role_ids = {r.id for r in member.roles}

    # если уже ок
    if target_role_id in member_role_ids and len(member_role_ids.intersection(ladder_role_ids)) == 1:
        return

    to_remove = [r for r in member.roles if r.id in ladder_role_ids and r.id != target_role_id]
    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason="Level rank cleanup")
        if target_role not in member.roles:
            await member.add_roles(target_role, reason="Level reward")
    except discord.Forbidden:
        print("Forbidden: нет прав Manage Roles или роль бота ниже нужных ролей.")
    except Exception as e:
        print("Ошибка выдачи ранговой роли:", e)

# =========================================
# REACTION ROLES
# =========================================

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
        print("Forbidden: нет прав выдавать роли (Manage Roles).")
    except Exception as e:
        print("Reaction add error:", e)

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
        print("Forbidden: нет прав снимать роли (Manage Roles).")
    except Exception as e:
        print("Reaction remove error:", e)

# =========================================
# VOICE XP LOOP
# =========================================

@tasks.loop(seconds=VOICE_TICK_SECONDS)
async def voice_xp_tick():
    # каждые 60 секунд пробегаемся по гильдиям/войсам
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            # считаем только если реально кто-то сидит (не боты)
            members = [m for m in vc.members if not m.bot]
            if not members:
                continue

            for m in members:
                # добавляем войс секунды и XP
                # 60 секунд войса = VOICE_XP_PER_MIN xp
                await add_voice_seconds(guild.id, m.id, VOICE_TICK_SECONDS)

                gained_xp = VOICE_XP_PER_MIN
                old_xp, _ = await get_user_stats(guild.id, m.id)
                new_xp = await add_xp(guild.id, m.id, gained_xp)

                old_lvl = level_from_xp(old_xp)
                new_lvl = level_from_xp(new_xp)
                if new_lvl > old_lvl:
                    await apply_level_role(m, new_lvl)

# =========================================
# AUTO VOICE HUB
# =========================================

async def is_hub_channel(channel: discord.abc.GuildChannel) -> bool:
    if not channel.guild:
        return False
    settings = await get_settings(channel.guild.id)
    hub_id = settings.get("auto_voice_hub_id", 0)
    return hub_id and channel.id == hub_id

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return

    guild = member.guild

    # 1) Если зашёл в HUB — создаём/переносим в личную комнату (1 человек = 1 комната)
    if after.channel and isinstance(after.channel, discord.VoiceChannel):
        if await is_hub_channel(after.channel):

            existing_id = AUTO_VOICE_USER_ROOM.get(member.id)
            if existing_id:
                ch = guild.get_channel(existing_id)
                if ch:
                    try:
                        await member.move_to(ch, reason="Auto voice: move to existing room")
                    except discord.Forbidden:
                        print("Forbidden: нужны права Manage Channels + Move Members.")
                    return
                else:
                    AUTO_VOICE_USER_ROOM.pop(member.id, None)

            category = after.channel.category
            new_name = f"🧊 Комната — {member.display_name}"

            try:
                new_channel = await guild.create_voice_channel(
                    name=new_name,
                    category=category,
                    reason="Auto voice create"
                )
                AUTO_VOICE_USER_ROOM[member.id] = new_channel.id
                AUTO_VOICE_ROOM_OWNER[new_channel.id] = member.id

                await member.move_to(new_channel, reason="Auto voice move")
            except discord.Forbidden:
                print("Forbidden: нужны права Manage Channels + Move Members.")
            except Exception as e:
                print("Auto voice create/move error:", e)
            return

    # 2) Удаление комнаты: если вышли из авто-комнаты и она стала пустой
    if before.channel and isinstance(before.channel, discord.VoiceChannel):
        owner_id = AUTO_VOICE_ROOM_OWNER.get(before.channel.id)
        if owner_id:
            if len(before.channel.members) == 0:
                try:
                    await before.channel.delete(reason="Auto voice cleanup (empty)")
                except discord.Forbidden:
                    print("Forbidden: нет прав удалить канал (Manage Channels).")
                except Exception as e:
                    print("Auto voice delete error:", e)

                AUTO_VOICE_ROOM_OWNER.pop(before.channel.id, None)
                AUTO_VOICE_USER_ROOM.pop(owner_id, None)

# =========================================
# EVENTS / XP MESSAGE
# =========================================

@bot.event
async def on_ready():
    print(f"🐻 Berloga Bot запущен как {bot.user}")

    if not db:
        await init_db()

    # стартуем войс тик
    if not voice_xp_tick.is_running():
        voice_xp_tick.start()

    # покажем настройки для первой гильдии (если есть)
    if bot.guilds:
        s = await get_settings(bot.guilds[0].id)
        print(f"✅ Settings loaded: ROLE_MESSAGE_ID={s['role_message_id']}, AUTO_VOICE_HUB_ID={s['auto_voice_hub_id']}")

@bot.event
async def on_member_join(member: discord.Member):
    try:
        await member.send(
            f"🐻 Добро пожаловать в Бункер 'Берлога', {member.name}!\n\n"
            "🎯 Выбери стиль ловли в #выбрать-роль\n"
            "📜 Ознакомься с правилами\n"
            "🏆 Участвуй в турнирах\n\n"
            "Удачного клёва!"
        )
    except Exception:
        pass

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None:
        return
    if payload.user_id == bot.user.id:
        return

    settings = await get_settings(payload.guild_id)
    role_message_id = settings.get("role_message_id", 0)

    if role_message_id and payload.message_id != role_message_id:
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
    role_message_id = settings.get("role_message_id", 0)

    if role_message_id and payload.message_id != role_message_id:
        return

    emoji = str(payload.emoji)
    if emoji not in REACTION_ROLE_MAP:
        return

    await remove_reaction_role(payload.guild_id, payload.user_id, emoji)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # команды сначала
    await bot.process_commands(message)

    # XP за сообщения
    key = (message.guild.id, message.author.id)
    now = time.time()

    if now - LAST_XP_TS.get(key, 0) < XP_COOLDOWN:
        return

    content = (message.content or "").strip()
    if len(content) < MIN_MSG_LEN:
        return

    LAST_XP_TS[key] = now
    gained = random.randint(XP_MIN, XP_MAX)

    old_xp, _ = await get_user_stats(message.guild.id, message.author.id)
    new_xp = await add_xp(message.guild.id, message.author.id, gained)

    old_lvl = level_from_xp(old_xp)
    new_lvl = level_from_xp(new_xp)

    if new_lvl > old_lvl:
        await apply_level_role(message.author, new_lvl)

# =========================================
# COMMANDS
# =========================================

def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator

@bot.command()
async def ping(ctx):
    await ctx.send("🏓 Pong!")

@bot.command()
async def xp(ctx, member: discord.Member | None = None):
    member = member or ctx.author
    xp_val, voice_seconds = await get_user_stats(ctx.guild.id, member.id)
    lvl = level_from_xp(xp_val)
    mins = voice_seconds // 60
    await ctx.reply(f"📈 {member.mention}: XP = **{xp_val}**, уровень = **{lvl}**, войс = **{mins} мин**")

@bot.command()
async def rank(ctx, member: discord.Member | None = None):
    member = member or ctx.author
    xp_val, _ = await get_user_stats(ctx.guild.id, member.id)
    lvl = level_from_xp(xp_val)
    role_id = best_level_role_id(lvl)
    role = ctx.guild.get_role(role_id) if role_id else None
    await ctx.reply(f"🏅 {member.mention}: уровень **{lvl}** | роль: **{role.name if role else 'нет'}**")

@bot.command()
async def setrolemsg(ctx, message_id: int):
    if not is_admin(ctx.author):
        return await ctx.reply("⛔ Только админ может это делать.")
    await set_role_message_id(ctx.guild.id, int(message_id))
    await ctx.reply(f"✅ ROLE_MESSAGE_ID установлен: `{message_id}`")

@bot.command()
async def sethub(ctx, voice_channel_id: int):
    if not is_admin(ctx.author):
        return await ctx.reply("⛔ Только админ может это делать.")
    await set_auto_voice_hub_id(ctx.guild.id, int(voice_channel_id))
    await ctx.reply(f"✅ AUTO_VOICE_HUB_ID установлен: `{voice_channel_id}`\n"
                    f"Теперь при заходе в этот канал будет создаваться личная комната.")

@bot.command()
async def syncroles(ctx):
    if not is_admin(ctx.author):
        return await ctx.reply("⛔ Только админ может это делать.")

    settings = await get_settings(ctx.guild.id)
    role_message_id = settings.get("role_message_id", 0)
    if not role_message_id:
        return await ctx.reply("⚠ Сначала задай ROLE_MESSAGE_ID командой `!setrolemsg <id>`.")

    try:
        msg = await ctx.channel.fetch_message(role_message_id)
    except Exception:
        return await ctx.reply("❌ Не смог найти сообщение. Запусти команду в том же канале, где сообщение.")

    for emoji in REACTION_ROLE_MAP.keys():
        try:
            await msg.add_reaction(emoji)
        except Exception:
            pass

    await ctx.reply("✅ Реакции добавлены на сообщение.")

# =========================================
# RUN
# =========================================

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN не задан. Добавь переменную окружения DISCORD_TOKEN в Railway.")

bot.run(TOKEN)
