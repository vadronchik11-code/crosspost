import asyncio
import hashlib
import hmac
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, tg, tguser, vk, vkweb, xray

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("crosspost")

STATIC_DIR = Path(__file__).parent / "static"
SESSION_TTL = 365 * 24 * 3600
ALLOWED_IMG = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
USERNAME_RE = re.compile(r"^[\w.\-]{2,32}$", re.UNICODE)

_in_progress: set[int] = set()
_targets_cache: dict = {"ts": 0, "data": None}


# ---------------------------------------------------------------- auth / sessions
def _sign(uid: int, exp: int) -> str:
    return hmac.new(config.SECRET_KEY.encode(), f"{uid}:{exp}".encode(), hashlib.sha256).hexdigest()


def _make_session(uid: int) -> str:
    exp = int(time.time()) + SESSION_TTL
    return f"{uid}.{exp}.{_sign(uid, exp)}"


def _session_user(value: str | None) -> dict | None:
    if not value:
        return None
    parts = value.split(".")
    if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    uid, exp, sig = int(parts[0]), int(parts[1]), parts[2]
    if exp < time.time() or not hmac.compare_digest(sig, _sign(uid, exp)):
        return None
    return db.get_user(uid)


def current_user(request: Request) -> dict:
    user = _session_user(request.cookies.get("session"))
    if not user:
        raise HTTPException(401, "Нужен вход")
    return user


def require_manager(user: dict = Depends(current_user)) -> dict:
    if user["role"] not in db.MANAGERS:
        raise HTTPException(403, "Только для руковода и тимлида")
    return user


def require_head(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "head":
        raise HTTPException(403, "Только для руковода")
    return user


def _can_edit_post(user: dict, post: dict) -> bool:
    return user["role"] in db.MANAGERS or post.get("created_by") == user["username"]


# ---------------------------------------------------------------- publishing
def _image_paths(post: dict, platform: str) -> list[str]:
    paths = []
    bg = post.get(f"{platform}_background")
    if bg:
        paths.append(str(config.BG_DIR / bg))
    for name in post.get("images") or []:
        paths.append(str(config.UPLOAD_DIR / name))
    return [p for p in paths if Path(p).is_file()]


def _recompute_status(post_id: int) -> dict:
    post = db.get_post(post_id)
    enabled = [p for p in ("vk", "tg") if post[f"{p}_enabled"]]
    statuses = [post[f"{p}_status"] for p in enabled]
    if enabled and all(s == "published" for s in statuses):
        db.update_post(post_id, {"status": "published", "published_at": post.get("published_at") or db.now_iso()})
    elif "error" in statuses or not enabled:
        db.update_post(post_id, {"status": "error"})
    return db.get_post(post_id)


def _ts(iso: str | None) -> int | None:
    if not iso:
        return None
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


VK_BROWSER_DELAY = 120  # «опубликовать сейчас» через браузер = отложка VK через 2 минуты


async def _publish_vk(post: dict, when_ts: int | None) -> dict:
    """VK: пост с картинками – через браузер в отложку VK (нужна сессия), без картинок – текст ключом."""
    paths = _image_paths(post, "vk")
    text = post["vk_text"] or ""
    if paths and vkweb.has_session():
        publish_at = max(int(when_ts or 0), int(time.time()) + VK_BROWSER_DELAY)
        plain, _ = vk.html_to_vk(text)
        res = await vkweb.manager.create_postponed(plain, paths, publish_at)
        pid = await vk.find_postponed(plain, publish_at)
        if pid:
            res["post_id"] = pid
            res["url"] = f"https://vk.com/wall-{config.VK_GROUP_ID}_{pid}"
            try:
                await vk.edit_postponed(pid, text, publish_at)   # дописываем жирный/ссылки через format_data
            except Exception as e:  # noqa: BLE001
                log.warning("VK: форматирование не применилось: %s", e)
        return res
    if paths:
        raise vk.VkError("У поста есть картинки, а сессии VK-браузера нет – войди через «VK-браузер» в шапке, иначе в VK уйдёт только текст")
    return await vk.publish(text, [])


async def handoff_vk(post_id: int, user: str) -> dict:
    """При планировании пост с картинками сразу уходит в отложку VK через браузер."""
    post = db.get_post(post_id)
    if not (post and post["vk_enabled"] and post["status"] == "scheduled" and post["vk_status"] != "published"
            and post.get("scheduled_at") and _image_paths(post, "vk") and vkweb.has_session()):
        return post
    try:
        res = await _publish_vk(post, _ts(post["scheduled_at"]))
        db.update_post(post_id, {"vk_status": "published", "vk_result": res, "vk_error": None})
        db.add_history(post_id, user, "publish", {"platform": "vk", "url": res.get("url"), "postponed": True})
    except Exception as e:  # noqa: BLE001
        log.exception("Пост %s: не удалось отдать в отложку VK", post_id)
        db.update_post(post_id, {"vk_status": "error", "vk_error": str(e)})
        db.add_history(post_id, user, "error", {"platform": "vk", "error": str(e)})
    return db.get_post(post_id)


async def _publish_tg(post: dict, when_ts: int | None) -> dict:
    """Telegram: если авторизован аккаунт – через него (умеет отложку на серверах Telegram), иначе бот."""
    text, paths, as_file = post["tg_text"] or "", _image_paths(post, "tg"), bool(post["tg_as_file"])
    if await tguser.manager.authorized():
        when = tguser.to_dt(when_ts) if when_ts and when_ts > time.time() + 30 else None
        return await tguser.manager.send(text, paths, as_file, when)
    return await tg.publish(text, paths, as_file)


async def handoff_tg(post_id: int, user: str) -> dict:
    """При планировании сразу отдаём пост в отложку Telegram (через аккаунт) – сервер сайта дальше не нужен."""
    post = db.get_post(post_id)
    if not (post and post["tg_enabled"] and post["status"] == "scheduled" and post["tg_status"] != "published"
            and post.get("scheduled_at")):
        return post
    if not await tguser.manager.authorized():
        return post  # аккаунта нет – опубликует наш планировщик через бота в назначенное время
    try:
        res = await _publish_tg(post, _ts(post["scheduled_at"]))
        db.update_post(post_id, {"tg_status": "published", "tg_result": res, "tg_error": None})
        db.add_history(post_id, user, "publish", {"platform": "tg", "scheduled": True})
    except Exception as e:  # noqa: BLE001
        log.exception("Пост %s: не удалось отдать в отложку Telegram", post_id)
        db.update_post(post_id, {"tg_status": "error", "tg_error": str(e)})
        db.add_history(post_id, user, "error", {"platform": "tg", "error": str(e)})
    return db.get_post(post_id)


async def resolve_tg(post_id: int) -> dict | None:
    """Отложка Telegram сработала – узнаём настоящие id сообщений (для правок и ссылки)."""
    post = db.get_post(post_id)
    res = (post or {}).get("tg_result") or {}
    if not (res.get("scheduled") and res.get("when") and res["when"] < time.time() - 45):
        return post
    try:
        found = await tguser.manager.resolve_published(res)
    except Exception as e:  # noqa: BLE001
        log.warning("Пост %s: не удалось найти опубликованное сообщение: %s", post_id, e)
        return post
    if found:
        db.update_post(post_id, {"tg_result": found})
        return _recompute_status(post_id)
    return post


async def publish_post(post_id: int, user: str | None = None) -> dict:
    if post_id in _in_progress:
        return db.get_post(post_id)
    _in_progress.add(post_id)
    try:
        post = db.get_post(post_id)
        if not post:
            raise HTTPException(404)
        for platform in ("vk", "tg"):
            if not post[f"{platform}_enabled"] or post[f"{platform}_status"] == "published":
                continue
            try:
                if platform == "vk":
                    res = await _publish_vk(post, None)
                else:
                    res = await _publish_tg(post, None)
                db.update_post(post_id, {f"{platform}_status": "published", f"{platform}_result": res, f"{platform}_error": None})
                db.add_history(post_id, user or "планировщик", "publish", {"platform": platform, "url": res.get("url")})
                log.info("Пост %s опубликован в %s: %s", post_id, platform, res)
            except Exception as e:  # noqa: BLE001
                log.exception("Пост %s: ошибка публикации в %s", post_id, platform)
                db.update_post(post_id, {f"{platform}_status": "error", f"{platform}_error": str(e)})
                db.add_history(post_id, user or "планировщик", "error", {"platform": platform, "error": str(e)})
        db.update_post(post_id, {"remote_dirty": False})
        return _recompute_status(post_id)
    finally:
        _in_progress.discard(post_id)


async def sync_post(post_id: int, user: str) -> dict:
    """Применить правки к уже опубликованному/отложенному посту на площадках."""
    if post_id in _in_progress:
        raise HTTPException(409, "Пост сейчас публикуется")
    _in_progress.add(post_id)
    try:
        await resolve_tg(post_id)
        post = db.get_post(post_id)
        errors = {}
        for platform in ("vk", "tg"):
            if not post[f"{platform}_enabled"] or post[f"{platform}_status"] != "published":
                continue
            result = post.get(f"{platform}_result") or {}
            try:
                if platform == "vk":
                    if result.get("postponed"):
                        if not result.get("post_id"):
                            raise vk.VkError("Не знаю id отложенной записи VK (ключу не дали wall.get) – поправь её в VK руками")
                        when = _ts(post["scheduled_at"]) if post["status"] == "scheduled" else None
                        res = await vk.edit_postponed(int(result["post_id"]), post["vk_text"] or "", when)
                        db.update_post(post_id, {"vk_result": {**result, **res}})
                    else:
                        await vk.edit(int(result["post_id"]), post["vk_text"] or "", [])
                elif result.get("scheduled"):
                    # ещё не вышло: снимаем с отложки и кладём заново с новым текстом/фото/временем
                    when = _ts(post["scheduled_at"]) if post["status"] == "scheduled" else result.get("when")
                    await tguser.manager.delete_scheduled(result)
                    res = await _publish_tg(post, when)
                    db.update_post(post_id, {"tg_result": res})
                elif result.get("via") == "user":
                    res = await tguser.manager.edit(result, post["tg_text"] or "", _image_paths(post, "tg"), bool(post["tg_as_file"]))
                    db.update_post(post_id, {"tg_result": res})
                else:
                    await tg.edit(result, post["tg_text"] or "", _image_paths(post, "tg"), bool(post["tg_as_file"]))
                db.update_post(post_id, {f"{platform}_error": None})
                db.add_history(post_id, user, "sync", {"platform": platform})
            except Exception as e:  # noqa: BLE001
                log.exception("Пост %s: ошибка правки в %s", post_id, platform)
                errors[platform] = str(e)
                db.update_post(post_id, {f"{platform}_error": f"Правка не применена: {e}"})
                db.add_history(post_id, user, "sync_error", {"platform": platform, "error": str(e)})
        if not errors:
            db.update_post(post_id, {"remote_dirty": False})
        post = db.get_post(post_id)
        post["sync_errors"] = errors
        return post
    finally:
        _in_progress.discard(post_id)


async def scheduler_loop():
    while True:
        try:
            for post in db.due_posts():
                log.info("Пора публиковать пост %s", post["id"])
                await publish_post(post["id"])
            for post in db.scheduled_tg_posts():
                await resolve_tg(post["id"])
        except Exception:  # noqa: BLE001
            log.exception("Ошибка планировщика")
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if db.users_count() == 0:
        if config.ADMIN_PASSWORD:
            db.create_user(config.ADMIN_LOGIN, config.ADMIN_PASSWORD, "head", None)
            log.info("Создан первый руковод: %s (из .env)", config.ADMIN_LOGIN)
        else:
            log.warning("Нет пользователей и ADMIN_PASSWORD пустой – задай его в .env!")
    await xray.manager.start()
    await tguser.manager.start()
    task = asyncio.create_task(scheduler_loop())
    yield
    task.cancel()
    await tguser.manager.stop()
    xray.manager.stop()


app = FastAPI(title="Crosspost", lifespan=lifespan)
api = APIRouter(prefix="/api", dependencies=[Depends(current_user)])


# ---------------------------------------------------------------- public routes
@app.post("/api/login")
async def login(request: Request, response: Response):
    body = await request.json()
    user = db.verify_user(str(body.get("login", "")).strip(), str(body.get("password", "")))
    if not user:
        raise HTTPException(401, "Неверный ник или пароль")
    response.set_cookie("session", _make_session(user["id"]), max_age=SESSION_TTL, httponly=True, samesite="lax")
    return {"ok": True, "user": user}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


# ---------------------------------------------------------------- me / users
@api.get("/me")
async def me(user: dict = Depends(current_user)):
    return user


@api.put("/me/password")
async def me_password(request: Request, user: dict = Depends(current_user)):
    body = await request.json()
    pw = str(body.get("password", ""))
    if len(pw) < 4:
        raise HTTPException(400, "Пароль не короче 4 символов")
    db.update_user(user["id"], password=pw)
    return {"ok": True}


@api.get("/users")
async def users_list(user: dict = Depends(require_head)):
    return db.list_users()


def _check_role_change(actor: dict, role: str, target: dict | None = None):
    if role not in db.ROLES:
        raise HTTPException(400, "Неизвестная роль")
    if actor["role"] != "head" and role == "head":
        raise HTTPException(403, "Назначать руковода может только руковод")
    if target and target["role"] == "head" and actor["role"] != "head":
        raise HTTPException(403, "Менять руковода может только руковод")


@api.post("/users")
async def users_create(request: Request, actor: dict = Depends(require_head)):
    body = await request.json()
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    role = str(body.get("role", "media"))
    if not USERNAME_RE.match(username):
        raise HTTPException(400, "Ник: 2–32 символа, буквы/цифры/точка/дефис/подчёркивание")
    if len(password) < 4:
        raise HTTPException(400, "Пароль не короче 4 символов")
    if db.get_user_by_name(username):
        raise HTTPException(409, "Такой ник уже есть")
    _check_role_change(actor, role)
    return db.create_user(username, password, role, actor["username"])


@api.put("/users/{user_id}")
async def users_update(user_id: int, request: Request, actor: dict = Depends(require_head)):
    target = db.get_user(user_id)
    if not target:
        raise HTTPException(404)
    body = await request.json()
    role = body.get("role")
    password = body.get("password")
    if role:
        _check_role_change(actor, role, target)
        if target["id"] == actor["id"] and role != actor["role"]:
            raise HTTPException(400, "Свою роль менять нельзя")
    if password is not None and len(str(password)) < 4:
        raise HTTPException(400, "Пароль не короче 4 символов")
    if target["role"] == "head" and actor["role"] != "head":
        raise HTTPException(403, "Менять руковода может только руковод")
    return db.update_user(user_id, password=password or None, role=role or None)


@api.delete("/users/{user_id}")
async def users_delete(user_id: int, actor: dict = Depends(require_head)):
    target = db.get_user(user_id)
    if not target:
        raise HTTPException(404)
    if target["id"] == actor["id"]:
        raise HTTPException(400, "Себя удалить нельзя")
    if target["role"] == "head" and actor["role"] != "head":
        raise HTTPException(403, "Удалять руковода может только руковод")
    db.delete_user(user_id)
    return {"ok": True}


# ---------------------------------------------------------------- targets
@api.get("/targets")
async def targets(refresh: bool = False):
    if not refresh and _targets_cache["data"] and time.time() - _targets_cache["ts"] < 600:
        return _targets_cache["data"]
    data = {
        "vk": {"configured": config.VK_CONFIGURED, "group_id": config.VK_GROUP_ID, "info": None, "error": None,
               "browser": {"session": vkweb.has_session(), "headed": vkweb.HEADED, "last_error": vkweb.manager.last_error, "last_shot": vkweb.manager.last_shot}},
        "tg": {
            "configured": config.TG_CONFIGURED, "chat_id": config.TG_CHAT_ID, "info": None, "bot": None, "error": None,
            "proxy": xray.tg_proxy_url(), "proxy_error": xray.manager.error, "vless": bool(config.TG_VLESS),
            "user": await tguser.manager.status(),
        },
    }
    if config.VK_CONFIGURED:
        try:
            data["vk"]["info"] = await vk.group_info()
        except Exception as e:  # noqa: BLE001
            data["vk"]["error"] = str(e)
    if config.TG_CONFIGURED:
        try:
            data["tg"]["bot"] = await tg.bot_info()
            data["tg"]["info"] = await tg.chat_info()
        except Exception as e:  # noqa: BLE001
            data["tg"]["error"] = str(e)
    _targets_cache.update(ts=time.time(), data=data)
    return data


@api.post("/vk/web/login")
async def vk_web_login(user: dict = Depends(require_head)):
    return vkweb.manager.start_login()


@api.get("/vk/web/login")
async def vk_web_login_status(user: dict = Depends(require_head)):
    st = dict(vkweb.manager.login_status)
    st["session"] = vkweb.has_session()
    if st.get("ok"):
        _targets_cache.update(ts=0, data=None)
    return st


@api.post("/vk/web/check")
async def vk_web_check(user: dict = Depends(require_head)):
    try:
        return await vkweb.manager.check_session()
    except vkweb.VkWebError as e:
        raise HTTPException(400, str(e))


@api.get("/vk/web/shots")
async def vk_web_shots(user: dict = Depends(require_head)):
    if not vkweb.DEBUG_DIR.is_dir():
        return []
    files = sorted(vkweb.DEBUG_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)[:12]
    return [p.name for p in files]


@api.get("/vk/web/shot/{name}")
async def vk_web_shot(name: str, user: dict = Depends(require_head)):
    if not SAFE_NAME.match(name) or not (vkweb.DEBUG_DIR / name).is_file():
        raise HTTPException(404)
    return FileResponse(vkweb.DEBUG_DIR / name)


@api.post("/tg/user/code")
async def tg_user_code(request: Request, user: dict = Depends(require_head)):
    body = await request.json()
    try:
        return await tguser.manager.send_code(str(body.get("phone", "")))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))


@api.post("/tg/user/signin")
async def tg_user_signin(request: Request, user: dict = Depends(require_head)):
    body = await request.json()
    try:
        res = await tguser.manager.sign_in(str(body.get("code", "")), body.get("password") or None)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))
    _targets_cache.update(ts=0, data=None)
    if res.get("ok"):
        db.add_history(0, user["username"], "tg_login", {"name": res.get("name")})
    return res


@api.post("/tg/user/logout")
async def tg_user_logout(user: dict = Depends(require_head)):
    await tguser.manager.logout()
    _targets_cache.update(ts=0, data=None)
    return {"ok": True}


@api.get("/tg/chats")
async def tg_chats():
    try:
        return await tg.find_chats()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))


# ---------------------------------------------------------------- posts
@api.get("/posts")
async def posts():
    return db.list_posts()


@api.get("/posts/{post_id}")
async def post_get(post_id: int):
    post = await resolve_tg(post_id) or db.get_post(post_id)
    if not post:
        raise HTTPException(404)
    return post


@api.get("/posts/{post_id}/history")
async def post_history(post_id: int):
    return db.post_history(post_id)


def _clean_post(body: dict) -> dict:
    data = {k: body.get(k) for k in db.COLUMNS if k in body}
    for k in ("vk_result", "tg_result", "vk_status", "tg_status", "vk_error", "tg_error",
              "published_at", "created_at", "updated_at", "created_by", "updated_by", "remote_dirty"):
        data.pop(k, None)
    if data.get("scheduled_at"):
        try:
            dt = datetime.fromisoformat(str(data["scheduled_at"]).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            data["scheduled_at"] = dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        except ValueError:
            raise HTTPException(400, "Некорректная дата")
    else:
        data["scheduled_at"] = None
    if data.get("status") not in ("draft", "scheduled"):
        data["status"] = "draft"
    if data["status"] == "scheduled" and not data.get("scheduled_at"):
        raise HTTPException(400, "Для отложки нужно указать время")
    data["images"] = [n for n in (data.get("images") or []) if SAFE_NAME.match(n)]
    # площадка включена, только если для неё есть текст
    if "vk_text" in data:
        data["vk_enabled"] = tg.plain_len(data.get("vk_text") or "") > 0
    if "tg_text" in data:
        data["tg_enabled"] = tg.plain_len(data.get("tg_text") or "") > 0
    for k in ("vk_background", "tg_background"):
        if data.get(k) and not SAFE_NAME.match(data[k]):
            data[k] = None
    return data


@api.post("/posts")
async def post_create(request: Request, user: dict = Depends(current_user)):
    data = _clean_post(await request.json())
    data["created_by"] = data["updated_by"] = user["username"]
    post = db.create_post(data)
    db.add_history(post["id"], user["username"], "create", {"status": post["status"]})
    await handoff_vk(post["id"], user["username"])
    return await handoff_tg(post["id"], user["username"])


@api.put("/posts/{post_id}")
async def post_update(post_id: int, request: Request, user: dict = Depends(current_user)):
    cur = db.get_post(post_id)
    if not cur:
        raise HTTPException(404)
    if not _can_edit_post(user, cur):
        raise HTTPException(403, "Можно править только свои посты")
    if post_id in _in_progress:
        raise HTTPException(409, "Пост сейчас публикуется")
    body = await request.json()
    data = _clean_post(body)

    published = any(cur[f"{p}_status"] == "published" for p in ("vk", "tg"))
    def _queued(pl):
        r = cur.get(f"{pl}_result") or {}
        return cur[f"{pl}_status"] == "published" and bool(r.get("scheduled") or (r.get("postponed") and r.get("publish_at", 0) > time.time()))
    all_queued = all(_queued(pl) or cur[f"{pl}_status"] != "published" for pl in ("vk", "tg"))
    if published and not all_queued:
        # у опубликованного поста статус не трогаем: он остаётся published, правки уходят через sync
        data.pop("status", None)
        data.pop("scheduled_at", None)

    changes = db.diff_post(cur, data)
    # Платформы с ошибкой сбрасываем в pending для повторной попытки; reset_* – принудительно.
    for p in ("vk", "tg"):
        if cur[f"{p}_status"] == "error" or body.get(f"reset_{p}"):
            data[f"{p}_status"] = "pending"
            data[f"{p}_error"] = None
            data[f"{p}_result"] = None
            changes[f"reset_{p}"] = [False, True]
    if not changes:
        return cur
    data["updated_by"] = user["username"]
    if published and any(k in db.CONTENT_FIELDS for k in changes):
        data["remote_dirty"] = True
    post = db.update_post(post_id, data)
    db.add_history(post_id, user["username"], "update", _short_changes(changes))
    await handoff_vk(post_id, user["username"])
    return await handoff_tg(post_id, user["username"])


def _short_changes(changes: dict) -> dict:
    out = {}
    for k, (a, b) in changes.items():
        cut = lambda v: (v[:300] + "…") if isinstance(v, str) and len(v) > 300 else v  # noqa: E731
        out[k] = [cut(a), cut(b)]
    return out


@api.delete("/posts/{post_id}")
async def post_delete(post_id: int, user: dict = Depends(current_user)):
    cur = db.get_post(post_id)
    if cur and not _can_edit_post(user, cur):
        raise HTTPException(403, "Можно удалять только свои посты")
    res = (cur or {}).get("tg_result") or {}
    if res.get("scheduled") and res.get("when", 0) > time.time():
        await tguser.manager.delete_scheduled(res)   # снимаем с отложки Telegram, чтобы не вышло
    vres = (cur or {}).get("vk_result") or {}
    if vres.get("postponed") and vres.get("post_id") and vres.get("publish_at", 0) > time.time():
        try:
            await vk.delete_post(int(vres["post_id"]))   # снимаем с отложки VK
        except Exception as e:  # noqa: BLE001
            log.warning("VK: не удалось удалить отложенную запись %s: %s", vres["post_id"], e)
    db.delete_post(post_id)
    return {"ok": True}


@api.post("/posts/{post_id}/publish")
async def post_publish(post_id: int, user: dict = Depends(current_user)):
    cur = db.get_post(post_id)
    if not cur:
        raise HTTPException(404)
    if not _can_edit_post(user, cur):
        raise HTTPException(403, "Можно публиковать только свои посты")
    return await publish_post(post_id, user["username"])


@api.post("/posts/{post_id}/sync")
async def post_sync(post_id: int, user: dict = Depends(current_user)):
    cur = db.get_post(post_id)
    if not cur:
        raise HTTPException(404)
    if not _can_edit_post(user, cur):
        raise HTTPException(403, "Можно править только свои посты")
    return await sync_post(post_id, user["username"])


# ---------------------------------------------------------------- files
async def _save_upload(file: UploadFile, folder: Path) -> str:
    """Файл пишется байт в байт – никакого пережатия на нашей стороне."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_IMG:
        raise HTTPException(400, "Только изображения: jpg, png, gif, webp")
    name = f"{uuid.uuid4().hex}{ext}"
    (folder / name).write_bytes(await file.read())
    return name


@api.post("/upload")
async def upload(file: UploadFile = File(...)):
    return {"name": await _save_upload(file, config.UPLOAD_DIR)}


@api.get("/backgrounds")
async def backgrounds():
    files = sorted(config.BG_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in files if p.suffix.lower() in ALLOWED_IMG]


@api.post("/backgrounds")
async def background_upload(file: UploadFile = File(...)):
    return {"name": await _save_upload(file, config.BG_DIR)}


@api.delete("/backgrounds/{name}")
async def background_delete(name: str, user: dict = Depends(require_manager)):
    if SAFE_NAME.match(name):
        (config.BG_DIR / name).unlink(missing_ok=True)
    return {"ok": True}


@api.get("/files/{kind}/{name}")
async def files(kind: str, name: str):
    folder = {"uploads": config.UPLOAD_DIR, "backgrounds": config.BG_DIR}.get(kind)
    if not folder or not SAFE_NAME.match(name) or not (folder / name).is_file():
        raise HTTPException(404)
    return FileResponse(folder / name)


app.include_router(api)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
