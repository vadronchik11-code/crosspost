"""Telegram через аккаунт пользователя (MTProto, Telethon).
Нужен для отложенных сообщений: они лежат на серверах Telegram, а бот их создавать не умеет
(SCHEDULE_BOT_NOT_ALLOWED). Аккаунт должен быть админом канала с правом публикации."""
import asyncio
import logging
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from . import config
from .tg import TgError, plain_len

log = logging.getLogger("tguser")

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


def _html_for_telethon(html: str) -> str:
    """Наш HTML -> HTML, который понимает парсер Telethon."""
    return (html or "").replace("<tg-spoiler>", "<spoiler>").replace("</tg-spoiler>", "</spoiler>")


def _plain(html: str) -> str:
    t = re.sub(r"<[^>]+>", "", html or "")
    return t.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")


def parse_mtproxy(link: str) -> tuple[str, int, str]:
    u = urlparse(link.strip().strip('"').strip("'"))
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    if u.scheme not in ("tg", "https") or not q.get("server") or not q.get("port") or not q.get("secret"):
        raise TgError("TG_MTPROXY: нужна ссылка вида tg://proxy?server=...&port=...&secret=...")
    secret = q["secret"].lower()
    if secret.startswith("ee"):
        raise TgError("TG_MTPROXY: секрет с префиксом ee (Fake TLS) Telethon не поддерживает – нужен dd… или обычный")
    return q["server"], int(q["port"]), secret


class TgUser:
    def __init__(self):
        self.client = None
        self.error: str | None = None
        self._phone: str | None = None
        self._code_hash: str | None = None
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- подключение
    async def start(self) -> None:
        if not config.TG_USER_CONFIGURED:
            return
        try:
            from telethon import TelegramClient
            from telethon.network import connection as tconn
            kwargs = {}
            if config.TG_MTPROXY:
                host, port, secret = parse_mtproxy(config.TG_MTPROXY)
                if secret.startswith("dd"):
                    secret = secret[2:]
                    kwargs["connection"] = tconn.ConnectionTcpMTProxyRandomizedIntermediate
                else:
                    kwargs["connection"] = tconn.ConnectionTcpMTProxyIntermediate
                kwargs["proxy"] = (host, port, secret)
            else:
                from . import xray
                socks = xray.tg_proxy_url()   # TG_PROXY или локальный SOCKS5 от xray (TG_VLESS)
                if socks and "://" in socks:
                    u = urlparse(socks)
                    kwargs["proxy"] = {"proxy_type": u.scheme.replace("socks5h", "socks5"), "addr": u.hostname, "port": u.port,
                                       "username": u.username, "password": u.password}
            self.client = TelegramClient(str(config.TG_SESSION), int(config.TG_API_ID), config.TG_API_HASH, **kwargs)
            await asyncio.wait_for(self.client.connect(), timeout=30)
            self.error = None
            log.info("Telegram-аккаунт: подключено, авторизован=%s", await self.client.is_user_authorized())
        except Exception as e:  # noqa: BLE001
            self.error = f"Telegram-аккаунт: не удалось подключиться ({e})"
            log.error(self.error)
            self.client = None

    async def stop(self) -> None:
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _ensure(self):
        if not self.client:
            await self.start()
        if not self.client:
            raise TgError(self.error or "Telegram-аккаунт не настроен: задай TG_API_ID и TG_API_HASH в .env")
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    async def authorized(self) -> bool:
        try:
            c = await self._ensure()
            return await c.is_user_authorized()
        except Exception:  # noqa: BLE001
            return False

    async def status(self) -> dict:
        from . import xray
        out = {"configured": config.TG_USER_CONFIGURED, "authorized": False, "name": None, "error": self.error,
               "mtproxy": bool(config.TG_MTPROXY), "proxy": config.TG_MTPROXY[:24] + "…" if config.TG_MTPROXY else xray.tg_proxy_url()}
        if not config.TG_USER_CONFIGURED:
            return out
        try:
            c = await self._ensure()
            if await c.is_user_authorized():
                me = await c.get_me()
                out["authorized"] = True
                out["name"] = " ".join(x for x in (me.first_name, me.last_name) if x) or me.username
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)
        return out

    # ---------------------------------------------------------------- вход
    async def send_code(self, phone: str) -> dict:
        c = await self._ensure()
        phone = re.sub(r"[^\d+]", "", phone)
        sent = await c.send_code_request(phone)
        self._phone, self._code_hash = phone, sent.phone_code_hash
        return {"ok": True, "phone": phone}

    async def sign_in(self, code: str, password: str | None = None) -> dict:
        from telethon.errors import SessionPasswordNeededError
        c = await self._ensure()
        if not self._phone or not self._code_hash:
            raise TgError("Сначала запроси код")
        try:
            await c.sign_in(self._phone, code.strip(), phone_code_hash=self._code_hash)
        except SessionPasswordNeededError:
            if not password:
                return {"ok": False, "need_password": True}
            await c.sign_in(password=password)
        self._phone = self._code_hash = None
        return {"ok": True, **(await self.status())}

    async def logout(self) -> None:
        if self.client:
            try:
                await self.client.log_out()
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- публикация
    async def _entity(self, c):
        chat = config.TG_CHAT_ID
        if re.fullmatch(r"-?\d+", chat):
            return await c.get_input_entity(int(chat))
        return await c.get_input_entity(chat)

    def _check(self, text: str, image_paths: list[str]) -> int:
        if not config.TG_CHAT_ID:
            raise TgError("TG_CHAT_ID не задан в .env")
        n = plain_len(text)
        if not n and not image_paths:
            raise TgError("Пустой пост")
        if n > TEXT_LIMIT:
            raise TgError(f"Текст длиннее {TEXT_LIMIT} символов")
        if len(image_paths) > 10:
            raise TgError("Telegram принимает не больше 10 фото в одном посте")
        return n

    async def send(self, text: str, image_paths: list[str], as_file: bool, when: datetime | None) -> dict:
        """Отправить сразу (when=None) или отложить на серверах Telegram (when – момент выхода)."""
        n = self._check(text, image_paths)
        c = await self._ensure()
        if not await c.is_user_authorized():
            raise TgError("Telegram-аккаунт не авторизован – войди через «Telegram» в шапке сайта")
        ent = await self._entity(c)
        html = _html_for_telethon(text)
        fits = n <= CAPTION_LIMIT
        media_ids: list[int] = []
        text_id: int | None = None
        async with self._lock:
            if image_paths:
                msgs = await c.send_file(ent, image_paths if len(image_paths) > 1 else image_paths[0],
                                        caption=html if (text and fits) else None, parse_mode="html",
                                        force_document=as_file, schedule=when)
                msgs = msgs if isinstance(msgs, list) else [msgs]
                media_ids = [m.id for m in msgs]
                if text and not fits:
                    m = await c.send_message(ent, html, parse_mode="html", schedule=when)
                    text_id = m.id
            else:
                m = await c.send_message(ent, html, parse_mode="html", schedule=when)
                text_id = m.id
        return {
            "via": "user", "scheduled": bool(when), "when": int(when.timestamp()) if when else None,
            "media_ids": media_ids, "text_id": text_id, "as_file": as_file, "plain": _plain(text)[:200],
            "message_id": (media_ids or [text_id])[0], "url": None,
        }

    async def delete_scheduled(self, result: dict) -> None:
        from telethon.tl import functions
        c = await self._ensure()
        ids = [i for i in (result.get("media_ids") or []) + [result.get("text_id")] if i]
        if not ids:
            return
        ent = await self._entity(c)
        try:
            await c(functions.messages.DeleteScheduledMessagesRequest(peer=ent, id=ids))
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось удалить отложенные сообщения %s: %s", ids, e)

    async def resolve_published(self, result: dict) -> dict | None:
        """После срабатывания отложки у сообщений новые id – ищем их в канале по тексту и времени."""
        c = await self._ensure()
        ent = await self._entity(c)
        when = result.get("when") or 0
        plain = (result.get("plain") or "").strip()
        msgs = await c.get_messages(ent, limit=40)
        near = [m for m in msgs if m.date and abs(m.date.timestamp() - when) < 900]
        media_ids, text_id = [], None
        want_media = len(result.get("media_ids") or [])
        if plain:
            for m in near:
                if (m.raw_text or "").strip()[:200] == plain[:200] and not (m.media and want_media == 0):
                    if m.media and want_media:
                        gid = m.grouped_id
                        group = [x for x in near if x.media and (x.grouped_id == gid if gid else x.id == m.id)]
                        media_ids = sorted(x.id for x in group)
                    else:
                        text_id = m.id
                    break
            if want_media and not media_ids:
                for m in near:
                    if m.media and not (m.raw_text or "").strip():
                        gid = m.grouped_id
                        group = [x for x in near if x.media and (x.grouped_id == gid if gid else x.id == m.id)]
                        media_ids = sorted(x.id for x in group)
                        break
        elif want_media:
            for m in near:
                if m.media:
                    gid = m.grouped_id
                    group = [x for x in near if x.media and (x.grouped_id == gid if gid else x.id == m.id)]
                    media_ids = sorted(x.id for x in group)
                    break
        if not media_ids and not text_id:
            return None
        first = (media_ids or [text_id])[0]
        chat = config.TG_CHAT_ID
        url = f"https://t.me/{chat.lstrip('@')}/{first}" if chat.startswith("@") else (f"https://t.me/c/{chat[4:]}/{first}" if chat.startswith("-100") else None)
        return {**result, "scheduled": False, "media_ids": media_ids, "text_id": text_id, "message_id": first, "url": url}

    async def edit(self, result: dict, text: str, image_paths: list[str], as_file: bool) -> dict:
        """Правка уже вышедшего поста аккаунтом (те же ограничения Telegram, что и у бота)."""
        n = self._check(text, image_paths)
        c = await self._ensure()
        ent = await self._entity(c)
        media_ids = list(result.get("media_ids") or [])
        text_id = result.get("text_id")
        if len(image_paths) != len(media_ids):
            raise TgError("Telegram не даёт менять количество фото в опубликованном посте – удали пост и опубликуй заново")
        fits = n <= CAPTION_LIMIT
        if media_ids and (text_id is None) != (fits or not text):
            raise TgError("Подпись пересекла лимит 1024 символов – Telegram не сможет перестроить пост")
        html = _html_for_telethon(text)
        for i, (mid, path) in enumerate(zip(media_ids, image_paths)):
            await c.edit_message(ent, mid, text=html if (i == 0 and text and fits) else "", file=path,
                                 parse_mode="html", force_document=as_file)
        if text_id:
            await c.edit_message(ent, text_id, html, parse_mode="html")
        return {**result, "plain": _plain(text)[:200]}


manager = TgUser()


def to_dt(ts: int | None) -> datetime | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
