"""Поднимает xray-core из vless-ссылки и отдаёт локальный SOCKS5-прокси для Telegram."""
import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import config

log = logging.getLogger("xray")


def parse_vless(link: str) -> dict:
    """vless://uuid@host:port?params#name -> xray outbound"""
    link = link.strip().strip('"').strip("'").strip()
    u = urlparse(link)
    if u.scheme != "vless":
        raise ValueError(f"Ссылка должна начинаться с vless:// (сейчас начинается с «{link[:12]}…»). Нужна сама vless-ссылка, не ссылка на подписку")
    if not u.username or not u.hostname or not u.port:
        raise ValueError("Не удалось разобрать vless-ссылку (uuid@host:port)")
    q = {k: unquote(v[0]) for k, v in parse_qs(u.query).items()}

    network = q.get("type", "tcp")
    security = q.get("security", "none")
    user = {"id": u.username, "encryption": q.get("encryption", "none")}
    if q.get("flow"):
        user["flow"] = q["flow"]

    stream: dict = {"network": network, "security": security}

    if security == "tls":
        tls = {"serverName": q.get("sni") or q.get("host") or u.hostname, "allowInsecure": q.get("allowInsecure") in ("1", "true")}
        if q.get("fp"):
            tls["fingerprint"] = q["fp"]
        if q.get("alpn"):
            tls["alpn"] = q["alpn"].split(",")
        stream["tlsSettings"] = tls
    elif security == "reality":
        stream["realitySettings"] = {
            "serverName": q.get("sni", ""),
            "fingerprint": q.get("fp", "chrome"),
            "publicKey": q.get("pbk", ""),
            "shortId": q.get("sid", ""),
            "spiderX": q.get("spx", "/"),
        }

    if network == "ws":
        ws = {"path": q.get("path", "/")}
        if q.get("host"):
            ws["headers"] = {"Host": q["host"]}
        stream["wsSettings"] = ws
    elif network == "grpc":
        stream["grpcSettings"] = {"serviceName": q.get("serviceName", ""), "multiMode": q.get("mode") == "multi"}
    elif network in ("http", "h2"):
        stream["network"] = "http"
        stream["httpSettings"] = {"path": q.get("path", "/"), "host": [q["host"]] if q.get("host") else []}
    elif network == "httpupgrade":
        stream["httpupgradeSettings"] = {"path": q.get("path", "/"), "host": q.get("host", "")}
    elif network in ("xhttp", "splithttp"):
        stream["network"] = "xhttp"
        x = {"path": q.get("path", "/"), "host": q.get("host", "")}
        if q.get("mode"):
            x["mode"] = q["mode"]
        stream["xhttpSettings"] = x
    elif network == "tcp" and q.get("headerType") == "http":
        stream["tcpSettings"] = {"header": {"type": "http", "request": {"path": [q.get("path", "/")], "headers": {"Host": [q.get("host", "")]}}}}

    return {
        "tag": "proxy",
        "protocol": "vless",
        "settings": {"vnext": [{"address": u.hostname, "port": u.port, "users": [user]}]},
        "streamSettings": stream,
    }


def build_config(link: str, socks_port: int) -> dict:
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {"tag": "socks", "listen": "127.0.0.1", "port": socks_port, "protocol": "socks", "settings": {"udp": True}},
            {"tag": "http", "listen": "127.0.0.1", "port": socks_port + 1, "protocol": "http"},
        ],
        "outbounds": [parse_vless(link), {"tag": "direct", "protocol": "freedom"}],
    }


def find_binary() -> str | None:
    candidates = []
    if config.XRAY_BIN:
        candidates.append(config.XRAY_BIN)
    candidates += [
        str(config.BASE_DIR / "xray" / "xray.exe"),
        str(config.BASE_DIR / "xray" / "xray"),
        "/usr/local/bin/xray",
    ]
    for c in candidates:
        if Path(c).is_file():
            return c
    return shutil.which("xray")


class XrayManager:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def proxy_url(self) -> str | None:
        return f"socks5://127.0.0.1:{config.XRAY_SOCKS_PORT}" if self.running else None

    async def start(self) -> None:
        if not config.TG_VLESS:
            return
        binary = find_binary()
        if not binary:
            self.error = "xray не найден: положи xray.exe в папку ./xray или укажи XRAY_BIN"
            log.error(self.error)
            return
        try:
            cfg = build_config(config.TG_VLESS, config.XRAY_SOCKS_PORT)
        except Exception as e:  # noqa: BLE001
            self.error = f"Ошибка разбора TG_VLESS: {e}"
            log.error(self.error)
            return
        cfg_path = config.DATA_DIR / "xray_config.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Запускаю xray: %s", binary)
        self.proc = subprocess.Popen(
            [binary, "run", "-c", str(cfg_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        await asyncio.sleep(1.5)
        if not self.running:
            err = self.proc.stderr.read().decode(errors="ignore") if self.proc.stderr else ""
            self.error = f"xray не запустился: {err.strip()[-500:]}"
            log.error(self.error)
        else:
            self.error = None
            log.info("xray работает, SOCKS5 на 127.0.0.1:%s", config.XRAY_SOCKS_PORT)

    def stop(self) -> None:
        if self.running:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


manager = XrayManager()


PROXY_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4")


def tg_proxy_url() -> str | None:
    """Прокси для Telegram: явный TG_PROXY имеет приоритет, иначе – запущенный xray.
    tg://proxy (MTProto) сюда не подходит: он только для Telegram-клиентов, а бот ходит по HTTPS."""
    if config.TG_PROXY:
        scheme = config.TG_PROXY.split("://", 1)[0].lower() if "://" in config.TG_PROXY else ""
        if scheme in PROXY_SCHEMES:
            return config.TG_PROXY
        manager.error = (
            f"TG_PROXY «{config.TG_PROXY[:14]}…» не подходит: нужен socks5:// или http:// прокси "
            f"(tg://proxy – MTProto, он только для клиента Telegram). Пока прокси не используется."
        )
        log.error(manager.error)
        return None
    return manager.proxy_url
