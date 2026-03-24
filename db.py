import asyncpg
import os
from config import DATABASE_URL

_pool = None


async def get_pool():
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. Add it to .env (Supabase → Settings → Database)."
            )
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            ssl="require",
            statement_cache_size=0,
            min_size=1,
            max_size=10,
            max_inactive_connection_lifetime=300,
        )
    return _pool


async def get_conn():
    pool = await get_pool()
    return await pool.acquire()


async def release_conn(conn):
    pool = await get_pool()
    await pool.release(conn)


async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                name TEXT,
                resume TEXT,
                preferences JSONB DEFAULT '{}',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS resume_url TEXT")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id SERIAL PRIMARY KEY,
                title TEXT,
                company TEXT,
                location TEXT,
                url TEXT UNIQUE,
                source TEXT,
                description TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                job_id INTEGER REFERENCES jobs(id),
                score INTEGER DEFAULT NULL,
                status TEXT DEFAULT 'new',
                applied_at TIMESTAMP,
                notes TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, job_id)
            )
        """)
        print("  Database initialized.")


async def insert_job(job: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO jobs (title, company, location, url, source, description)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (url) DO NOTHING
            RETURNING id
            """,
            job["title"],
            job["company"],
            job["location"],
            job["url"],
            job["source"],
            job.get("description", ""),
        )
        return row["id"] if row else None


async def insert_jobs_batch(jobs: list[dict]):
    if not jobs:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO jobs (title, company, location, url, source, description)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (url) DO NOTHING
            """,
            [
                (
                    j["title"],
                    j["company"],
                    j["location"],
                    j["url"],
                    j["source"],
                    j.get("description", ""),
                )
                for j in jobs
            ],
        )


async def get_unscored_jobs(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT j.* FROM jobs j
            LEFT JOIN applications a ON a.job_id = j.id AND a.user_id = $1
            WHERE a.id IS NULL OR a.score IS NULL
            """,
            user_id,
        )


async def upsert_application(user_id: int, job_id: int, score: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO applications (user_id, job_id, score)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, job_id) DO UPDATE SET score = EXCLUDED.score
            """,
            user_id,
            job_id,
            score,
        )


async def update_application_status(user_id: int, job_id: int, status: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO applications (user_id, job_id, status)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, job_id) DO UPDATE SET status = $3, applied_at = NOW()
            """,
            user_id,
            job_id,
            status,
        )


async def get_top_jobs(user_id: int, min_score: int = 6, limit: int = 50):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT j.*, a.score, a.status
            FROM jobs j
            JOIN applications a ON a.job_id = j.id
            WHERE a.user_id = $1 AND a.score >= $2
            ORDER BY a.score DESC
            LIMIT $3
            """,
            user_id,
            min_score,
            limit,
        )


async def get_or_create_user(email: str, name: str = "") -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (email, name)
            VALUES ($1, $2)
            ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
            """,
            email,
            name,
        )
        return row["id"]


async def get_user_prefs(user_id: int) -> dict:
    import json
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT preferences FROM users WHERE id = $1", user_id
        )
        if row and row["preferences"]:
            prefs = row["preferences"]
            if isinstance(prefs, str):
                prefs = json.loads(prefs)
            return prefs
        return {}


async def get_user_id_by_email(email: str) -> int | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM users WHERE email = $1", email)
        return row["id"] if row else None
