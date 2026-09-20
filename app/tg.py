"""Публикация и редактирование в Telegram-канале через Bot API (через прокси/VPN, если настроен)."""
import json
import re

import httpx

from . import config, xray

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


class TgError(Exception):
    pass


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{config.TG_BOT_TOKEN}/{method}"


def client(timeout: float = 120) -> httpx.AsyncClient:
    return httpx.AsyncClient(proxy=xray.tg_proxy_url(), timeout=timeout)


async def call(c: httpx.AsyncClient, method: str, data: dict | None = None, files: dict | None = None):
    try:
        r = await c.post(_api(method), data=data, files=files)
    except httpx.HTTPError as e:
        raise TgError(f"Telegram недоступен ({e.__class__.__name__}: {e}). Проверь VPN/прокси.") from e
    try:
        res = r.json()
    except ValueError as e:
        raise TgError(f"Telegram: некорректный ответ {r.status_code}") from e
    if not res.get("ok"):
        raise TgError(f"Telegram {method}: {res.get('description')}")
    return res["result"]


def _msg_url(chat: dict, message_id: int) -> str | None:
    if chat.get("username"):
        return f"https://t.me/{chat['username']}/{message_id}"
    cid = str(chat.get("id", ""))
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{message_id}"
    return None


def plain_len(html: str) -> int:
    """Длина видимого текста: теги не считаются, сущности – как один символ."""
    return len(re.sub(r"&(amp|lt|gt|quot);", "x", re.sub(r"<[^>]+>", "", html or "")))


def _check(text: str, image_paths: list[str]) -> int:
    if not config.TG_CONFIGURED or not config.TG_CHAT_ID:
        raise TgError("TG_BOT_TOKEN / TG_CHAT_ID не заданы в .env")
    n = plain_len(text)
    if not n and not image_paths:
        raise TgError("Пустой пост")
    if n > TEXT_LIMIT:
        raise TgError(f"Текст длиннее {TEXT_LIMIT} символов")
    if len(image_paths) > 10:
        raise TgError("Telegram принимает не больше 10 фото в одном посте")
    return n


async def publish(text: str, image_paths: list[str], as_file: bool = False) -> dict:
    """text – HTML из редактора (подмножество, которое понимает Bot API).
    as_file – отправить картинки как документы, без сжатия Telegram."""
    n = _check(text, image_paths)
    kind = "document" if as_file else "photo"
    base = {"chat_id": config.TG_CHAT_ID, "parse_mode": "HTML"}
    caption_fits = n <= CAPTION_LIMIT
    media_ids: list[int] = []
    text_id: int | None = None
    first: dict = {}

    async with client() as c:
        if not image_paths:
            first = await call(c, "sendMessage", {**base, "text": text})
            text_id = first["message_id"]
        elif len(image_paths) == 1:
            with open(image_paths[0], "rb") as f:
                data = dict(base)
                if text and caption_fits:
                    data["caption"] = text
                first = await call(c, "sendDocument" if as_file else "sendPhoto", data, files={kind: f})
            media_ids = [first["message_id"]]
        else:
            handles = [open(p, "rb") for p in image_paths]
            try:
                media = []
                for i, _ in enumerate(image_paths):
                    item = {"type": kind, "media": f"attach://file{i}"}
                    if i == 0 and text and caption_fits:
                        item["caption"] = text
                        item["parse_mode"] = "HTML"
                    media.append(item)
                files = {f"file{i}": h for i, h in enumerate(handles)}
                res = await call(c, "sendMediaGroup", {"chat_id": config.TG_CHAT_ID, "media": json.dumps(media)}, files=files)
                first = res[0]
                media_ids = [m["message_id"] for m in res]
            finally:
                for h in handles:
                    h.close()

        if image_paths and text and not caption_fits:
            msg = await call(c, "sendMessage", {**base, "text": text})
            text_id = msg["message_id"]

    chat = first.get("chat", {})
    return {
        "message_id": first["message_id"],
        "media_ids": media_ids,
        "text_id": text_id,
        "as_file": as_file,
        "chat_id": chat.get("id"),
        "url": _msg_url(chat, first["message_id"]),
    }


async def edit(result: dict, text: str, image_paths: list[str], as_file: bool = False) -> dict:
    """Правит уже опубликованный пост. Telegram не даёт менять число фото в альбоме
    и переносить текст между подписью и отдельным сообщением – в этих случаях ошибка."""
    n = _check(text, image_paths)
    media_ids = list(result.get("media_ids") or [])
    text_id = result.get("text_id")
    if len(image_paths) != len(media_ids):
        raise TgError("Telegram не даёт менять количество фото в опубликованном посте – удали пост и опубликуй заново")
    if bool(result.get("as_file")) != bool(as_file) and media_ids:
        raise TgError("Telegram не даёт менять тип вложения (фото ↔ файл) в опубликованном посте")
    caption_fits = n <= CAPTION_LIMIT
    if media_ids and (text_id is None) != (caption_fits or not text):
        raise TgError("Подпись пересекла лимит 1024 символов – Telegram не сможет перестроить пост. Верни длину или опубликуй заново")

    kind = "document" if as_file else "photo"
    base = {"chat_id": config.TG_CHAT_ID, "parse_mode": "HTML"}
    async with client() as c:
        for i, (mid, path) in enumerate(zip(media_ids, image_paths)):
            media = {"type": kind, "media": "attach://f"}
            if i == 0 and text and caption_fits:
                media["caption"] = text
                media["parse_mode"] = "HTML"
            with open(path, "rb") as f:
                await call(c, "editMessageMedia", {"chat_id": config.TG_CHAT_ID, "message_id": mid, "media": json.dumps(media)}, files={"f": f})
        if text_id:
            await call(c, "editMessageText", {**base, "message_id": text_id, "text": text})
    return result


async def chat_info() -> dict | None:
    if not config.TG_CONFIGURED or not config.TG_CHAT_ID:
        return None
    async with client(20) as c:
        chat = await call(c, "getChat", {"chat_id": config.TG_CHAT_ID})
        try:
            members = await call(c, "getChatMemberCount", {"chat_id": config.TG_CHAT_ID})
        except TgError:
            members = None
        return {"name": chat.get("title") or chat.get("username"), "username": chat.get("username"), "id": chat.get("id"), "members": members}


async def bot_info() -> dict:
    async with client(20) as c:
        me = await call(c, "getMe")
        return {"username": me.get("username"), "name": me.get("first_name")}


async def find_chats() -> list[dict]:
    """Каналы/чаты, из которых бот видел сообщения (для поиска TG_CHAT_ID)."""
    async with client(30) as c:
        updates = await call(c, "getUpdates", {"limit": 100, "allowed_updates": json.dumps(["channel_post", "message", "my_chat_member"])})
    seen: dict = {}
    for u in updates:
        obj = u.get("channel_post") or u.get("message") or u.get("my_chat_member") or {}
        chat = obj.get("chat")
        if chat:
            seen[chat["id"]] = {"id": chat["id"], "title": chat.get("title") or chat.get("first_name"), "username": chat.get("username"), "type": chat.get("type")}
    return list(seen.values())
