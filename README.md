# Job Bot

Job discovery and scoring bot. Scrapes Greenhouse, scores jobs with a **keyword matcher** (multi-user ready), and stores data in **PostgreSQL** (Supabase).

## Setup

1. **Create a Supabase project** → Settings → Database → copy the **URI** connection string.

2. **Create and activate virtual environment:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate   # Windows: venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

4. **Configure `.env`:**
   ```env
   DATABASE_URL=postgresql://postgres:[PASSWORD]@db.xxxx.supabase.co:5432/postgres
   USER_EMAIL=you@email.com
   USER_NAME=Your Name
   ```

   Use **Transaction** mode URI from Supabase (port `5432`). If you use the **pooler** (port `6543`), add `?sslmode=require` to the URL and set `statement_cache_size=0` in code if you hit prepared-statement errors.

## Run

```bash
python main.py      # scrape + score for USER_EMAIL
python viewer.py    # dashboard for that user
```

Flow:

1. Creates tables (`users`, `jobs`, `applications`) if missing
2. Ensures your user row exists (`USER_EMAIL` / `USER_NAME`)
3. Scrapes Greenhouse jobs into `jobs` (dedupe by `url`)
4. Scores jobs for **your** `user_id` into `applications` (keyword matcher, 1–10)

## Project structure

```
job-bot/
├── .env
├── config.py       # DATABASE_URL, USER_EMAIL, USER_NAME
├── db.py           # asyncpg + multi-tenant schema
├── main.py
├── matcher.py      # keyword scoring + user_id
├── viewer.py       # CLI dashboard per user
├── scrapers/
│   ├── greenhouse.py
│   └── indeed.py
└── requirements.txt
```

## Next steps

- Playwright apply engine for Greenhouse/Lever
- Web UI + auth (map login → `user_id`)
- Stripe billing
