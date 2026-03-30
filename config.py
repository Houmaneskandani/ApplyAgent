from dotenv import load_dotenv
import os

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CAPSOLVER_API_KEY = os.getenv("CAPSOLVER_API_KEY", "")  # get at capsolver.com
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")            # get at rapidapi.com → JSearch (free tier)
JOB_KEYWORDS = os.getenv("JOB_KEYWORDS", "").split(",")
JOB_LOCATION = os.getenv("JOB_LOCATION", "")
DATABASE_URL = os.getenv("DATABASE_URL")

# Default user for local runs (matches web login later)
USER_EMAIL = os.getenv("USER_EMAIL", "you@email.com")
USER_NAME = os.getenv("USER_NAME", "Your Name")

STRIPE_SECRET_KEY     = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
FRONTEND_URL          = os.getenv("FRONTEND_URL", "http://localhost:5173")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM        = os.getenv("TWILIO_FROM", "")   # your Twilio number e.g. +15551234567
TWILIO_TO          = os.getenv("TWILIO_TO", "")     # your personal number to receive texts

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")
