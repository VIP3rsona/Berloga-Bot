import os
import re
import time
import random
import asyncio
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

# Владельцы (кому можно управлять ботом). Формат: "id,id,id"
BOT_OWNERS = set()
_raw_owners = os.getenv("BOT_OWNERS", "").strip()
if _raw_owners:
    for part in _raw_owners.split(","):
        part = part.strip()
        if part.isdigit():
            BOT_OWNERS.add(int(part))

# =========================
# CONFIG (меняй под себя)
# =========================

# emoji -> role_id (твои реальные role_id)
REACTION_ROLE_MAP = {
    "🎯": 1477939798600712222,  # Спиннинг
    "🐟": 1477940426085367808,  # Донка
    "🪱": 1477940549674602626,  # Поплавок
    "🧊": 1477940631119859773,  # Фидер
    "🪝": 1477940689244520611,  # Кастинг
    "🐠": 1477940771918315701,  # Универсал
}

# level_required -> role_id (твои ранговые роли)
LEVEL_ROLE_LADDER = {
    1: 1477952593618534400,    # 🐣 Новичок
    5: 1477952664900997191,    # 🐟 Рыбак
    10: 1477952755644629177,   # 🐠 Профи
    20: 1477952840650592497,   # 🦈 Трофейщик
    30: 1477952930932985997,   # 👑 Легенда
}

# XP за сообщения
XP_MIN = 8
XP_MAX = 15
XP_COOLDOWN = 90  # сек
MIN_MSG_LEN = 8

# XP за голос
VOICE_XP_PER_TICK = 5
VOICE_TICK_SECONDS = 60
VOICE_MIN_MEMBERS_IN_CHANNEL = 1  # если 1 — то даже один в войсе получает XP

# Авто-голос (HUB -> создаём личный)
AUTO_VOICE_BASE_NAME = "🎙 Комната"
AUTO_VOICE_CLEANUP_SECONDS = 60  # проверка пустых каждые N сек

# =========================
# DISCORD INIT
# =========================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)

db: Optional[asyncpg.Pool] = None

# memory for cooldown message xp
LAST_XP_TS: dict[tuple[int, int], float] = {}

# авто-войс: кто какую комнату создал (guild_id, user_id) -> channel_id
USER_AUTO_VOICE: dict[tuple[int, int], int] = {}

# =========================
# PERMISSIONS (owner-only)
# =========================

def is_owner_id(user_id: int) -> bool:
    return user_id in BOT_OWNERS

def owner_only():
    async def predicate(ctx: commands.Context):
        if ctx.author.guild_permissions.administrator:
            return True
        return is_owner_id(ctx.author.id)
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

async def db_fetchval(query: str, *args):
    if not db:
        return None
    async with db.acquire() as conn:
        return await conn.fetchval(query, *args)

async def ensure_schema():
    """
    Создаём таблицы и недостающие колонки автоматически.
    Так ты НЕ обязан руками выполнять SQL при изменениях.
    """
    # guild_settings
    await db_exec("""
    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id BIGINT PRIMARY KEY,
        role_message_id BIGINT NOT NULL DEFAULT 0,
        auto_voice_hub_id BIGINT NOT NULL DEFAULT 0
    );
    """)

    # user_stats
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

async def get_settings(guild_id: int) -> dict:
    row = await db_fetchrow(
        "SELECT role_message_id, auto_voice_hub_id FROM guild_settings WHERE guild_id=$1;",
        guild_id
    )
    if not row:
        await db_exec(
            "INSERT INTO guild_settings(guild_id, role_message_id, auto_voice_hub_id) VALUES($1, 0, 0) ON CONFLICT DO NOTHING;",
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

async def add_xp(guild_id: int, user_id: int, amount: int, add_msg_count: int = 0):
    await db_exec("""
    INSERT INTO user_stats(guild_id, user_id, xp, voice_seconds, msg_count)
    VALUES($1, $2, $3, 0, $4)
    ON CONFLICT (guild_id, user_id)
    DO UPDATE SET xp = user_stats.xp + EXCLUDED.xp,
                  msg_count = user_stats.msg_count + EXCLUDED.msg_count;
    """, guild_id, user_id, amount, add_msg_count)

async def add_voice_time_and_xp(guild_id: int, user_id: int, seconds: int, xp_amount: int):
    await db_exec("""
    INSERT INTO user_stats(guild_id, user_id, xp, voice_seconds, msg_count)
    VALUES($1, $2, $4, $3, 0)
    ON CONFLICT (guild_id, user_id)
    DO UPDATE SET xp = user_stats.xp + EXCLUDED.xp,
                  voice_seconds = user_stats.voice_seconds + EXCLUDED.voice_seconds;
    """, guild_id, user_id, seconds, xp_amount)

async def get_user_xp(guild_id: int, user_id: int) -> int:
    val = await db_fetchval("SELECT xp FROM user_stats WHERE guild_id=$1 AND user_id=$2;", guild_id, user_id)
    return int(val) if val is not None else 0

async def get_user_voice_seconds(guild_id: int, user_id: int) -> int:
    val = await db_fetchval("SELECT voice_seconds FROM user_stats WHERE guild_id=$1 AND user_id=$2;", guild_id, user_id)
    return int(val) if val is not None else 0

# =========================
# LEVEL / ROLES
# =========================

def level_from_xp(xp: int) -> int:
    # сложнее: медленный рост
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
    to_remove = [r for r in member.roles if r.id in ladder_role_ids and r.id != target_role_id]

    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason="Level rank cleanup")
        if target_role not in member.roles:
            await member.add_roles(target_role, reason="Level reward")
    except discord.Forbidden:
        print("Нет прав Manage Roles или роль бота ниже выдаваемых ролей.")
    except Exception as e:
        print("Ошибка выдачи ранговой роли:", e)

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
        print("Forbidden: нет прав выдавать роли (Manage Roles / позиция ролей).")
    except Exception as e:
        print("Ошибка выдачи reaction роли:", e)

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
        print("Forbidden: нет прав снимать роли.")
    except Exception as e:
        print("Ошибка снятия reaction роли:", e)

# =========================
# AUTO VOICE HELPERS
# =========================

def is_auto_voice_name(name: str) -> bool:
    return name.startswith(AUTO_VOICE_BASE_NAME)

async def cleanup_member_old_room(member: discord.Member):
    """Если у пользователя уже есть созданная комната — удаляем/чистим мапу при необходимости."""
    key = (member.guild.id, member.id)
    ch_id = USER_AUTO_VOICE.get(key)
    if not ch_id:
        return
    ch = member.guild.get_channel(ch_id)
    # если канала нет — просто чистим
    if not ch:
        USER_AUTO_VOICE.pop(key, None)

async def create_or_move_to_personal_room(member: discord.Member, hub_channel: discord.VoiceChannel):
    """
    1 пользователь = 1 комната.
    Если у него уже есть комната и она существует — просто переносим туда.
    """
    await cleanup_member_old_room(member)

    key = (member.guild.id, member.id)
    existing_id = USER_AUTO_VOICE.get(key)
    if existing_id:
        ch = member.guild.get_channel(existing_id)
        if ch and isinstance(ch, discord.VoiceChannel):
            try:
                await member.move_to(ch, reason="Move to existing personal room")
            except discord.Forbidden:
                print("Forbidden: нужны права Move Members.")
            return

    guild = member.guild
    category = hub_channel.category
    new_name = f"{AUTO_VOICE_BASE_NAME} — {member.display_name}"

    try:
        new_channel = await guild.create_voice_channel(
            name=new_name,
            category=category,
            reason="Auto voice create"
        )
        USER_AUTO_VOICE[key] = new_channel.id
        await member.move_to(new_channel, reason="Auto voice move")
    except discord.Forbidden:
        print("Forbidden: нужны права Manage Channels + Move Members.")
    except Exception as e:
        print("Auto voice create/move error:", e)

@tasks.loop(seconds=AUTO_VOICE_CLEANUP_SECONDS)
async def auto_voice_cleanup_loop():
    """Удаляем пустые авто-комнаты и чистим мапу USER_AUTO_VOICE."""
    for guild in bot.guilds:
        # соберём каналы к удалению
        to_delete = []
        for ch in guild.voice_channels:
            if is_auto_voice_name(ch.name) and len(ch.members) == 0:
                to_delete.append(ch)

        for ch in to_delete:
            try:
                await ch.delete(reason="Auto voice cleanup (empty)")
            except Exception:
                pass

        # чистим USER_AUTO_VOICE от несуществующих
        dead_keys = []
        for (g_id, u_id), ch_id in USER_AUTO_VOICE.items():
            if g_id != guild.id:
                continue
            if not guild.get_channel(ch_id):
                dead_keys.append((g_id, u_id))
        for k in dead_keys:
            USER_AUTO_VOICE.pop(k, None)

# =========================
# VOICE XP TICK
# =========================

@tasks.loop(seconds=VOICE_TICK_SECONDS)
async def voice_xp_tick():
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            if len(vc.members) < VOICE_MIN_MEMBERS_IN_CHANNEL:
                continue
            for member in vc.members:
                if member.bot:
                    continue
                # начисляем время + xp
                await add_voice_time_and_xp(guild.id, member.id, VOICE_TICK_SECONDS, VOICE_XP_PER_TICK)

                # проверка уровня/ранга (не каждую секунду, но норм раз в минуту)
                xp_val = await get_user_xp(guild.id, member.id)
                lvl = level_from_xp(xp_val)
                await apply_level_role(member, lvl)

# =========================
# EVENTS
# =========================

@bot.event
async def on_ready():
    global db
    print(f"🐻 Berloga Bot запущен как {bot.user}")

    if DATABASE_URL and not db:
        try:
            db = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
            await ensure_schema()
            print("✅ Database connected")
        except Exception as e:
            db = None
            print("❌ Database connect error:", e)
    else:
        print("⚠ DATABASE_URL не задан — работаем без БД (не рекомендую).")

    if not voice_xp_tick.is_running():
        voice_xp_tick.start()
    if not auto_voice_cleanup_loop.is_running():
        auto_voice_cleanup_loop.start()

    # просто лог текущих настроек по всем гильдиям
    for g in bot.guilds:
        try:
            s = await get_settings(g.id)
            print(f"✅ Settings loaded: guild={g.id}, ROLE_MESSAGE_ID={s['role_message_id']}, AUTO_VOICE_HUB_ID={s['auto_voice_hub_id']}")
        except Exception as e:
            print("Settings load error:", e)

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

    # команды
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

    old_xp = await get_user_xp(message.guild.id, message.author.id)
    await add_xp(message.guild.id, message.author.id, gained, add_msg_count=1)
    new_xp = old_xp + gained

    old_lvl = level_from_xp(old_xp)
    new_lvl = level_from_xp(new_xp)

    if new_lvl > old_lvl:
        await apply_level_role(message.author, new_lvl)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # если зашёл в канал
    if after.channel and isinstance(after.channel, discord.VoiceChannel):
        settings = await get_settings(member.guild.id)
        hub_id = settings["auto_voice_hub_id"]

        # если это HUB — создаём/переносим в личную
        if hub_id and after.channel.id == hub_id:
            await create_or_move_to_personal_room(member, after.channel)

# =========================
# COMMANDS (OWNER / ADMIN)
# =========================

@bot.command()
async def ping(ctx):
    await ctx.send("🏓 Pong!")

@bot.command()
async def xp(ctx, member: discord.Member | None = None):
    member = member or ctx.author
    xp_val = await get_user_xp(ctx.guild.id, member.id)
    lvl = level_from_xp(xp_val)
    voice_sec = await get_user_voice_seconds(ctx.guild.id, member.id)
    await ctx.reply(f"📈 {member.mention}: XP = **{xp_val}**, уровень = **{lvl}**, войс = **{voice_sec//60} мин**")

@bot.command()
async def rank(ctx, member: discord.Member | None = None):
    member = member or ctx.author
    xp_val = await get_user_xp(ctx.guild.id, member.id)
    lvl = level_from_xp(xp_val)
    role_id = best_level_role_id(lvl)
    role = ctx.guild.get_role(role_id) if role_id else None
    await ctx.reply(f"🏅 {member.mention}: уровень **{lvl}** | роль: **{role.name if role else 'нет'}**")

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
        return await ctx.reply("❌ Не смог найти сообщение по ROLE_MESSAGE_ID в этом канале. Запусти команду в том же канале.")

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
    await ctx.reply(f"✅ AUTO_VOICE_HUB_ID установлен: `{channel_id}`\nТеперь при заходе в этот войс будет создаваться личная комната.")

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
                await member.add_roles(role, reason="Mass role give")
                count += 1
                await asyncio.sleep(0.3)
            except discord.Forbidden:
                pass

    await ctx.send(f"✅ Готово. Роль выдана **{count}** участникам.")

# =========================
# RUN
# =========================

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN не задан. Добавь DISCORD_TOKEN в Railway Variables.")
if not DATABASE_URL:
    print("⚠ DATABASE_URL не задан. Таблицы/XP не будут сохраняться в БД.")

bot.run(TOKEN)
