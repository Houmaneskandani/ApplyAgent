import asyncpg
import os
from config import DATABASE_URL

_pool = None

# Per-company application cooldown: after ANY live application to a company,
# every other posting at that company is off-limits for this many days.
# Companies rate-limit applicants (SpaceX emailed the user after he hit their
# 30-day cap — the bot had applied to several DIFFERENT SpaceX roles, which
# the company+title dedupe correctly allowed but the company disallowed).
# Env-overridable; per-user prefs key company_cooldown_days wins when set.
try:
    COMPANY_COOLDOWN_DAYS = int(os.getenv("COMPANY_COOLDOWN_DAYS", "30"))
except ValueError:
    COMPANY_COOLDOWN_DAYS = 30


def company_cooldown_days(prefs: dict | None) -> int:
    """Resolve the cooldown for a user: prefs override, else the default."""
    try:
        v = int((prefs or {}).get("company_cooldown_days", COMPANY_COOLDOWN_DAYS))
        return max(0, min(v, 365))
    except (TypeError, ValueError):
        return COMPANY_COOLDOWN_DAYS


async def get_pool():
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. Add it to .env (Supabase → Settings → Database)."
            )
        # DB_POOL_MAX: Supabase's session-mode pooler allows 15 clients TOTAL
        # across the API service, the cron worker, and any ad-hoc scripts.
        # The always-on API keeps the default 10; short-lived workers/scripts
        # should set DB_POOL_MAX=2 so they can't starve the API (EMAXCONNSESSION).
        try:
            _max = int(os.getenv("DB_POOL_MAX", "10"))
        except ValueError:
            _max = 10
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            ssl="require",
            statement_cache_size=0,
            min_size=1,
            max_size=max(1, _max),
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
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token TEXT")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token_expires TIMESTAMP")
        # Wrong-code counter for the 6-digit reset code: the token is
        # invalidated after MAX_RESET_ATTEMPTS so the ~1M code space can't be
        # brute-forced by rotating source IPs around the per-IP rate limit.
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_attempts INTEGER DEFAULT 0")
        # Set on every password change; JWTs issued (iat) before it are
        # rejected, so a reset actually logs out whoever held the old token.
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMP")
        # Default free credits for new signups. Kept modest (25 ≈ 60 applies)
        # to limit throwaway-email farming until email verification lands.
        # Existing users keep whatever balance they already have.
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS credits FLOAT DEFAULT 25")
        await conn.execute("ALTER TABLE users ALTER COLUMN credits SET DEFAULT 25")
        # ZipRecruiter 1-Click Apply submits through the user's logged-in ZR
        # account — there's no anonymous form. We store the captured browser
        # session (Playwright storage_state + the UA it was issued to) here,
        # Fernet-encrypted, mirroring the imap_pass-at-rest pattern. NULL until
        # the user runs the one-time login-capture flow.
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ziprecruiter_session TEXT")
        # Stripe webhook idempotency: one row per processed event id. The
        # webhook inserts here (ON CONFLICT DO NOTHING) before crediting, so a
        # retried/duplicate delivery can't grant credits twice.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS stripe_events (
                event_id TEXT PRIMARY KEY,
                processed_at TIMESTAMP DEFAULT NOW()
            )
        """)
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
        # Which job_categories key DISCOVERED this job (e.g.
        # 'warehouse_logistics'). NULL = professional/uncategorized (all
        # historical rows). Lets the dashboard's Professional/Warehouse mode
        # toggle and the rule-based scorer separate commodity local jobs from
        # career jobs without guessing from titles.
        await conn.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS category TEXT")
        # Response Inbox — recruiter replies / interview invites / assessments
        # found in the user's Gmail (scanned by response_scanner.py).
        # UNIQUE(user_id, message_id) makes rescans idempotent.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS responses (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                job_id INTEGER REFERENCES jobs(id),
                message_id TEXT,
                sender TEXT,
                subject TEXT,
                snippet TEXT,
                kind TEXT,
                received_at TIMESTAMP,
                seen BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE (user_id, message_id)
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
        # Schema evolution for `applications` runs AFTER the CREATE above so a
        # brand-new database (local dev, a fresh environment, integration
        # tests) initializes cleanly — it used to ALTER before CREATE and
        # fail with "relation applications does not exist".
        await conn.execute("ALTER TABLE applications ADD COLUMN IF NOT EXISTS queue_position INTEGER DEFAULT 0")
        await conn.execute("ALTER TABLE applications ADD COLUMN IF NOT EXISTS dry_run BOOLEAN DEFAULT TRUE")
        # `force_submit` is a one-shot flag: the /force-submit endpoint sets
        # it to TRUE when the user overrides a reviewer-blocked apply
        # ("Submit anyway"). run_application reads-and-clears it atomically
        # at the start of each attempt so it can't accidentally persist
        # across normal retries.
        await conn.execute("ALTER TABLE applications ADD COLUMN IF NOT EXISTS force_submit BOOLEAN DEFAULT FALSE")
        # `credit_reserved` is TRUE while a live apply holds the upfront 0.4
        # credit reservation. Every refund path flips it FALSE atomically
        # (refund_reservation) so a reservation can be refunded AT MOST ONCE,
        # no matter how many safety nets (timeout handler, zombie sweep,
        # error handler) observe the same failed row.
        await conn.execute("ALTER TABLE applications ADD COLUMN IF NOT EXISTS credit_reserved BOOLEAN DEFAULT FALSE")
        # NOTE: this used to blanket-reset every 'applying' row to 'failed'
        # ("Server restarted during apply") — WITHOUT refunding the reserved
        # credit, and it ran from the cron worker too (every 6h), which marked
        # the web service's genuinely in-flight applies as failed. Stuck rows
        # are now handled by fail_stuck_applications() (refund-aware, age
        # gated) from the API boot + the periodic zombie sweep instead.
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
            INSERT INTO jobs (title, company, location, url, source, description, category)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (url) DO UPDATE
              SET description = EXCLUDED.description
              WHERE (jobs.description IS NULL OR jobs.description = '')
                AND EXCLUDED.description != ''
            """,
            [
                (
                    j["title"],
                    j["company"],
                    j["location"],
                    j["url"],
                    j["source"],
                    j.get("description", ""),
                    j.get("category"),
                )
                for j in jobs
            ],
        )


async def get_unscored_jobs(user_id: int, rescore: bool = False, limit: int = None):
    # Score the FRESHEST jobs first. Previously there was no ORDER BY, so with
    # a backlog of unscored rows the DB returned an arbitrary (effectively
    # oldest-by-insertion) slice — newly-scraped postings could starve and
    # never reach the scorer. created_at DESC fixes that. Cap is env-tunable
    # (raised from 100 → 250 default) so a multi-category/ZR/JSearch cycle
    # clears its backlog in fewer passes.
    if limit is None:
        try:
            limit = int(os.getenv("MATCHER_SCORE_LIMIT", "250"))
        except ValueError:
            limit = 250
    pool = await get_pool()
    async with pool.acquire() as conn:
        if rescore:
            return await conn.fetch(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT $1", limit
            )
        return await conn.fetch(
            """
            SELECT j.* FROM jobs j
            LEFT JOIN applications a ON a.job_id = j.id AND a.user_id = $1
            WHERE a.id IS NULL OR a.score IS NULL
            ORDER BY j.created_at DESC
            LIMIT $2
            """,
            user_id,
            limit,
        )


async def prune_stale_jobs(retention_days: int = None) -> int:
    """
    Delete old postings that NObody has scored/queued/applied to.

    Safe by construction: only removes jobs older than retention_days that have
    ZERO application rows referencing them (NOT IN (SELECT job_id ...)) — so no
    user's score, queue entry, or apply history is ever touched. Keeps the jobs
    table lean and stops the scorer from re-considering long-expired postings.
    Returns the number of rows deleted. Env-tunable via JOB_RETENTION_DAYS.
    """
    if retention_days is None:
        try:
            retention_days = int(os.getenv("JOB_RETENTION_DAYS", "45"))
        except ValueError:
            retention_days = 45
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM jobs
             WHERE created_at < NOW() - ($1 || ' days')::interval
               AND NOT EXISTS (
                   SELECT 1 FROM applications a WHERE a.job_id = jobs.id
               )
            """,
            str(retention_days),
        )
    # asyncpg returns a tag like "DELETE 37"; parse the count.
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError, AttributeError):
        return 0


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
        if status == "applied":
            await conn.execute(
                """
                INSERT INTO applications (user_id, job_id, status, applied_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (user_id, job_id) DO UPDATE SET status = $3, applied_at = NOW()
                """,
                user_id,
                job_id,
                status,
            )
        else:
            await conn.execute(
                """
                INSERT INTO applications (user_id, job_id, status)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id, job_id) DO UPDATE SET status = $3
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


async def get_user_credits(user_id: int) -> float:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT credits FROM users WHERE id = $1", user_id)
        return float(row["credits"] or 0) if row else 0.0


async def deduct_credits(user_id: int, amount: float) -> bool:
    """
    Atomically deduct credits. Returns True on success, False if the user
    had insufficient balance.

    SECURITY: this MUST be a single conditional UPDATE. A SELECT-then-UPDATE
    pattern is a textbook TOCTOU race — N concurrent /apply requests for a
    user with just enough credits can all pass the SELECT, all UPDATE, and
    leave the user with a negative balance.

    The `credits >= $1` predicate and the RETURNING clause together give us
    a single atomic check-and-decrement: Postgres takes a row-level lock for
    the UPDATE; the predicate is evaluated against the locked row; rows that
    fail the predicate are not updated and RETURNING is empty.
    """
    if amount <= 0:
        return True  # nothing to deduct
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE users
               SET credits = credits - $1
             WHERE id = $2 AND COALESCE(credits, 0) >= $1
            RETURNING credits
            """,
            amount, user_id,
        )
        return row is not None


async def add_credits(user_id: int, amount: float):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET credits = COALESCE(credits, 0) + $1 WHERE id = $2",
            amount, user_id,
        )


LIVE_APPLY_CREDIT = 0.4


async def reserve_credit(user_id: int, job_id: int, amount: float = LIVE_APPLY_CREDIT) -> bool:
    """
    Reserve `amount` credits for a live apply of `job_id`: atomically deduct
    from the user's balance AND mark the application row `credit_reserved`.
    Returns False (nothing deducted) if the balance is insufficient.

    Both statements run in ONE transaction so a crash between them can't
    leave a deducted credit with no reservation flag (which would make it
    un-refundable).
    """
    if amount <= 0:
        return True
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE users
                   SET credits = credits - $1
                 WHERE id = $2 AND COALESCE(credits, 0) >= $1
                RETURNING credits
                """,
                amount, user_id,
            )
            if row is None:
                return False
            # Upsert so a caller without a pre-existing row (e.g. a one-off
            # script) still gets a refundable reservation.
            await conn.execute(
                """
                INSERT INTO applications (user_id, job_id, status, credit_reserved)
                VALUES ($1, $2, 'applying', TRUE)
                ON CONFLICT (user_id, job_id) DO UPDATE SET credit_reserved = TRUE
                """,
                user_id, job_id,
            )
            return True


async def refund_reservation(user_id: int, job_id: int, amount: float = LIVE_APPLY_CREDIT) -> bool:
    """
    Refund the reserved credit for (user_id, job_id) EXACTLY ONCE.

    The conditional UPDATE on `credit_reserved = TRUE` is the lock: only the
    first caller flips the flag and gets a RETURNING row, so concurrent
    safety nets (apply error handler, timeout/cancel handler, zombie sweep,
    GET /queue sweep) can never double-refund. Returns True if a refund was
    actually issued.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            claimed = await conn.fetchrow(
                """
                UPDATE applications SET credit_reserved = FALSE
                 WHERE user_id = $1 AND job_id = $2 AND credit_reserved = TRUE
                RETURNING id
                """,
                user_id, job_id,
            )
            if claimed is None:
                return False
            await conn.execute(
                "UPDATE users SET credits = COALESCE(credits, 0) + $1 WHERE id = $2",
                amount, user_id,
            )
            return True


async def consume_reservation(user_id: int, job_id: int) -> None:
    """The apply succeeded: the reservation becomes the charge. Clear the
    flag so no later safety net can refund it."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE applications SET credit_reserved = FALSE WHERE user_id = $1 AND job_id = $2",
            user_id, job_id,
        )


async def fail_stuck_applications(
    user_id: int | None = None,
    older_than_minutes: int = 15,
    note: str = "Timed out — no response after 15 minutes",
) -> dict:
    """
    Mark rows stuck in 'applying' for more than `older_than_minutes` as
    failed AND refund their reserved credit (once — see refund_reservation).

    `applied_at` is set to NOW() when the queue drainer claims a row, so it
    is the apply START time. Scoped to one user when `user_id` is given
    (GET /queue), global otherwise (zombie sweep + API boot).

    The age gate matters on boot: Railway overlaps the old and new instance
    during a deploy, so a row younger than the gate may still be a LIVE apply
    on the outgoing process. Those are picked up by the next periodic sweep.
    Returns {"failed": n, "refunded": m}.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if user_id is None:
            rows = await conn.fetch(
                """
                UPDATE applications
                   SET status = 'failed', notes = $1
                 WHERE status = 'applying'
                   AND applied_at < NOW() - make_interval(mins => $2::int)
                RETURNING user_id, job_id
                """,
                note, older_than_minutes,
            )
        else:
            rows = await conn.fetch(
                """
                UPDATE applications
                   SET status = 'failed', notes = $1
                 WHERE status = 'applying'
                   AND user_id = $3
                   AND applied_at < NOW() - make_interval(mins => $2::int)
                RETURNING user_id, job_id
                """,
                note, older_than_minutes, user_id,
            )
    refunded = 0
    for r in rows:
        try:
            if await refund_reservation(r["user_id"], r["job_id"]):
                refunded += 1
        except Exception as e:
            print(f"[StuckSweep] refund failed user={r['user_id']} job={r['job_id']}: "
                  f"{type(e).__name__}: {e}")
    if rows:
        print(f"[StuckSweep] failed {len(rows)} stuck 'applying' row(s), "
              f"refunded {refunded} credit reservation(s)")
    return {"failed": len(rows), "refunded": refunded}


async def add_to_queue(user_id: int, job_id: int, dry_run: bool) -> int:
    """
    Add job to the application queue. Returns queue position (1-indexed).

    Computes the position inside a transaction so two concurrent POST
    /apply requests can't both observe the same COUNT and both insert
    with the same queue_position.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("""
                SELECT COUNT(*) as cnt FROM applications
                WHERE user_id = $1 AND status IN ('queued', 'applying')
            """, user_id)
            position = (row["cnt"] or 0) + 1

            await conn.execute("""
                INSERT INTO applications (user_id, job_id, status, queue_position, dry_run)
                VALUES ($1, $2, 'queued', $3, $4)
                ON CONFLICT (user_id, job_id) DO UPDATE
                SET status = 'queued', queue_position = $3, dry_run = $4
            """, user_id, job_id, position, dry_run)

            return position
