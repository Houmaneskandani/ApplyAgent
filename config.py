from dotenv import load_dotenv
import os

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
JOB_KEYWORDS = os.getenv("JOB_KEYWORDS", "").split(",")
JOB_LOCATION = os.getenv("JOB_LOCATION", "")
DATABASE_URL = os.getenv("DATABASE_URL")

# Default user for local runs (matches web login later)
USER_EMAIL = os.getenv("USER_EMAIL", "you@email.com")
USER_NAME = os.getenv("USER_NAME", "Your Name")
