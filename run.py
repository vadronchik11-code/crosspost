"""
Запуск на ПК одним файлом: создаёт venv, ставит зависимости, при необходимости
скачивает xray-core (для VLESS), поднимает сервер и открывает браузер.
"""
import io
import os
import platform
import shutil
import subprocess
import sys
import threading
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQ = ROOT / "requirements.txt"
XRAY_DIR = ROOT / "xray"
os.chdir(ROOT)


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def read_env() -> dict:
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def step_env():
    if (ROOT / ".env").exists():
        return
    shutil.copy(ROOT / ".env.example", ROOT / ".env")
    print("\nСоздал .env из .env.example. Заполни его (логин/пароль, токены) и запусти снова.\n")
    if os.name == "nt":
        subprocess.Popen(["notepad.exe", str(ROOT / ".env")])
    sys.exit(0)


def step_venv():
    if sys.version_info < (3, 10):
        sys.exit("Нужен Python 3.10 или новее")
    if not venv_python().exists():
        print("Создаю виртуальное окружение…")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
    marker = VENV / ".req_hash"
    req_hash = str(REQ.stat().st_mtime_ns)
    if not marker.exists() or marker.read_text() != req_hash:
        print("Устанавливаю зависимости…")
        subprocess.check_call([str(venv_python()), "-m", "pip", "install", "-q", "--upgrade", "pip"])
        subprocess.check_call([str(venv_python()), "-m", "pip", "install", "-q", "-r", str(REQ)])
        marker.write_text(req_hash)
    # Chromium для VK-браузера (один раз, ~150 МБ)
    pw_marker = VENV / ".chromium_ok"
    if not pw_marker.exists():
        print("Ставлю Chromium для VK-браузера…")
        try:
            subprocess.check_call([str(venv_python()), "-m", "playwright", "install", "chromium"])
            pw_marker.write_text("ok")
        except Exception as e:  # noqa: BLE001
            print(f"Chromium не установился ({e}) – VK-браузер работать не будет, остальное работает")


def step_xray(env: dict):
    if not env.get("TG_VLESS") or env.get("TG_PROXY"):
        return
    if env.get("XRAY_BIN") and Path(env["XRAY_BIN"]).exists():
        return
    exe = XRAY_DIR / ("xray.exe" if os.name == "nt" else "xray")
    if exe.exists() or shutil.which("xray"):
        return
    system = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}[platform.system()]
    arch = "arm64-v8a" if platform.machine().lower() in ("arm64", "aarch64") else "64"
    url = f"https://github.com/XTLS/Xray-core/releases/latest/download/Xray-{system}-{arch}.zip"
    print(f"Скачиваю xray-core: {url}")
    XRAY_DIR.mkdir(exist_ok=True)
    data = urllib.request.urlopen(url, timeout=120).read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(XRAY_DIR)
    if os.name != "nt":
        exe.chmod(0o755)
    print("xray-core установлен в ./xray")


def main():
    step_env()
    step_venv()
    env = read_env()
    try:
        step_xray(env)
    except Exception as e:  # noqa: BLE001
        print(f"Не удалось скачать xray автоматически: {e}\n"
              f"Скачай Xray-windows-64.zip с https://github.com/XTLS/Xray-core/releases и распакуй в папку ./xray")

    host = env.get("HOST") or "0.0.0.0"
    port = env.get("PORT") or "8000"
    url = f"http://127.0.0.1:{port}"
    print(f"\nСервер: {url}  (Ctrl+C — остановить)\n")
    threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    try:
        subprocess.call([str(venv_python()), "-m", "uvicorn", "app.main:app", "--host", host, "--port", port])
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
