import asyncpg
import os

DATABASE_URL = os.getenv("DATABASE_URL")

pool = None


async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)


async def add_xp(guild_id, user_id, xp):
    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO user_stats (guild_id, user_id, xp)
        VALUES ($1,$2,$3)
        ON CONFLICT (guild_id,user_id)
        DO UPDATE SET xp = user_stats.xp + $3
        """, guild_id, user_id, xp)


async def get_top_xp(guild_id):
    async with pool.acquire() as conn:
        return await conn.fetch("""
        SELECT user_id, xp
        FROM user_stats
        WHERE guild_id = $1
        ORDER BY xp DESC
        LIMIT 10
        """, guild_id)
