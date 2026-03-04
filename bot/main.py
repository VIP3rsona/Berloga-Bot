import os
import time
import random
import asyncio
from typing import Optional

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import asyncpg

# =========================
# ENV + INIT
# =========================

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")  # в Railway: DATABASE_URL = ${{ Postgres.DATABASE_URL }}

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN не задан. Добавь переменную окружения DISCORD_TOKEN в Railway.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не задан. В Railway добавь DATABASE_URL = ${{ Postgres.DATABASE_URL }}")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)
db: Optional[asyncpg.Connection] = None

# =========================
# CONFIG (настрой здесь)
# =========================

# Сообщение для реакций (можно не заполнять руками — команда !setrolemsg)
ROLE_MESSAGE_ID = int(os.getenv("ROLE_MESSAGE_ID", "0"))

# emoji -> role_id
REACTION_ROLE_MAP = {
    "🎯": 1477939798600712222,  # Спиннинг
    "🐟": 1477940426085367808,  # Донка
    "🪱": 1477940549674602626,  # Поплавок
    "🧊": 1477940631119859773,  # Фидер
    "🪝": 1477940689244520611,  # Кастинг
    "🐠": 1477940771918315701,  # Универсал
}

# Ранги: выдаём ТОЛЬКО самый высокий подходящий
LEVEL_ROLE_LADDER = {
    1: 1477952593618534400,    # 🐣 Новичок
    5: 1477952664900997191,    # 🐟 Рыбак
    10: 1477952755644629177,   # 🐠 Профи
    20: 1477952840650592497,   # 🦈 Трофейщик
    30: 1477952930932985997,   # 👑 Легенда
}

# XP за сообщения
MSG_XP_MIN = 8
MSG_XP_MAX = 15
MSG_XP_COOLDOWN = 90  # сек между начислениями XP одному юзеру
MIN_MSG_LEN = 8       # короткие сообщения не считаем

# XP за войс (оч мало, как ты хотел)
VOICE_TICK_SECONDS = 60     # раз в 60 секунд проверяем войс
VOICE_XP_PER_TICK = 1       # +1 XP за тик
VOICE_MIN_MEMBERS = 1       # учитывать одиночку? да (для теста). Потом поставишь 2.

# Автовойс: канал-вход (установишь командой !setautovoice)
AUTO_VOICE_HUB_ID = int(os.getenv("AUTO_VOICE_HUB_ID", "0"))

# =========================
# DB HELPERS
# =========================

async def db_exec(query: str, *args):
    global db
    if db is None:
        return
    try:
        return await db.execute(query, *args)
    except Exception as e:
        print("DB execute error:", e)

async def db_fetch(query: str, *args):
    global db
    if db is None:
        return None
    try:
        return await db.fetch(query, *args)
    except Exception as e:
        print("DB fetch error:", e)
        return None

async def db_fetchrow(query: str, *args):
    global db
    if db is None:
        return None
    try:
        return await db.fetchrow(query, *args)
    except Exception as e:
        print("DB fetchrow error:", e)
        return None

# =========================
# XP / LEVEL HELPERS
# =========================

def level_from_xp(xp: int) -> int:
    """
    Сложная прокачка: медленно.
    lvl = floor(sqrt(xp/600)) + 1
    """
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

    if target_role_id in member_role_ids and len(member_role_ids.intersection(ladder_role_ids)) == 1:
        return

    to_remove = [r for r in member.roles if r.id in ladder_role_ids and r.id != target_role_id]
    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason="Level rank cleanup")
        if target_role not in member.roles:
            await member.add_roles(target_role, reason="Level reward")
    except discord.Forbidden:
        print("Forbidden: bot role must be выше выдаваемых ролей + Manage Roles.")
    except Exception as e:
        print("apply_level_role error:", e)

# =========================
# REACTION ROLE HELPERS
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
        print("Forbidden: нет прав выдавать роли (Manage Roles/позиция ролей).")
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
        print("Forbidden: нет прав снимать роли.")
    except Exception as e:
        print("remove_reaction_role error:", e)

# =========================
# DB SCHEMA + STARTUP
# =========================

async def ensure_schema():
    # таблица с XP/активностью
    await db_exec("""
    CREATE TABLE IF NOT EXISTS user_stats (
        guild_id BIGINT NOT NULL,
        user_id  BIGINT NOT NULL,
        xp BIGINT NOT NULL DEFAULT 0,
        message_count BIGINT NOT NULL DEFAULT 0,
        voice_seconds BIGINT NOT NULL DEFAULT 0,
        last_msg_xp_ts BIGINT NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (guild_id, user_id)
    );
    """)

    # настройки сервера
    await db_exec("""
    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id BIGINT PRIMARY KEY,
        role_message_id BIGINT NOT NULL DEFAULT 0,
        auto_voice_hub_id BIGINT NOT NULL DEFAULT 0
    );
    """)

async def load_guild_settings_into_memory():
    global ROLE_MESSAGE_ID, AUTO_VOICE_HUB_ID

    rows = await db_fetch("SELECT guild_id, role_message_id, auto_voice_hub_id FROM guild_settings;")
    if not rows:
        return

    # В этом боте предполагаем 1 сервер, но на всякий оставим "последнее"
    for r in rows:
        if r["role_message_id"]:
            ROLE_MESSAGE_ID = int(r["role_message_id"])
        if r["auto_voice_hub_id"]:
            AUTO_VOICE_HUB_ID = int(r["auto_voice_hub_id"])

# =========================
# EVENTS
# =========================

@bot.event
async def on_ready():
    global db
    print(f"🐻 Berloga Bot запущен как {bot.user}")

    if db is None:
        db = await asyncpg.connect(DATABASE_URL)
        print("✅ Database connected")

        await ensure_schema()
        await load_guild_settings_into_memory()
        print(f"✅ Settings loaded: ROLE_MESSAGE_ID={ROLE_MESSAGE_ID}, AUTO_VOICE_HUB_ID={AUTO_VOICE_HUB_ID}")

    if not voice_xp_tick.is_running():
        voice_xp_tick.start()

    if not auto_voice_cleanup.is_running():
        auto_voice_cleanup.start()

@bot.command()
async def ping(ctx):
    await ctx.send("🏓 Pong!")

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
        print("Не удалось отправить ЛС пользователю (возможно закрыты DM).")

# =========================
# REACTION ROLES (RAW)
# =========================

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    if payload.guild_id is None:
        return

    if ROLE_MESSAGE_ID and payload.message_id != ROLE_MESSAGE_ID:
        return

    emoji = str(payload.emoji)
    if emoji not in REACTION_ROLE_MAP:
        return

    await add_reaction_role(payload.guild_id, payload.user_id, emoji)

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None:
        return

    if ROLE_MESSAGE_ID and payload.message_id != ROLE_MESSAGE_ID:
        return

    emoji = str(payload.emoji)
    if emoji not in REACTION_ROLE_MAP:
        return

    await remove_reaction_role(payload.guild_id, payload.user_id, emoji)

# =========================
# XP: MESSAGES
# =========================

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    await bot.process_commands(message)

    content = (message.content or "").strip()
    if len(content) < MIN_MSG_LEN:
        return

    guild_id = message.guild.id
    user_id = message.author.id

    # достаем last_msg_xp_ts
    row = await db_fetchrow(
        "SELECT xp, last_msg_xp_ts FROM user_stats WHERE guild_id=$1 AND user_id=$2;",
        guild_id, user_id
    )

    now_ts = int(time.time())
    last_ts = int(row["last_msg_xp_ts"]) if row else 0
    if now_ts - last_ts < MSG_XP_COOLDOWN:
        # но сообщения считаем для статистики
        await db_exec("""
        INSERT INTO user_stats (guild_id, user_id, message_count)
        VALUES ($1,$2,1)
        ON CONFLICT (guild_id, user_id)
        DO UPDATE SET message_count = user_stats.message_count + 1, updated_at=NOW();
        """, guild_id, user_id)
        return

    gained = random.randint(MSG_XP_MIN, MSG_XP_MAX)

    # обновляем xp + message_count + last_msg_xp_ts
    await db_exec("""
    INSERT INTO user_stats (guild_id, user_id, xp, message_count, last_msg_xp_ts)
    VALUES ($1,$2,$3,1,$4)
    ON CONFLICT (guild_id, user_id)
    DO UPDATE SET
      xp = user_stats.xp + $3,
      message_count = user_stats.message_count + 1,
      last_msg_xp_ts = $4,
      updated_at = NOW();
    """, guild_id, user_id, gained, now_ts)

    # проверяем ап уровня
    new_row = await db_fetchrow("SELECT xp FROM user_stats WHERE guild_id=$1 AND user_id=$2;", guild_id, user_id)
    if not new_row:
        return

    new_xp = int(new_row["xp"])
    new_lvl = level_from_xp(new_xp)

    # применяем ранговую роль
    try:
        await apply_level_role(message.author, new_lvl)
    except Exception as e:
        print("Ошибка ранговой роли:", e)

# =========================
# XP: VOICE (TICK)
# =========================

@tasks.loop(seconds=VOICE_TICK_SECONDS)
async def voice_xp_tick():
    # по всем серверам, где бот есть
    for guild in bot.guilds:
        for member in guild.members:
            if member.bot:
                continue
            if not member.voice or not member.voice.channel:
                continue

            # если хочешь учитывать только людей, которые не одни — поставь VOICE_MIN_MEMBERS=2
            channel = member.voice.channel
            if len(channel.members) < VOICE_MIN_MEMBERS:
                continue

            # +voice seconds
            await db_exec("""
            INSERT INTO user_stats (guild_id, user_id, voice_seconds, xp)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET
              voice_seconds = user_stats.voice_seconds + $3,
              xp = user_stats.xp + $4,
              updated_at = NOW();
            """, guild.id, member.id, VOICE_TICK_SECONDS, VOICE_XP_PER_TICK)

            # ранговая роль по новому xp
            row = await db_fetchrow("SELECT xp FROM user_stats WHERE guild_id=$1 AND user_id=$2;", guild.id, member.id)
            if row:
                lvl = level_from_xp(int(row["xp"]))
                await apply_level_role(member, lvl)

# =========================
# AUTO VOICE: CREATE + MOVE + CLEANUP
# =========================

async def is_hub_channel(channel: discord.VoiceChannel) -> bool:
    return AUTO_VOICE_HUB_ID != 0 and channel.id == AUTO_VOICE_HUB_ID

def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # Заход в HUB: создаём новый войс и переносим
    if after.channel and isinstance(after.channel, discord.VoiceChannel):
        if await is_hub_channel(after.channel):
            guild = member.guild

            # создаём новый канал рядом с HUB по категории
            category = after.channel.category
            base_name = "🎙 Комната"
            new_name = f"{base_name} — {member.display_name}"

            try:
                new_channel = await guild.create_voice_channel(
                    name=new_name,
                    category=category,
                    reason="Auto voice create"
                )
                await member.move_to(new_channel, reason="Auto voice move")
            except discord.Forbidden:
                print("Forbidden: нужны права Manage Channels + Move Members.")
            except Exception as e:
                print("Auto voice create/move error:", e)

@tasks.loop(minutes=2)
async def auto_voice_cleanup():
    # Удаляем пустые авто-каналы, которые были созданы ботом
    for guild in bot.guilds:
        for ch in guild.voice_channels:
            # не трогаем HUB
            if AUTO_VOICE_HUB_ID and ch.id == AUTO_VOICE_HUB_ID:
                continue

            # удаляем только каналы, созданные ботом по шаблону
            if ch.name.startswith("🎙 Комната") and len(ch.members) == 0:
                try:
                    await ch.delete(reason="Auto voice cleanup (empty)")
                except Exception:
                    pass

# =========================
# COMMANDS: SETTINGS
# =========================

@bot.command()
async def setrolemsg(ctx, message_id: int):
    if not is_admin(ctx.author):
        return await ctx.reply("⛔ Только админ может это делать.")

    global ROLE_MESSAGE_ID
    ROLE_MESSAGE_ID = int(message_id)

    await db_exec("""
    INSERT INTO guild_settings (guild_id, role_message_id)
    VALUES ($1,$2)
    ON CONFLICT (guild_id)
    DO UPDATE SET role_message_id=$2;
    """, ctx.guild.id, ROLE_MESSAGE_ID)

    await ctx.reply(f"✅ ROLE_MESSAGE_ID установлен: `{ROLE_MESSAGE_ID}`")

@bot.command()
async def syncroles(ctx):
    if not is_admin(ctx.author):
        return await ctx.reply("⛔ Только админ может это делать.")
    if not ROLE_MESSAGE_ID:
        return await ctx.reply("⚠ Сначала `!setrolemsg <id>`")

    try:
        msg = await ctx.channel.fetch_message(ROLE_MESSAGE_ID)
    except Exception:
        return await ctx.reply("❌ Не нашёл сообщение в этом канале. Запусти команду в том же канале где сообщение.")

    for emoji in REACTION_ROLE_MAP.keys():
        try:
            await msg.add_reaction(emoji)
        except Exception:
            pass

    await ctx.reply("✅ Реакции добавлены на сообщение.")

@bot.command()
async def setautovoice(ctx, hub_channel_id: int):
    """Задаёт Voice HUB канал: заходишь в него -> создаётся новая комната и переносит."""
    if not is_admin(ctx.author):
        return await ctx.reply("⛔ Только админ может это делать.")

    global AUTO_VOICE_HUB_ID
    AUTO_VOICE_HUB_ID = int(hub_channel_id)

    await db_exec("""
    INSERT INTO guild_settings (guild_id, auto_voice_hub_id)
    VALUES ($1,$2)
    ON CONFLICT (guild_id)
    DO UPDATE SET auto_voice_hub_id=$2;
    """, ctx.guild.id, AUTO_VOICE_HUB_ID)

    await ctx.reply(f"✅ AUTO_VOICE_HUB_ID установлен: `{AUTO_VOICE_HUB_ID}`")

# =========================
# COMMANDS: STATS / LEADERBOARDS
# =========================

@bot.command()
async def xp(ctx, member: Optional[discord.Member] = None):
    member = member or ctx.author
    row = await db_fetchrow(
        "SELECT xp, message_count, voice_seconds FROM user_stats WHERE guild_id=$1 AND user_id=$2;",
        ctx.guild.id, member.id
    )
    if not row:
        return await ctx.reply(f"📈 {member.mention}: пока нет статистики.")
    xp_val = int(row["xp"])
    lvl = level_from_xp(xp_val)
    voice_sec = int(row["voice_seconds"])
    mins = voice_sec // 60
    await ctx.reply(f"📈 {member.mention}: XP=**{xp_val}**, lvl=**{lvl}**, msgs=**{row['message_count']}**, voice=**{mins} мин**")

@bot.command()
async def topxp(ctx, limit: int = 10):
    limit = max(1, min(limit, 25))
    rows = await db_fetch(
        "SELECT user_id, xp FROM user_stats WHERE guild_id=$1 ORDER BY xp DESC LIMIT $2;",
        ctx.guild.id, limit
    )
    if not rows:
        return await ctx.reply("Пока пусто.")
    lines = []
    for i, r in enumerate(rows, start=1):
        user = ctx.guild.get_member(int(r["user_id"]))
        name = user.display_name if user else f"<@{r['user_id']}>"
        lines.append(f"**{i}.** {name} — **{int(r['xp'])} XP**")
    await ctx.reply("🏆 **TOP XP**\n" + "\n".join(lines))

@bot.command()
async def topvoice(ctx, limit: int = 10):
    limit = max(1, min(limit, 25))
    rows = await db_fetch(
        "SELECT user_id, voice_seconds FROM user_stats WHERE guild_id=$1 ORDER BY voice_seconds DESC LIMIT $2;",
        ctx.guild.id, limit
    )
    if not rows:
        return await ctx.reply("Пока пусто.")
    lines = []
    for i, r in enumerate(rows, start=1):
        user = ctx.guild.get_member(int(r["user_id"]))
        name = user.display_name if user else f"<@{r['user_id']}>"
        mins = int(r["voice_seconds"]) // 60
        lines.append(f"**{i}.** {name} — **{mins} мин**")
    await ctx.reply("🎙 **TOP VOICE**\n" + "\n".join(lines))

@bot.command()
async def topmsg(ctx, limit: int = 10):
    limit = max(1, min(limit, 25))
    rows = await db_fetch(
        "SELECT user_id, message_count FROM user_stats WHERE guild_id=$1 ORDER BY message_count DESC LIMIT $2;",
        ctx.guild.id, limit
    )
    if not rows:
        return await ctx.reply("Пока пусто.")
    lines = []
    for i, r in enumerate(rows, start=1):
        user = ctx.guild.get_member(int(r["user_id"]))
        name = user.display_name if user else f"<@{r['user_id']}>"
        lines.append(f"**{i}.** {name} — **{int(r['message_count'])} сообщений**")
    await ctx.reply("💬 **TOP MESSAGES**\n" + "\n".join(lines))

# =========================
# RUN
# =========================

bot.run(TOKEN)
