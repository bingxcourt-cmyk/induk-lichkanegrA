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

# Правила Forex Bulls: перевод реплаев через Claude API
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

# Правила Forex Bulls: цена FxPro через cTrader Open API
CTRADER_CLIENT_ID = os.environ.get("CTRADER_CLIENT_ID", "")
CTRADER_CLIENT_SECRET = os.environ.get("CTRADER_CLIENT_SECRET", "")
CTRADER_ACCESS_TOKEN = os.environ.get("CTRADER_ACCESS_TOKEN", "")
CTRADER_REFRESH_TOKEN = os.environ.get("CTRADER_REFRESH_TOKEN", "")
CTRADER_ACCOUNT_ID = os.environ.get("CTRADER_ACCOUNT_ID", "")
CTRADER_LIVE = os.environ.get("CTRADER_LIVE", "1") not in ("0", "false", "no", "")
FXPRO_SYMBOL = os.environ.get("FXPRO_SYMBOL", "XAUUSD")

# Временно, пока не одобрен cTrader: цена OANDA (v20 API). Если cTrader настроен, OANDA не используется.
OANDA_TOKEN = os.environ.get("OANDA_TOKEN", "")
OANDA_LIVE = os.environ.get("OANDA_LIVE", "0") not in ("0", "false", "no", "")

SESSION = str(BASE / "data" / "user")
DB_PATH = str(BASE / "data" / "bot.db")
CTRADER_TOKEN_FILE = str(BASE / "data" / "ctrader_token.json")  # обновлённые токены cTrader

(BASE / "data").mkdir(exist_ok=True)
