"""VK через браузер (Playwright + Chromium): ключ сообщества не умеет фото, поэтому пост с картинками
создаётся в отложке VK так же, как это делает человек на vk.com – под аккаунтом админа.

Сессия: data/vk_web_state.json (cookies + localStorage), получается один раз через видимое окно на ПК
(«Открыть окно входа») и переносится на VPS простым копированием файла.
Каждый шаг делает скриншот в data/vk_debug/ – по ним чинятся селекторы, когда VK меняет вёрстку."""
import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

log = logging.getLogger("vkweb")

STATE = config.DATA_DIR / "vk_web_state.json"
DEBUG_DIR = config.DATA_DIR / "vk_debug"
HEADED = os.getenv("VK_WEB_HEADED", "").strip() in ("1", "true", "yes")
# VK – российский сайт, через VPN он часто не отвечает (ERR_TIMED_OUT). По умолчанию браузер идёт напрямую,
# минуя системный прокси. VK_WEB_PROXY=http://host:port – если наоборот нужен прокси.
PROXY = os.getenv("VK_WEB_PROXY", "").strip()
BASE = "https://vk.ru"
TZ = timezone(timedelta(hours=5))  # VK показывает время в часовом поясе аккаунта; у нас Челябинск
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


class VkWebError(Exception):
    pass


def has_session() -> bool:
    return STATE.is_file() and STATE.stat().st_size > 50


class VkWeb:
    def __init__(self):
        self._lock = asyncio.Lock()
        self.login_task: asyncio.Task | None = None
        self.login_status: dict = {"running": False, "ok": None, "message": ""}
        self.last_error: str | None = None
        self.last_shot: str | None = None

    # ---------------------------------------------------------------- инфраструктура
    async def _browser(self, headed: bool = False):
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        args = ["--disable-blink-features=AutomationControlled", "--lang=ru-RU", "--disable-quic"]  # QUIC/UDP через VPN виснет
        kwargs = {}
        if PROXY:
            kwargs["proxy"] = {"server": PROXY}
        else:
            args.append("--no-proxy-server")
        try:
            browser = await pw.chromium.launch(headless=not headed, args=args, **kwargs)
        except Exception as e:  # noqa: BLE001
            await pw.stop()
            raise VkWebError(f"Chromium не запустился: {e}. Выполни: python -m playwright install chromium") from e
        return pw, browser

    async def _context(self, browser, with_state: bool):
        kwargs = {"user_agent": UA, "locale": "ru-RU", "timezone_id": "Asia/Yekaterinburg", "viewport": {"width": 1280, "height": 900}}
        if with_state:
            if not has_session():
                raise VkWebError("Нет сессии VK-браузера – войди через «VK-браузер» в шапке (на ПК) или скопируй data/vk_web_state.json")
            kwargs["storage_state"] = str(STATE)
        return await browser.new_context(**kwargs)

    async def _shot(self, page, name: str) -> str:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        fn = f"{int(time.time())}-{name}.png"
        try:
            await page.screenshot(path=str(DEBUG_DIR / fn), full_page=False)
            self.last_shot = fn
        except Exception:  # noqa: BLE001
            pass
        return fn

    async def _goto(self, page, path: str, attempts: int = 4):
        """vk.ru напрямую (vk.com редиректит на него), с повторами: через VPN VK часто отвечает не с первого раза."""
        last = None
        for i in range(attempts):
            url = f"{BASE}{path}" if i % 2 == 0 else f"https://vk.com{path}"
            try:
                await page.goto(url, wait_until="commit", timeout=45000)
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
                await self._pass_challenge(page)
                return
            except Exception as e:  # noqa: BLE001
                last = e
                log.warning("VK-браузер: %s не открылся (попытка %s): %s", url, i + 1, str(e)[:120])
                await asyncio.sleep(2)
        raise VkWebError(f"vk.com не открывается из браузера ({str(last)[:80]}). Если VPN на всём ПК – добавь vk.com/vk.ru/userapi.com в исключения VPN")

    async def _pass_challenge(self, page):
        """VK при подозрении на бота показывает challenge.html (429). Обычно это JS-проверка,
        которая проходит сама за несколько секунд – ждём; если не прошла, говорим человеку."""
        if "challenge" not in page.url:
            return
        log.warning("VK-браузер: антибот-проверка VK (%s), жду", page.url[:80])
        for i in range(30):
            await asyncio.sleep(2)
            if i in (1, 6, 12):  # «Проверяем, что вы не робот» → «Продолжить»
                try:
                    btn = page.get_by_role("button", name=re.compile("Продолжить|Continue", re.I)).first
                    if await btn.count() and await btn.is_visible():
                        await btn.click()
                except Exception:  # noqa: BLE001
                    pass
            if "challenge" not in page.url:
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
                await asyncio.sleep(1)
                return
        fn = await self._shot(page, "challenge")
        raise VkWebError(
            f"VK показал антибот-проверку и не пропустил автоматически (скриншот data/vk_debug/{fn}). "
            f"Подожди 10–15 минут, не запускай подряд; если повторяется – войди на ПК с VK_WEB_HEADED=1, пройди проверку вручную и повтори"
        )

    @staticmethod
    async def _first(page, candidates: list, timeout: int = 4000):
        """Первый видимый локатор из списка кандидатов (селекторы или готовые локаторы)."""
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            for c in candidates:
                loc = page.locator(c).first if isinstance(c, str) else c.first
                try:
                    if await loc.count() and await loc.is_visible():
                        return loc
                except Exception:  # noqa: BLE001
                    continue
            await asyncio.sleep(0.3)
        return None

    # ---------------------------------------------------------------- сессия
    async def _logged_in(self, page) -> bool:
        # vk.com сейчас редиректит на vk.ru – куки могут быть на любом из доменов
        cookies = await page.context.cookies(["https://vk.com", "https://vk.ru"])
        return any(c["name"] == "remixsid" and c["value"] for c in cookies) and "/login" not in page.url

    async def check_session(self) -> dict:
        if not has_session():
            return {"ok": False, "message": "Сессии нет"}
        pw, browser = await self._browser()
        try:
            ctx = await self._context(browser, True)
            page = await ctx.new_page()
            await self._goto(page, "/feed")
            await asyncio.sleep(2)
            ok = await self._logged_in(page)
            name = None
            if ok:
                loc = await self._first(page, ["#top_profile_link", "[data-testid='top_profile_link']", "a[href^='/id'] img[alt]", "img.TopNavBtn__profileImg"], 3000)
                try:
                    name = (await loc.get_attribute("aria-label")) or (await loc.inner_text()) if loc else None
                except Exception:  # noqa: BLE001
                    name = None
            await self._shot(page, "check")
            return {"ok": ok, "name": (name or "").strip()[:60] or None, "message": "" if ok else "Сессия протухла – войди заново"}
        finally:
            await browser.close(); await pw.stop()

    def start_login(self) -> dict:
        """Видимое окно Chromium на этой машине: человек логинится, мы сохраняем состояние."""
        if self.login_task and not self.login_task.done():
            return self.login_status
        self.login_status = {"running": True, "ok": None, "message": "Открыл окно Chromium – войди в VK под админом группы"}
        self.login_task = asyncio.create_task(self._login_flow())
        return self.login_status

    async def _login_flow(self):
        try:
            pw, browser = await self._browser(headed=True)
        except VkWebError as e:
            self.login_status = {"running": False, "ok": False, "message": str(e)}
            return
        try:
            ctx = await self._context(browser, False)
            page = await ctx.new_page()
            await self._goto(page, "/login")
            deadline = time.time() + 600
            while time.time() < deadline:
                await asyncio.sleep(2)
                if page.is_closed():
                    break
                try:
                    if await self._logged_in(page):
                        await asyncio.sleep(3)
                        STATE.parent.mkdir(parents=True, exist_ok=True)
                        await ctx.storage_state(path=str(STATE))
                        self.login_status = {"running": False, "ok": True, "message": "Сессия сохранена в data/vk_web_state.json"}
                        return
                except Exception:  # noqa: BLE001
                    break
            self.login_status = {"running": False, "ok": False, "message": "Окно закрыто или вышло время (10 минут) – вход не завершён"}
        except Exception as e:  # noqa: BLE001
            self.login_status = {"running": False, "ok": False, "message": f"Ошибка входа: {e}"}
        finally:
            try:
                await browser.close(); await pw.stop()
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- отложенный пост
    async def create_postponed(self, text: str, image_paths: list[str], publish_at: int) -> dict:
        """Создаёт в группе отложенную запись с фото и текстом на время publish_at (unix)."""
        async with self._lock:
            pw, browser = await self._browser(headed=HEADED)
            try:
                ctx = await self._context(browser, True)
                page = await ctx.new_page()
                return await self._compose(page, text, image_paths, publish_at)
            finally:
                await browser.close(); await pw.stop()

    async def _dump(self, page, name: str) -> None:
        """Список кнопок/полей на странице – чтобы чинить селекторы по отчёту, не заходя в VK заново."""
        js = r"""() => [...document.querySelectorAll('button, a[role=button], [role=menuitem], [role=option], [role=tab], [data-testid], [contenteditable=true], input, select, textarea, label')]
              .filter(e => e.offsetParent !== null || e.type === 'file')
              .map(e => [e.tagName, e.getAttribute('data-testid')||'', e.getAttribute('aria-label')||'', e.id||'', e.type||'', e.getAttribute('placeholder')||'', e.getAttribute('accept')||'', (e.innerText||e.value||'').trim().slice(0,60).replace(/[\s]+/g,' ')].join(' | '))
              .slice(0, 600).join(String.fromCharCode(10))"""
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            els = await page.evaluate(js)
            (DEBUG_DIR / f"{name}.txt").write_text(els, encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            log.warning("dump failed: %s", e)

    async def _fail(self, page, step: str, what: str):
        fn = await self._shot(page, f"fail-{step}")
        await self._dump(page, fn[:-4])
        self.last_error = f"{what} (шаг {step}). Скриншот: data/vk_debug/{fn}"
        raise VkWebError(self.last_error)

    async def _compose(self, page, text: str, image_paths: list[str], publish_at: int) -> dict:
        gid = config.VK_GROUP_ID
        await self._goto(page, f"/club{gid}")
        await asyncio.sleep(2)
        if not await self._logged_in(page):
            await self._fail(page, "login", "Сессия VK протухла – войди заново")
        await self._shot(page, "01-group")

        # 1. открыть композер. Новый дизайн: блок «Создать» внизу → меню → «Пост». Старый: поле «Что у Вас нового?»
        create = await self._first(page, [
            "[data-testid='group_publish_block'] button", page.get_by_role("button", name=re.compile(r"^\s*Создать\s*$")),
            page.get_by_text(re.compile(r"^\s*Создать\s*$")),
        ], 8000)
        opener = None
        if create:
            await create.scroll_into_view_if_needed()
            await asyncio.sleep(0.5)
            await create.click()
            await asyncio.sleep(1.2)
            item = await self._first(page, [
                page.get_by_role("menuitem", name=re.compile(r"^\s*Пост\s*$")), page.get_by_text(re.compile(r"^\s*Пост\s*$")),
                "[data-testid='group_publish_menu_post']",
            ], 5000)
            if not item:
                await self._fail(page, "create-menu", "Нажал «Создать», но пункта «Пост» в меню нет")
            await item.click()
            await asyncio.sleep(2)
        else:
            opener = await self._first(page, [
                "#post_field", "[data-testid='posting_input']",
                page.get_by_text(re.compile(r"Что у Вас нового|Напишите что-нибудь|Что нового", re.I)),
            ], 3000)
            if not opener:
                await self._fail(page, "composer", "Не нашёл ни блок «Создать», ни поле «Что у Вас нового?» на странице группы")
            await opener.click()
            await asyncio.sleep(1)
        field = await self._first(page, [
            "[data-testid='posting_input'] [contenteditable='true']", "[data-testid='posting_root'] [contenteditable='true']",
            "[role='dialog'] [contenteditable='true']", "#post_field[contenteditable='true']", "[contenteditable='true']",
        ], 8000)
        if not field:
            await self._fail(page, "field", "Окно создания поста открылось, но поле ввода текста не нашлось")
        await self._shot(page, "02-composer")
        await self._dump(page, "02-composer")

        dialog = page.locator("[data-testid='posting_modal_box']").first
        if not await dialog.count():
            dialog = page.locator("[role='dialog']").last if await page.locator("[role='dialog']").count() else page

        # 2. фото: «Загрузить с устройства» – это label над скрытым input, кладём файлы прямо в него
        if image_paths:
            inp = page.locator("input[data-testid='posting_base_screen_download_from_device']")
            if not await inp.count():
                inp = dialog.locator("input[type='file'][accept*='image']")
            if not await inp.count():
                await self._fail(page, "photo-input", "Не нашёл поле загрузки фото в окне поста")
            await inp.first.set_input_files(image_paths)
            # ждём превью внутри окна: после загрузки появляется кнопка «Фото/Видео» и картинка
            ok = None
            deadline = time.time() + 120
            while time.time() < deadline and not ok:
                await asyncio.sleep(1)
                if await dialog.get_by_text(re.compile(r"Фото/Видео", re.I)).count():
                    imgs = dialog.locator("img")
                    for i in range(await imgs.count()):
                        try:
                            src = (await imgs.nth(i).get_attribute("src")) or ""
                            if ("userapi" in src or "vkuser" in src or src.startswith("blob:") or src.startswith("data:")) and await imgs.nth(i).is_visible():
                                ok = True
                                break
                        except Exception:  # noqa: BLE001
                            continue
            if not ok:
                await self._fail(page, "photo-preview", "Фото не появилось в окне поста за 2 минуты")
            await asyncio.sleep(2)
            await self._shot(page, "03-photos")

        # 3. текст: «Напишите что-нибудь…»
        if text.strip():
            fld = await self._first(page, [
                "[data-testid^='posting_base_screen_input_message'][contenteditable='true']",
                "[data-testid='posting_modal_box'] [contenteditable='true']", dialog.locator("[contenteditable='true']"),
                dialog.get_by_text(re.compile("Напишите что-нибудь", re.I)),
            ], 5000)
            if not fld:
                await self._fail(page, "text", "Не нашёл поле текста в окне поста")
            await fld.click()
            await page.keyboard.type(text, delay=5)
            await asyncio.sleep(0.5)
        await self._shot(page, "04-text")

        # 4. «Далее» → «Настройки»
        nxt = await self._first(page, ["[data-testid='posting_base_screen_next']", dialog.get_by_role("button", name=re.compile(r"^\s*Далее\s*$"))], 5000)
        if not nxt:
            await self._fail(page, "next", "Не нашёл кнопку «Далее»")
        await nxt.click()
        await asyncio.sleep(2)
        await self._shot(page, "05-settings")
        await self._dump(page, "05-settings")

        # 5. «Запланировать» → календарь
        when = datetime.fromtimestamp(publish_at, tz=TZ)
        plan = await self._first(page, [
            dialog.get_by_role("button", name=re.compile(r"Запланировать", re.I)), dialog.get_by_text(re.compile(r"^\s*Запланировать\s*$", re.I)),
            "[data-testid*='schedule']", "[data-testid*='postpone']",
        ], 6000)
        if not plan:
            await self._fail(page, "plan", "Не нашёл кнопку «Запланировать» на экране «Настройки»")
        await plan.click()
        await asyncio.sleep(1.5)
        await self._shot(page, "06-calendar")
        await self._dump(page, "06-calendar")
        await self._set_datetime(page, when)
        await asyncio.sleep(1)
        await self._shot(page, "07-datetime")

        # 6. закрыть открытую выпадашку минут (клик по заголовку окна), затем «Добавить в очередь»
        try:
            title = dialog.get_by_text(re.compile(r"^\s*Настройки\s*$")).first
            if await title.count():
                await title.click()
            else:
                await page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.6)
        modal = page.locator("[data-testid='posting_modal_box']")
        for attempt in range(3):
            submit = await self._first(page, [
                "[data-testid='posting_settings_submit_button']", "[data-testid='posting_settings_submit']",
                page.get_by_role("button", name=re.compile(r"Добавить в очередь|В очередь", re.I)), page.get_by_text(re.compile(r"Добавить в очередь", re.I)),
            ], 5000)
            if not submit:
                await self._fail(page, "submit", "Не нашёл кнопку «Добавить в очередь»")
            await submit.click()
            # успех = окно поста закрылось
            for _ in range(20):
                await asyncio.sleep(1)
                if not await modal.count() or not await modal.first.is_visible():
                    break
            else:
                err = await self._first(page, [page.get_by_text(re.compile("ошибка|не удалось|слишком|нельзя", re.I))], 800)
                if err:
                    try:
                        msg = (await err.inner_text()).strip()[:200]
                    except Exception:  # noqa: BLE001
                        msg = "VK показал ошибку"
                    await self._fail(page, "vk-error", msg)
                await self._shot(page, f"08-retry{attempt}")
                continue
            break
        else:
            await self._fail(page, "submit-stuck", "Нажал «Добавить в очередь», но окно поста не закрылось – VK не принял запись")
        await asyncio.sleep(2)
        await self._shot(page, "08-done")
        await self._dump(page, "08-done")
        return {"postponed": True, "publish_at": publish_at, "via": "browser", "url": f"https://vk.com/club{gid}?act=postponed"}

    MONTHS = ["январ", "феврал", "март", "апрел", "ма[йя]", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр"]

    async def _pick_select(self, page, sel, want_label: str | None = None, want_index: int | None = None) -> bool:
        """Выбрать значение в <select> или в кастомном селекте VKUI (клик → пункт списка)."""
        try:
            tag = (await sel.evaluate("e => e.tagName")).upper()
            if tag == "SELECT":
                if want_label is not None:
                    opts = await sel.locator("option").all_inner_texts()
                    for i, o in enumerate(opts):
                        if re.search(want_label, o.strip(), re.I):
                            await sel.select_option(index=i)
                            return True
                    return False
                await sel.select_option(index=want_index)
                return True
            await sel.click()
            await asyncio.sleep(0.5)
            opts = page.locator("[role='option'], [class*='CustomSelectOption']")
            n = await opts.count()
            for i in range(n):
                o = opts.nth(i)
                t = (await o.inner_text()).strip()
                if (want_label is not None and re.search(want_label, t, re.I)) or (want_index is not None and i == want_index):
                    await o.click()
                    await asyncio.sleep(0.3)
                    return True
            await page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        return False

    GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]

    async def _type_value(self, page, inp, value: str) -> bool:
        """Поле-выпадашка VKUI: кликаем, стираем, печатаем, выбираем совпавший пункт (или Enter), проверяем."""
        try:
            await inp.click()
            await asyncio.sleep(0.2)
            await page.keyboard.press("Control+A")
            await page.keyboard.type(value, delay=40)
            await asyncio.sleep(0.4)
            opt = page.locator("[role='option']").filter(has_text=re.compile(rf"^\s*{re.escape(value)}\s*$"))
            if await opt.count() and await opt.first.is_visible():
                await opt.first.click()
            else:
                await page.keyboard.press("Enter")
            await asyncio.sleep(0.3)
            got = (await inp.input_value()).strip()
            if got.lstrip("0") == value.lstrip("0") or got == value:
                return True
            await page.keyboard.press("Tab")
            await asyncio.sleep(0.2)
            got = (await inp.input_value()).strip()
            return got.lstrip("0") == value.lstrip("0") or got == value
        except Exception:  # noqa: BLE001
            return False

    async def _set_datetime(self, page, when: datetime):
        """Календарь VK (posting_postponed_calendar): листаем месяцы стрелкой, кликаем день, печатаем часы и минуты."""
        cal = page.locator("[data-testid='posting_postponed_calendar']")
        if not await cal.count():
            await self._set_datetime_generic(page, when)
            return
        month_in = cal.locator("[data-testid='posting_postponed_calendar_month_dropdown']")
        year_in = cal.locator("[data-testid='posting_postponed_calendar_year_dropdown']")
        nxt = cal.locator("[data-testid='posting_postponed_calendar_next_month']")
        # месяц/год: шагаем вперёд, пока заголовок не совпадёт
        for _ in range(24):
            try:
                m = (await month_in.input_value()).strip().lower()
                y = int((await year_in.input_value()).strip() or 0)
            except Exception:  # noqa: BLE001
                break
            mi = next((i + 1 for i, name in enumerate(self.MONTHS) if re.match(name, m, re.I)), None)
            if mi == when.month and y == when.year:
                break
            if (y, mi or 0) > (when.year, when.month):
                await self._fail(page, "month", f"Календарь VK показывает {m} {y}, а нужен более ранний месяц")
            await nxt.click()
            await asyncio.sleep(0.4)
        # день
        cell = cal.locator("[data-testid='posting_postponed_calendar_day']").filter(has_text=re.compile(rf",\s*{when.day}\s+{self.GEN[when.month - 1]}"))
        if not await cell.count():
            await self._fail(page, "day", f"Не нашёл день {when.day} {self.GEN[when.month - 1]} в календаре")
        await cell.first.click()
        await asyncio.sleep(0.4)
        # часы и минуты
        hours = cal.locator("[data-testid='posting_postponed_calendar_hours']")
        minutes = cal.locator("[data-testid='posting_postponed_calendar_minutes']")
        h_ok = await self._type_value(page, hours, f"{when.hour:02d}")
        m_ok = await self._type_value(page, minutes, f"{when.minute:02d}")
        if not (h_ok and m_ok):
            await self._fail(page, "time", f"Не выставил время {when:%H:%M} (часы: {h_ok}, минуты: {m_ok})")

    async def _set_datetime_generic(self, page, when: datetime):
        """Запасной вариант, если у календаря нет data-testid: input[type=date/time] или селекты."""
        d_in = await self._first(page, ["input[type='date']"], 1000)
        t_in = await self._first(page, ["input[type='time']"], 800)
        if d_in and t_in:
            await d_in.fill(when.strftime("%Y-%m-%d"))
            await t_in.fill(when.strftime("%H:%M"))
            await page.keyboard.press("Tab")
            return
        await self._fail(page, "datetime", "Календарь VK без знакомых полей – нужен новый дамп")


manager = VkWeb()
