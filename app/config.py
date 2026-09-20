import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


ADMIN_LOGIN = _env("ADMIN_LOGIN", "admin")
ADMIN_PASSWORD = _env("ADMIN_PASSWORD", "")
SECRET_KEY = _env("SECRET_KEY") or hashlib.sha256(
    f"{ADMIN_LOGIN}:{ADMIN_PASSWORD}:crosspost".encode()
).hexdigest()

VK_TOKEN = _env("VK_TOKEN")
VK_GROUP_ID = _env("VK_GROUP_ID").lstrip("-")

TG_BOT_TOKEN = _env("TG_BOT_TOKEN")
TG_CHAT_ID = _env("TG_CHAT_ID")
TG_VLESS = _env("TG_VLESS")
TG_PROXY = _env("TG_PROXY")
# аккаунт пользователя Telegram (для отложки на серверах Telegram): api_id/api_hash с my.telegram.org
TG_API_ID = _env("TG_API_ID")
TG_API_HASH = _env("TG_API_HASH")
TG_MTPROXY = _env("TG_MTPROXY")   # tg://proxy?server=...&port=...&secret=...
XRAY_BIN = _env("XRAY_BIN")
XRAY_SOCKS_PORT = int(_env("XRAY_SOCKS_PORT", "10808"))

HOST = _env("HOST", "0.0.0.0")
PORT = int(_env("PORT", "8000"))

DATA_DIR = Path(_env("DATA_DIR") or BASE_DIR / "data")
UPLOAD_DIR = DATA_DIR / "uploads"
BG_DIR = DATA_DIR / "backgrounds"
DB_PATH = DATA_DIR / "crosspost.db"
TG_SESSION = DATA_DIR / "tg_user"   # Telethon-сессия аккаунта (tg_user.session)

for d in (DATA_DIR, UPLOAD_DIR, BG_DIR):
    d.mkdir(parents=True, exist_ok=True)

VK_CONFIGURED = bool(VK_TOKEN and VK_GROUP_ID)
TG_CONFIGURED = bool(TG_BOT_TOKEN)
TG_USER_CONFIGURED = bool(TG_API_ID and TG_API_HASH)


def set_env_value(key: str, value: str) -> None:
    """Записывает KEY=value в .env (заменяя строку или добавляя) и обновляет текущий процесс."""
    path = BASE_DIR / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    done = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            done = True
    if not done:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value
    globals()[key] = value
    if key in ("VK_TOKEN", "VK_GROUP_ID"):
        globals()["VK_CONFIGURED"] = bool(globals()["VK_TOKEN"] and globals()["VK_GROUP_ID"])
