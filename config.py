import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS")

SHARED_SECRET = os.getenv("SHARED_SECRET")