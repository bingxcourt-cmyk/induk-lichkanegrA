import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def _load_env():
    env = BASE / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


def _chat(v: str):
    v = v.strip()
    return int(v) if v.lstrip("-").isdigit() else v


API_ID = int(os.environ.get("API_ID") or 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SOURCE_CHANNEL = _chat(os.environ.get("SOURCE_CHANNEL", "0"))
TARGET_CHANNEL = _chat(os.environ.get("TARGET_CHANNEL", "0"))
ADMIN_ID = int(os.environ.get("ADMIN_ID", "7842338078"))
TZ = os.environ.get("TZ_NAME", "Europe/Moscow")
STATS_TIME = os.environ.get("STATS_TIME", "23:40")
CANCEL_DELAY_MIN = int(os.environ.get("CANCEL_DELAY_MIN", "10"))

SESSION = str(BASE / "data" / "user")
DB_PATH = str(BASE / "data" / "bot.db")

(BASE / "data").mkdir(exist_ok=True)
