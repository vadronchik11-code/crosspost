"""Публикация в сообщество VK: загрузка фото на стену + wall.post."""
import asyncio
import json
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from . import config

# VK всегда напрямую: trust_env=False игнорирует HTTP(S)_PROXY, а xray (TG_VLESS) сюда не подключён вовсе
API = "https://api.vk.com/method/"
VERSION = "5.199"


class VkError(Exception):
    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code


_token_type: str | None = None  # "user" | "group", определяется один раз


async def call(client: httpx.AsyncClient, method: str, **params):
    params.update(access_token=config.VK_TOKEN, v=VERSION)
    r = await client.post(API + method, data=params)
    data = r.json()
    if "error" in data:
        err = data["error"]
        code = err.get("error_code")
        if code == 5:
            raise VkError("VK: токен недействителен или истёк (error 5). Создай новый ключ доступа сообщества.", code)
        if code == 15:
            raise VkError(f"VK: нет доступа ({err.get('error_msg')}). У ключа сообщества должны быть права «Стена», «Фотографии», «Сообщения».", code)
        if code == 27:
            raise VkError(f"VK {method}: метод недоступен ключу сообщества ({err.get('error_msg')})", code)
        raise VkError(f"VK {method}: [{code}] {err.get('error_msg')}", code)
    return data["response"]


_group_perms: list[str] = []


async def token_type(client: httpx.AsyncClient) -> str:
    """groups.getTokenPermissions отвечает только ключу сообщества (пользователю – ошибка 27)."""
    global _token_type, _group_perms
    if _token_type is None:
        try:
            res = await call(client, "groups.getTokenPermissions")
            _token_type = "group"
            _group_perms = [p.get("name") for p in res.get("permissions", [])]
        except VkError as e:
            if e.code != 27:
                raise
            _token_type = "user"
    return _token_type


MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}


async def _upload(client: httpx.AsyncClient, upload_url: str, path: str) -> dict:
    """POST файла на загрузчик VK. Тело отправляем одним куском из памяти:
    при потоковой отдаче VK иногда отвечает раньше, чем получит файл, и возвращает photo=''."""
    filename = path.replace("\\", "/").rsplit("/", 1)[-1]
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    with open(path, "rb") as f:
        data = f.read()
    r = await client.post(upload_url, files={"photo": (filename, data, MIME.get(ext, "application/octet-stream"))})
    try:
        up = r.json()
    except ValueError:
        raise VkError(f"VK upload: некорректный ответ загрузчика ({r.status_code})")
    if "error" in up:
        raise VkError(f"VK upload: {up.get('error')} {up.get('error_descr', '')}".strip())
    if not up.get("photo") or up.get("photo") == "[]":
        raise VkError("VK upload: загрузчик не принял файл (пустой photo)")
    return up


async def _upload_retry(client: httpx.AsyncClient, get_server, path: str, attempts: int = 3) -> dict:
    """Пустой ответ загрузчика бывает случайным – пробуем ещё раз с новым upload_url."""
    last: Exception | None = None
    for i in range(attempts):
        srv = await get_server()
        try:
            return await _upload(client, srv["upload_url"], path)
        except VkError as e:
            if "пустой photo" not in str(e) and "некорректный ответ" not in str(e):
                raise
            last = e
            await asyncio.sleep(1.5 * (i + 1))
    raise VkError(
        f"{last}. VK трижды не принял картинку. Проверь, что файл – обычный JPG/PNG, "
        f"и попробуй ещё раз; если не помогает – переключи VPN на другой сервер или отключи его для vk.com."
    )


GROUP_PHOTO_ERROR = "Ключ сообщества не может прикрепить фото к посту VK – в VK уходит только текст."


async def _upload_as_group(client: httpx.AsyncClient, path: str) -> str:
    """Ключ сообщества: загрузчик сообщений работает, но wall.post такие фото игнорирует.
    Оставлено на случай, если VK это разрешит; сейчас publish() не пускает сюда."""
    async def get_server():
        try:
            return await call(client, "photos.getMessagesUploadServer", peer_id=0)
        except VkError as e:
            if e.code in (7, 15):
                raise VkError("VK: у ключа сообщества нет права «Сообщения» – без него фото не загрузить. Создай ключ заново с правами «Стена», «Фотографии», «Сообщения».", e.code)
            raise
    up = await _upload_retry(client, get_server, path)
    saved = await call(client, "photos.saveMessagesPhoto", server=up["server"], photo=up["photo"], hash=up["hash"])
    p = saved[0]
    att = f"photo{p['owner_id']}_{p['id']}"
    return f"{att}_{p['access_key']}" if p.get("access_key") else att


async def _upload_as_user(client: httpx.AsyncClient, path: str) -> str:
    up = await _upload_retry(client, lambda: call(client, "photos.getWallUploadServer", group_id=config.VK_GROUP_ID), path)
    saved = await call(
        client, "photos.saveWallPhoto",
        group_id=config.VK_GROUP_ID, server=up["server"], photo=up["photo"], hash=up["hash"],
    )
    p = saved[0]
    return f"photo{p['owner_id']}_{p['id']}"


async def upload_wall_photo(client: httpx.AsyncClient, path: str) -> str:
    """Загружает фото и возвращает строку вложения для wall.post."""
    global _token_type
    if await token_type(client) == "group":
        return await _upload_as_group(client, path)
    try:
        return await _upload_as_user(client, path)
    except VkError as e:
        if e.code != 27:
            raise
        _token_type = "group"          # определение ошиблось – это всё-таки ключ сообщества
        return await _upload_as_group(client, path)


class _VkFormat(HTMLParser):
    """HTML из редактора -> чистый текст + format_data (жирный/курсив/подчёркнутый/ссылка).
    Смещения считаются в UTF-16 code units – так их считает веб-клиент VK."""
    TAGS = {"b": "bold", "strong": "bold", "i": "italic", "em": "italic", "u": "underline"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.pos = 0
        self.stack: list[tuple[str, int, str | None]] = []
        self.items: list[dict] = []

    def _emit(self, s: str):
        self.parts.append(s)
        self.pos += len(s.encode("utf-16-le")) // 2

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self._emit("\n")
        elif tag in self.TAGS:
            self.stack.append((self.TAGS[tag], self.pos, None))
        elif tag == "a":
            self.stack.append(("url", self.pos, dict(attrs).get("href")))

    def handle_endtag(self, tag):
        kind = self.TAGS.get(tag) or ("url" if tag == "a" else None)
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == kind:
                _, start, url = self.stack.pop(i)
                if self.pos > start:
                    item = {"type": kind, "offset": start, "length": self.pos - start}
                    if kind == "url":
                        if not url:
                            continue
                        item["url"] = url
                    self.items.append(item)
                break

    def handle_data(self, data):
        self._emit(data)


def html_to_vk(html: str) -> tuple[str, dict | None]:
    p = _VkFormat()
    p.feed(html or "")
    p.close()
    text = "".join(p.parts)
    return text, ({"version": "1", "items": p.items} if p.items else None)


async def publish(text: str, image_paths: list[str]) -> dict:
    """text – HTML из редактора; VK получает чистый текст + format_data."""
    if not config.VK_CONFIGURED:
        raise VkError("VK_TOKEN / VK_GROUP_ID не заданы в .env")
    message, format_data = html_to_vk(text)
    if not message.strip() and not image_paths:
        raise VkError("Пустой пост")
    async with httpx.AsyncClient(trust_env=False, timeout=90) as client:
        if image_paths and await token_type(client) == "group":
            raise VkError(GROUP_PHOTO_ERROR)
        attachments = [await upload_wall_photo(client, p) for p in image_paths]
        params = {"owner_id": f"-{config.VK_GROUP_ID}", "from_group": 1, "message": message}
        if format_data:
            params["format_data"] = json.dumps(format_data, ensure_ascii=False)
        if attachments:
            params["attachments"] = ",".join(attachments)
        res = await call(client, "wall.post", **params)
        post_id = res["post_id"]
        return {"post_id": post_id, "url": f"https://vk.com/wall-{config.VK_GROUP_ID}_{post_id}"}


async def edit(post_id: int, text: str, image_paths: list[str]) -> dict:
    """Правит уже опубликованный пост: wall.edit заменяет текст и вложения целиком."""
    if not config.VK_CONFIGURED:
        raise VkError("VK_TOKEN / VK_GROUP_ID не заданы в .env")
    message, format_data = html_to_vk(text)
    if not message.strip() and not image_paths:
        raise VkError("Пустой пост")
    async with httpx.AsyncClient(trust_env=False, timeout=90) as client:
        if image_paths and await token_type(client) == "group":
            raise VkError(GROUP_PHOTO_ERROR)
        attachments = [await upload_wall_photo(client, p) for p in image_paths]
        params = {"owner_id": f"-{config.VK_GROUP_ID}", "post_id": post_id, "message": message}
        if format_data:
            params["format_data"] = json.dumps(format_data, ensure_ascii=False)
        params["attachments"] = ",".join(attachments) if attachments else ""
        await call(client, "wall.edit", **params)
        return {"post_id": post_id, "url": f"https://vk.com/wall-{config.VK_GROUP_ID}_{post_id}"}


async def group_info() -> dict | None:
    if not config.VK_CONFIGURED:
        return None
    async with httpx.AsyncClient(trust_env=False, timeout=20) as client:
        res = await call(client, "groups.getById", group_id=config.VK_GROUP_ID, fields="photo_100,screen_name,members_count")
        g = res["groups"][0] if isinstance(res, dict) else res[0]
        return {
            "name": g.get("name"), "photo": g.get("photo_100"), "screen_name": g.get("screen_name"),
            "members": g.get("members_count"), "token_type": await token_type(client), "perms": _group_perms,
        }


# ---------------------------------------------------------------- отложенные записи (созданы браузером; ключом их можно найти/поправить/удалить)
def _att_string(a: dict) -> str | None:
    t = a.get("type")
    obj = a.get(t) or {}
    if t in ("photo", "video", "doc", "audio"):
        base = f"{t}{obj.get('owner_id')}_{obj.get('id')}"
        return f"{base}_{obj['access_key']}" if obj.get("access_key") else base
    if t == "link":
        return obj.get("url")
    return None


async def _postponed_items(client: httpx.AsyncClient) -> list[dict]:
    got = await call(client, "wall.get", owner_id=f"-{config.VK_GROUP_ID}", filter="postponed", count=100)
    return got.get("items", [])


async def find_postponed(plain_text: str, publish_at: int) -> int | None:
    """Ищем только что созданную браузером запись по времени выхода и тексту."""
    if not config.VK_CONFIGURED:
        return None
    key = (plain_text or "").strip()[:80]
    async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
        try:
            items = await _postponed_items(client)
        except VkError:
            return None
    best = None
    for it in items:
        if abs((it.get("date") or 0) - publish_at) <= 120 and (not key or (it.get("text") or "").strip().startswith(key[:40])):
            if best is None or it["id"] > best:
                best = it["id"]
    return best


async def edit_postponed(post_id: int, text: str, publish_at: int | None) -> dict:
    """Меняем текст/время отложенной записи, фото передаём явно – иначе wall.edit их снимет."""
    message, format_data = html_to_vk(text)
    async with httpx.AsyncClient(trust_env=False, timeout=60) as client:
        it = next((x for x in await _postponed_items(client) if x["id"] == post_id), None)
        if not it:
            raise VkError("Отложенной записи уже нет в очереди VK – она вышла или удалена")
        atts = [x for x in (_att_string(a) for a in it.get("attachments") or []) if x]
        params = {"owner_id": f"-{config.VK_GROUP_ID}", "post_id": post_id, "message": message, "attachments": ",".join(atts)}
        params["publish_date"] = max(int(publish_at or it.get("date") or 0), int(time.time()) + 90)
        if format_data:
            params["format_data"] = json.dumps(format_data, ensure_ascii=False)
        await call(client, "wall.edit", **params)
        after = next((x for x in await _postponed_items(client) if x["id"] == post_id), None)
        if after is not None and it.get("attachments") and not after.get("attachments"):
            raise VkError("После правки фото слетело с отложенной записи – VK не принял вложение обратно. Удали пост и создай заново")
        return {"post_id": post_id, "publish_at": params["publish_date"]}


async def delete_post(post_id: int) -> None:
    async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
        await call(client, "wall.delete", owner_id=f"-{config.VK_GROUP_ID}", post_id=post_id)
