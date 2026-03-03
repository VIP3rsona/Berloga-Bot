import os
import random
import time
import discord
from discord.ext import commands
from dotenv import load_dotenv

# =========================
# CONFIG (настрой здесь)
# =========================

# Сообщение, на которое ставят реакции (можно не заполнять руками, см. команду !setrolemsg)
ROLE_MESSAGE_ID = int(os.getenv("ROLE_MESSAGE_ID", "0"))

# emoji -> role_id (ЗАМЕНИ role_id на реальные)
REACTION_ROLE_MAP = {
    "🎯": 111111111111111111,  # Спиннинг
    "🐟": 222222222222222222,  # Донка
    "🪱": 333333333333333333,  # Поплавок
    "🧊": 444444444444444444,  # Фидер
    "🪝": 555555555555555555,  # Кастинг
    "🐠": 666666666666666666,  # Универсал
}

# Уровни/роли (выдаём ТОЛЬКО самую высокую подходящую роль)
# level_required -> role_id (ЗАМЕНИ role_id на реальные)
LEVEL_ROLE_LADDER = {
    1: 777777777777777777,    # 🐣 Новичок
    5: 888888888888888888,    # 🐟 Рыбак
    10: 999999999999999999,   # 🐠 Профи
    20: 101010101010101010,   # 🦈 Трофейщик
    30: 121212121212121212,   # 👑 Легенда
}

# XP настройки (сложная прокачка)
XP_MIN = 8
XP_MAX = 15
XP_COOLDOWN = 90  # секунд между начислениями XP одному юзеру
MIN_MSG_LEN = 8   # короткие сообщения не считаем

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

    # все роли "рангов" (которые мы управляем)
    ladder_role_ids = set(LEVEL_ROLE_LADDER.values())
    member_role_ids = {r.id for r in member.roles}

    # если уже есть целевая роль и нет других ранговых — всё ок
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
    if not member:
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
    if not member:
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


# =========================
# EVENTS
# =========================

@bot.event
async def on_ready():
    print(f"🐻 Berloga Bot запущен как {bot.user}")
    # Можно попытаться "досинхронизировать" реакции (если бот перезапускался)
    # но без message fetch это не критично.


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
        # у человека могут быть закрыты личные сообщения
        print("Не удалось отправить ЛС пользователю")


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

    # сначала команды
    await bot.process_commands(message)

    # XP начисляем только в гильдии
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
        # выдаём/обновляем ранговую роль
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
    """Задаёт ID сообщения для reaction-ролей."""
    if not is_admin(ctx.author):
        return await ctx.reply("⛔ Только админ может это делать.")

    global ROLE_MESSAGE_ID
    ROLE_MESSAGE_ID = int(message_id)

    await ctx.reply(f"✅ ROLE_MESSAGE_ID установлен: `{ROLE_MESSAGE_ID}`\n"
                    f"Теперь реакции на это сообщение будут выдавать роли.")


@bot.command()
async def syncroles(ctx):
    """
    Добавляет на сообщение все нужные реакции (удобно для панели ролей).
    Использование: !syncroles (в канале, где сообщение)
    """
    if not is_admin(ctx.author):
        return await ctx.reply("⛔ Только админ может это делать.")
    if not ROLE_MESSAGE_ID:
        return await ctx.reply("⚠ Сначала задай ROLE_MESSAGE_ID командой `!setrolemsg <id>`.")

    try:
        msg = await ctx.channel.fetch_message(ROLE_MESSAGE_ID)
    except Exception:
        return await ctx.reply("❌ Не смог найти сообщение по ROLE_MESSAGE_ID в этом канале. "
                               "Убедись, что ты запускаешь команду в том же канале.")

    for emoji in REACTION_ROLE_MAP.keys():
        try:
            await msg.add_reaction(emoji)
        except Exception:
            pass

    await ctx.reply("✅ Реакции добавлены на сообщение.")


@bot.command()
async def xp(ctx, member: discord.Member | None = None):
    """Показывает XP."""
    member = member or ctx.author
    key = (ctx.guild.id, member.id)
    xp_val = USER_XP.get(key, 0)
    lvl = level_from_xp(xp_val)
    await ctx.reply(f"📈 {member.mention}: XP = **{xp_val}**, уровень = **{lvl}**")


@bot.command()
async def rank(ctx, member: discord.Member | None = None):
    """Показывает текущий ранговый уровень/роль."""
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
