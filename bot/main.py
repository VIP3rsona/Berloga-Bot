import os
import random
import time
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

# =========================
# CONFIG (настрой здесь)
# =========================

# Сообщение, на которое ставят реакции (можно не заполнять руками, см. команду !setrolemsg)
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

# Уровни/роли (выдаём ТОЛЬКО самую высокую подходящую роль)
LEVEL_ROLE_LADDER = {
    1: 1477952593618534400,    # 🐣 Новичок
    5: 1477952664900997191,    # 🐟 Рыбак
    10: 1477952755644629177,   # 🐠 Профи
    20: 1477952840650592497,   # 🦈 Трофейщик
    30: 1477952930932985997,   # 👑 Легенда
}

# XP настройки (сложная прокачка)
XP_MIN = 8
XP_MAX = 15
XP_COOLDOWN = 90  # секунд между начислениями XP одному юзеру
MIN_MSG_LEN = 8   # короткие сообщения не считаем

# VOICE XP (очень мало + анти-фарм)
VOICE_XP_MIN = 2
VOICE_XP_MAX = 4
VOICE_TICK_SEC = 180        # 3 минуты
VOICE_MIN_MEMBERS = 2       # минимум людей в войсе
VOICE_IGNORE_DEAFENED = True

# =========================
# INIT
# =========================

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)

# XP хранилище (в памяти)
USER_XP: dict[tuple[int, int], int] = {}       # (guild_id, user_id) -> xp
LAST_XP_TS: dict[tuple[int, int], float] = {}  # (guild_id, user_id) -> timestamp

# Кто сейчас "учитывается" в войсе (помогает фильтровать deaf/выходы)
IN_VOICE: dict[tuple[int, int], bool] = {}     # (guild_id, user_id) -> True


# =========================
# HELPERS
# =========================

def level_from_xp(xp: int) -> int:
    """
    Сложная прокачка: уровень растёт медленно.
    Формула примерно: lvl = floor(sqrt(xp/600)) + 1
    """
    if xp <= 0:
        return 1
    return max(1, int((xp / 600) ** 0.5) + 1)


def best_level_role_id(level: int) -> int | None:
    """Возвращает role_id самой высокой роли, подходящей по уровню."""
    best = None
    for lvl_req, role_id in LEVEL_ROLE_LADDER.items():
        if level >= lvl_req:
            if best is None or lvl_req > best[0]:
                best = (lvl_req, role_id)
    return best[1] if best else None


async def apply_level_role(member: discord.Member, level: int):
    """Оставляем у пользователя только 1 ранговую роль (самую высокую)."""
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
        print("Нет прав управлять ролями (Forbidden). Проверь роль бота и её позицию.")
    except Exception as e:
        print("Ошибка выдачи ранговой роли:", e)


async def add_reaction_role(guild_id: int, user_id: int, emoji: str):
    role_id = REACTION_ROLE_MAP.get(emoji)
    if not role_id:
        return

    guild = bot.get_guild(guild_id)
    if not guild:
        return

    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            return

    role = guild.get_role(role_id)
    if not role:
        return

    try:
        if role not in member.roles:
            await member.add_roles(role, reason="Reaction role")
    except discord.Forbidden:
        print("Нет прав выдавать роли (Forbidden). Проверь Manage Roles и позицию роли бота.")
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
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            return

    role = guild.get_role(role_id)
    if not role:
        return

    try:
        if role in member.roles:
            await member.remove_roles(role, reason="Reaction role removed")
    except discord.Forbidden:
        print("Нет прав снимать роли (Forbidden). Проверь Manage Roles и позицию роли бота.")
    except Exception as e:
        print("Ошибка снятия reaction роли:", e)


def member_voice_eligible(member: discord.Member) -> bool:
    vs = member.voice
    if not vs or not vs.channel:
        return False
    if VOICE_IGNORE_DEAFENED and (vs.self_deaf or vs.deaf):
        return False
    return True


def eligible_members_in_channel(channel: discord.VoiceChannel | discord.StageChannel) -> list[discord.Member]:
    members: list[discord.Member] = []
    for m in channel.members:
        if isinstance(m, discord.Member) and (not m.bot) and member_voice_eligible(m):
            members.append(m)
    return members


# =========================
# VOICE XP LOOP
# =========================

@tasks.loop(seconds=VOICE_TICK_SEC)
async def voice_xp_tick():
    for guild in bot.guilds:
        for channel in guild.voice_channels:
            eligible = eligible_members_in_channel(channel)

            # анти-фарм: минимум людей
            if len(eligible) < VOICE_MIN_MEMBERS:
                continue

            for member in eligible:
                key = (guild.id, member.id)
                if not IN_VOICE.get(key):
                    continue

                gained = random.randint(VOICE_XP_MIN, VOICE_XP_MAX)

                old_xp = USER_XP.get(key, 0)
                new_xp = old_xp + gained
                USER_XP[key] = new_xp

                old_lvl = level_from_xp(old_xp)
                new_lvl = level_from_xp(new_xp)

                if new_lvl > old_lvl:
                    await apply_level_role(member, new_lvl)


# =========================
# EVENTS
# =========================

@bot.event
async def on_ready():
    print(f"🐻 Berloga Bot запущен как {bot.user}")
    if not voice_xp_tick.is_running():
        voice_xp_tick.start()


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
        print("Не удалось отправить ЛС пользователю")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if not member.guild:
        return

    key = (member.guild.id, member.id)

    if after.channel is None:
        IN_VOICE.pop(key, None)
        return

    # в войсе, но если deaf — не учитываем
    if VOICE_IGNORE_DEAFENED and (after.self_deaf or after.deaf):
        IN_VOICE.pop(key, None)
        return

    IN_VOICE[key] = True


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    if ROLE_MESSAGE_ID and payload.message_id != ROLE_MESSAGE_ID:
        return
    if payload.guild_id is None:
        return

    emoji = str(payload.emoji)
    if emoji not in REACTION_ROLE_MAP:
        return

    await add_reaction_role(payload.guild_id, payload.user_id, emoji)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if ROLE_MESSAGE_ID and payload.message_id != ROLE_MESSAGE_ID:
        return
    if payload.guild_id is None:
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

    key = (message.guild.id, message.author.id)
    now = time.time()

    if now - LAST_XP_TS.get(key, 0) < XP_COOLDOWN:
        return

    content = (message.content or "").strip()
    if len(content) < MIN_MSG_LEN:
        return

    LAST_XP_TS[key] = now
    gained = random.randint(XP_MIN, XP_MAX)

    old_xp = USER_XP.get(key, 0)
    new_xp = old_xp + gained
    USER_XP[key] = new_xp

    old_lvl = level_from_xp(old_xp)
    new_lvl = level_from_xp(new_xp)

    if new_lvl > old_lvl:
        try:
            await apply_level_role(message.author, new_lvl)
        except Exception as e:
            print("Ошибка при апдейте ранговой роли:", e)


# =========================
# ADMIN COMMANDS
# =========================

def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator


@bot.command()
async def setrolemsg(ctx, message_id: int):
    if not is_admin(ctx.author):
        return await ctx.reply("⛔ Только админ может это делать.")

    global ROLE_MESSAGE_ID
    ROLE_MESSAGE_ID = int(message_id)

    await ctx.reply(
        f"✅ ROLE_MESSAGE_ID установлен: `{ROLE_MESSAGE_ID}`\n"
        f"Теперь реакции на это сообщение будут выдавать роли."
    )


@bot.command()
async def syncroles(ctx):
    if not is_admin(ctx.author):
        return await ctx.reply("⛔ Только админ может это делать.")
    if not ROLE_MESSAGE_ID:
        return await ctx.reply("⚠ Сначала задай ROLE_MESSAGE_ID командой `!setrolemsg <id>`.")

    try:
        msg = await ctx.channel.fetch_message(ROLE_MESSAGE_ID)
    except Exception:
        return await ctx.reply(
            "❌ Не смог найти сообщение по ROLE_MESSAGE_ID в этом канале. "
            "Убедись, что ты запускаешь команду в том же канале."
        )

    for emoji in REACTION_ROLE_MAP.keys():
        try:
            await msg.add_reaction(emoji)
        except Exception:
            pass

    await ctx.reply("✅ Реакции добавлены на сообщение.")


@bot.command()
async def xp(ctx, member: discord.Member | None = None):
    member = member or ctx.author
    key = (ctx.guild.id, member.id)
    xp_val = USER_XP.get(key, 0)
    lvl = level_from_xp(xp_val)
    await ctx.reply(f"📈 {member.mention}: XP = **{xp_val}**, уровень = **{lvl}**")


@bot.command()
async def rank(ctx, member: discord.Member | None = None):
    member = member or ctx.author
    key = (ctx.guild.id, member.id)
    xp_val = USER_XP.get(key, 0)
    lvl = level_from_xp(xp_val)
    role_id = best_level_role_id(lvl)
    role = ctx.guild.get_role(role_id) if role_id else None
    await ctx.reply(f"🏅 {member.mention}: уровень **{lvl}** | роль: **{role.name if role else 'нет'}**")


# =========================
# RUN
# =========================

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN не задан. Добавь переменную окружения DISCORD_TOKEN в Railway.")

bot.run(TOKEN)
