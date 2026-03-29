import asyncio
import json
import os
import re
from aiosteampy.client import SteamClient
from yarl import URL
import aiohttp
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, Browser, BrowserContext, TimeoutError as PlaywrightTimeoutError

try:
    from config import CONFIG_DATA
except ImportError:
    CONFIG_DATA = {"points_shop_protobufs": {}, "free_game_params": {}}
    print("Файл config.py не найден или не содержит CONFIG_DATA. Создаю временный пустой словарь.")

load_dotenv()

CONFIG_FILE_PATH = 'config.py'
MAFILES_DIR = "./maFiles"
URLS_FILE = "urls.txt"
SESSIONS_PATH = "./sessions"
os.makedirs(SESSIONS_PATH, exist_ok=True)

def save_session_cookies(username: str, cookies_dict: dict):
    """Сохраняет куки аккаунта в файл."""
    path = os.path.join(SESSIONS_PATH, f"{username}.json")

    serializable_cookies = {k: v.value for k, v in cookies_dict.items()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(serializable_cookies, f)

def load_session_cookies(username: str) -> dict | None:
    """Загружает куки из файла."""
    path = os.path.join(SESSIONS_PATH, f"{username}.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

async def is_session_alive(cookies: dict) -> bool:
    """Проверяет валидность сессии по реальным данным профиля."""
    if not cookies: return False

    clean_cookies = {k: (v.value if hasattr(v, 'value') else v) for k, v in cookies.items()}

    async with aiohttp.ClientSession(cookies=clean_cookies) as session:
        try:
            async with session.get("https://store.steampowered.com/dynamicstore/userdata/", timeout=10) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                return len(data.get("rgOwnedApps", [])) > 0
        except Exception as e:
            print(f"Ошибка при проверке userdata: {e}")
            return False

async def update_config_data_in_file(data: dict):
    """Атомарно обновляет CONFIG_DATA в config.py."""
    tmp_path = CONFIG_FILE_PATH + '.tmp'
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            f.write(f'CONFIG_DATA = {json.dumps(data, indent=4, ensure_ascii=False)}')

        os.replace(tmp_path, CONFIG_FILE_PATH)
        print("✅ Файл config.py успешно обновлен.")

    except Exception as e:
        print(f"❌ Ошибка при записи в config.py: {e}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

async def load_mafile(mafile_path: str):
    """Загружает данные mafile из указанного пути."""
    with open(mafile_path, "r", encoding="utf-8") as f:
        return json.load(f)

def normalize_steam_url(url: str) -> str:
    """Гарантирует наличие https:// в ссылке."""
    url = url.strip()
    return url if url.startswith("http") else f"https://{url}"

async def _parse_multipart_field(multipart_string: str, field_name: str) -> str | None:
    """
    Парсит строку multipart/form-data для извлечения значения конкретного поля.
    """
    pattern = rf'name="{re.escape(field_name)}"\r\n\r\n(.*?)\r\n(?:--|$)'
    match = re.search(pattern, multipart_string, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def _prepare_playwright_cookies(cookies: dict, initial_url: str) -> list[dict]:
    """Конвертация morsel-куков в формат Playwright."""
    prepared = []
    host = URL(initial_url).host
    for key, val in cookies.items():
        actual_value = val.value if hasattr(val, 'value') else val

        prepared.append({
            "name": key,
            "value": str(actual_value),
            "domain": host,
            "path": "/",
            "secure": True,
            "httpOnly": True if key in ['steamLoginSecure', 'sessionid'] else False,
            "sameSite": "Lax"
        })
    return prepared

async def _setup_playwright_page(cookies: dict, url: str, steamid: str) -> tuple[Page, Browser, BrowserContext]:
    """Инициализация браузера с внедрением куков."""
    url = normalize_steam_url(url)
    p = await async_playwright().start()
    browser = await p.chromium.launch(headless=False)
    context = await browser.new_context()

    await context.add_cookies(_prepare_playwright_cookies(cookies, url))
    page = await context.new_page()

    print(f"[{steamid}] Playwright: Перехожу на страницу: {url}...")
    await page.goto(url, wait_until="load", timeout=60000)
    return page, browser, context


async def _handle_age_verification(page: Page, steamid: str) -> bool:
    """Устанавливает возраст на странице проверки возраста, если она появляется."""
    if "/agecheck/app/" in page.url:
        print(f"[{steamid}] Playwright: Обнаружена страница проверки возраста. Устанавливаю дату рождения.")
        try:
            await page.select_option('#ageYear', '1999')
            await page.select_option('#ageMonth', 'January')
            await page.select_option('#ageDay', '1')

            old_url = page.url
            await page.click('#view_product_page_btn')
            print(f"[{steamid}] Playwright: Кнопка 'Открыть страницу' нажата. Ожидаю редирект...")
            await page.wait_for_function(f"window.location.href !== '{old_url}'", timeout=30000)
            await page.wait_for_load_state('load')
            return True
        except PlaywrightTimeoutError:
            print(
                f"[{steamid}] Playwright: Таймаут при обработке страницы проверки возраста. Возможно, не удалось перейти на страницу игры.")
            return False
        except Exception as e:
            print(f"[{steamid}] Playwright: Ошибка при обработке страницы проверки возраста: {e}")
            return False
    return False

async def _check_if_game_owned(page: Page, steamid: str):
    """Проверяет наличие игры."""
    owned_div = await page.query_selector("div.game_area_already_owned")
    if owned_div:
        print(f"[{steamid}] ℹ️ Игра уже есть в библиотеке. Пропускаю.")
        return True
    return False


async def _click_element(page: Page, selector: str, steamid: str, label: str) -> bool:
    """Универсальный клик по элементу."""
    try:
        btn = await page.query_selector(selector)
        if btn and await btn.is_visible():
            print(f"[{steamid}] ✅ Найдена {label}. Выполняю клик.")
            await btn.click()
            await asyncio.sleep(0.2)
            return True
        return False
    except Exception as e:
        print(f"[{steamid}] ❌ Ошибка при клике на {label}: {e}")
        return False

async def _wait_and_click(page: Page, selector: str, steamid: str, label: str, timeout: int = 2000) -> bool:
    """Ожидание элемента и последующий клик."""
    try:
        btn = await page.wait_for_selector(selector, timeout=timeout)
        if btn:
            print(f"[{steamid}] ✅ {label} появилась. Кликаю.")
            await btn.click()
            await asyncio.sleep(0.2)
            return True
    except PlaywrightTimeoutError:
        print(f"[{steamid}] ⚠️ {label} не появилась за {timeout}мс.")
    except Exception as e:
        print(f"[{steamid}] ❌ Ошибка ожидания {label}: {e}")
    return False

async def _attempt_to_close_any_modal(page: Page, steamid: str):
    """Попытка закрыть любое открытое модальное окно или оверлей."""
    close_selectors = [
        'button._3Ju8vy_foEPg9ILmy2-htb._1hcJa9ylImmFKuHsfilos.Focusable:has-text("Позже")',
        'button._3Ju8vy_foEPg9ILmy2-htb._1hcJa9ylImmFKuHsfilos.Focusable:has-text("Later")',
        'button[aria-label="Close"]',
        'div[class*="ModalPosition_TopBar"] button',
        'button:has-text("Отмена")',
        'button:has-text("Cancel")'
    ]

    for selector in close_selectors:
        if await _click_element(page, selector, steamid, f"кнопка закрытия ({selector[:20]}...)"):
            return True
    print(f"[{steamid}] Playwright: Кнопка закрытия/отмены не найдена.")
    return False

async def _handle_success_modal(page: Page, steamid: str) -> bool:
    """Ищет и закрывает модальное окно после успешного добавления игры."""
    modal_selector = 'div.newmodal_content_border'
    ok_button_selector = 'div.newmodal_buttons span:has-text("OK"), div.newmodal_buttons span:has-text("ОК")'
    try:
        if await page.wait_for_selector(modal_selector, timeout=2000):
            print(f"[{steamid}] ✅ Найдено модальное окно успеха. Пытаюсь закрыть его...")
            return await _wait_and_click(page, ok_button_selector, steamid, "кнопка OK")
    except:
        print(f"[{steamid}] Playwright: Модальное окно успеха не появилось в ожидаемое время.")
    return False

async def _check_and_click_add_button(page: Page, steamid: str) -> str | None:
    """
    Проверяет наличие и кликает на кнопку 'Добавить на аккаунт' или 'Add to Account'.
    Возвращает 'modal' если появляется модальное окно, 'redirect' если происходит переадресация,
    или None если кнопка не найдена.
    """
    # 1. Поиск кнопки "Добавить на аккаунт" / "Add to Account" (с href)
    add_to_account_selectors = (
        'div.game_area_purchase_game:not(.demo_above_purchase) '
        'a.btn_green_steamui:has(span:has-text("Добавить на аккаунт"))',
        'div.game_area_purchase_game:not(.demo_above_purchase) '
        'a.btn_green_steamui:has(span:has-text("Add to Account"))'
    )
    for s in add_to_account_selectors:
        if await _click_element(page, s, steamid, "кнопка добавления на аккаунт (редирект)"):
            return 'redirect'

    # 2. Поиск кнопки "Добавить в библиотеку" / "Add to Library" (с onclick)
    add_to_library_selectors = (
        'div.game_area_purchase_game:not(.demo_above_purchase) '
        'span.btn_blue_steamui:has(span:has-text("Добавить в библиотеку"))',
        'div.game_area_purchase_game:not(.demo_above_purchase) '
        'span.btn_blue_steamui:has(span:has-text("Add to Library"))'
    )
    for s in add_to_library_selectors:
        if await _click_element(page, s, steamid, "кнопка добавления в библиотеку (модалка)"):
            return 'modal'

    # 3. Поиск кнопки "Установить игру" / "Install Game" / "Загрузить" / "Download"
    install_game_selectors = (
        'a.btn_green_steamui.btn_medium[href^="javascript:addToCart"]:has(span:has-text("Установить игру"))',
        'a.btn_green_steamui.btn_medium[href^="javascript:addToCart"]:has(span:has-text("Install Game"))',
        'a.btn_green_steamui.btn_medium[href^="javascript:addToCart"]:has(span:has-text("Загрузить"))',
        'a.btn_green_steamui.btn_medium[href^="javascript:addToCart"]:has(span:has-text("Download"))'
    )

    for s in install_game_selectors:
        if await _click_element(page, s, steamid, "кнопка загрузки (редирект)"):
            return 'redirect'

    print(f"[{steamid}] Кнопка для добавления не найдена.")
    return None


async def collect_points_items(session: aiohttp.ClientSession, steamid: str, cookies: dict, shop_url: str,
                               access_token: str, global_config_data: dict):
    """
    Собирает бесплатные предметы за очки Steam.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36',
        'Origin': 'https://store.steampowered.com',
        'Referer': shop_url,
    }

    app_id_match = re.search(r'/app/(\d+)', shop_url)
    app_id = app_id_match.group(1) if app_id_match else "unknown_app"

    protobufs_for_app = global_config_data["points_shop_protobufs"].get(app_id)
    newly_collected_protobufs = []
    protobuf_ids_to_use = []

    if protobufs_for_app and len(protobufs_for_app) > 0:
        print(
            f"[{steamid}] Для AppID {app_id}: Использую уже собранные protobuf-идентификаторы для ускоренного выкупа. Идентификаторы: {protobufs_for_app}")
        protobuf_ids_to_use = protobufs_for_app
    else:
        print(f"[{steamid}] Для AppID {app_id}: Запущен проход (с Playwright) для сбора protobuf-идентификаторов.")
        browser = None
        context = None
        try:
            page, browser, context = await _setup_playwright_page(cookies, shop_url, steamid)

            async def route_handler(route):
                request = route.request
                url = request.url
                method = request.method

                if "ILoyaltyRewardsService/RedeemPoints/v1" in url and method == "POST":
                    post_data_str = request.post_data

                    if post_data_str:
                        redeem_protobuf = await _parse_multipart_field(post_data_str, "input_protobuf_encoded")

                        if redeem_protobuf and redeem_protobuf not in newly_collected_protobufs:
                            newly_collected_protobufs.append(redeem_protobuf)
                            print(f"[{steamid}] ✅ Playwright: Перехвачен Redeem Protobuf: {redeem_protobuf}")
                        elif not redeem_protobuf:
                            print(
                                f"[{steamid}] Playwright: Не удалось извлечь input_protobuf_encoded из post_data (multipart). Начало сырых данных: {post_data_str[:100]}...")

                await route.continue_()

            await page.route("**/api.steampowered.com/**", route_handler)

            await page.wait_for_selector('div.skI5tVFxF4zkY8z56LALc', timeout=30000)
            await asyncio.sleep(2)

            item_elements = await page.query_selector_all('div.skI5tVFxF4zkY8z56LALc')
            print(f"[{steamid}] Playwright: Найдено {len(item_elements)} потенциальных элементов предметов.")

            for i, item_el in enumerate(item_elements):
                print(f"[{steamid}] Playwright: --- Обработка предмета #{i + 1} ---")
                try:
                    price_element = await item_el.query_selector('div.BqFe2n5bs-NKOIO-N-o-P')

                    if price_element:
                        price_text = (await price_element.text_content() or "").strip()
                        print(
                            f"[{steamid}] Playwright: Отладка: price_element найден для предмета #{i + 1}. Текст: '{price_text}'")
                    else:
                        price_text = ""
                        print(f"[{steamid}] Playwright: Отладка: price_element НЕ найден для предмета #{i + 1}.")

                    is_free = False
                    if "Free" in price_text or "Бесплатно" in price_text:
                        is_free = True

                    if is_free:
                        print(
                            f"[{steamid}] Playwright: Найден бесплатный предмет #{i + 1}. Попытка кликнуть по элементу.")

                        await item_el.click()
                        print(f"[{steamid}] Playwright: Кликнул по элементу предмета.")

                        modal_container_selector = 'dialog._32QRvPPBL733SpNR9x0Gp3'
                        try:
                            modal_container = await page.wait_for_selector(modal_container_selector, timeout=10000)
                            print(
                                f"[{steamid}] Playwright: Главный контейнер модального окна появился (селектор: '{modal_container_selector}').")

                            modal_overlay_content_selector = 'div.ModalOverlayContent.active'
                            purchase_modal_content = await modal_container.wait_for_selector(
                                modal_overlay_content_selector, timeout=5000)
                            print(
                                f"[{steamid}] Playwright: Активное содержимое модального окна появилось (селектор: '{modal_overlay_content_selector}').")

                            free_purchase_button = await purchase_modal_content.query_selector(
                                'div[role="button"]._19X6AbdPOUHqSxNz3mm18i:has(div._2pwsWXANIuk8w8cZ8wvNz:has-text("Бесплатно")), div[role="button"]._19X6AbdPOUHqSxNz3mm18i:has(div._2pwsWXANIuk8w8cZ8wvNz:has-text("Free"))'
                            )

                            equip_now_button = await purchase_modal_content.query_selector(
                                'button.SRxqV4jytIuP55fxgfpD1._1hcJa9ylImmFKuHsfilos.Focusable:has-text("Использовать сейчас"), button.SRxqV4jytIuP55fxgfpD1._1hcJa9ylImmFKuHsfilos.Focusable:has-text("Equip now")'
                            )

                            later_button_in_modal = await purchase_modal_content.query_selector(
                                'button._3Ju8vy_foEPg9ILmy2-htb._1hcJa9ylImmFKuHsfilos.Focusable:has-text("Позже"), button._3Ju8vy_foEPg9ILmy2-htb._1hcJa9ylImmFKuHsfilos.Focusable:has-text("Later")'
                            )

                            if free_purchase_button and await free_purchase_button.is_visible():
                                print(
                                    f"[{steamid}] Playwright: Найдена кнопка 'Бесплатно' в модальном окне. Кликаю...")
                                await free_purchase_button.click()
                                await asyncio.sleep(0.5)

                                try:
                                    later_button_selector = 'button._3Ju8vy_foEPg9ILmy2-htb._1hcJa9ylImmFKuHsfilos.Focusable:has-text("Позже"), button._3Ju8vy_foEPg9ILmy2-htb._1hcJa9ylImmFKuHsfilos.Focusable:has-text("Later")'
                                    later_button = await page.wait_for_selector(later_button_selector, timeout=5000)
                                    if later_button and await later_button.is_visible():
                                        print(f"[{steamid}] Playwright: Найдена кнопка 'Позже'. Кликаю.")
                                        await later_button.click()
                                        print(
                                            f"[{steamid}] Playwright: ✅ Предмет #{i + 1} успешно куплен и модальное окно закрыто.")
                                        await asyncio.sleep(0.2)
                                    else:
                                        print(
                                            f"[{steamid}] Playwright: Кнопка 'Позже' не найдена или невидима после покупки. Попытка закрыть модальное окно.")
                                        await _attempt_to_close_any_modal(page, steamid)
                                except PlaywrightTimeoutError:
                                    print(
                                        f"[{steamid}] Playwright: Таймаут ожидания кнопки 'Позже' после покупки. Попытка закрыть модальное окно.")
                                    await _attempt_to_close_any_modal(page, steamid)
                                except Exception as e:
                                    print(
                                        f"[{steamid}] Playwright: Ошибка при обработке кнопки 'Позже' после покупки: {e}. Попытка закрыть модальное окно.")
                                    await _attempt_to_close_any_modal(page, steamid)

                            elif equip_now_button and await equip_now_button.is_visible():
                                print(
                                    f"[{steamid}] Playwright: Предмет #{i + 1} уже куплен (обнаружена кнопка 'Использовать сейчас'). Попытка закрыть модальное окно.")
                                await _attempt_to_close_any_modal(page, steamid)
                                print(
                                    f"[{steamid}] Playwright: ✅ Предмет #{i + 1} был уже куплен. Модальное окно закрыто.")

                            elif later_button_in_modal and await later_button_in_modal.is_visible():
                                print(
                                    f"[{steamid}] Playwright: Предмет #{i + 1} уже куплен (обнаружена кнопка 'Позже'). Попытка закрыть модальное окно.")
                                await later_button_in_modal.click()
                                await asyncio.sleep(0.2)
                                print(
                                    f"[{steamid}] Playwright: ✅ Предмет #{i + 1} был уже куплен. Модальное окно закрыто.")

                            else:
                                print(
                                    f"[{steamid}] Playwright: В модальном окне не найдена кнопка 'Бесплатно', 'Использовать сейчас' или 'Позже'. Возможно, произошла ошибка или неожиданное состояние.")
                                try:
                                    print(
                                        f"[{steamid}] Playwright: Отладка: Inner HTML содержимого модального окна (кнопки не найдены):")
                                    print(await purchase_modal_content.inner_html())
                                except Exception as debug_e:
                                    print(
                                        f"[{steamid}] Playwright: Отладка: Ошибка при получении innerHTML модального окна: {debug_e}")
                                await _attempt_to_close_any_modal(page, steamid)

                        except PlaywrightTimeoutError:
                            print(
                                f"[{steamid}] Playwright: Таймаут ожидания активного содержимого модального окна. Пропускаю этот предмет.")
                            await _attempt_to_close_any_modal(page, steamid)
                        except Exception as modal_e:
                            print(
                                f"[{steamid}] Playwright: Ошибка при работе с модальным окном (после клика по предмету): {modal_e}. Пропускаю этот предмет.")
                            await _attempt_to_close_any_modal(page, steamid)

                        await asyncio.sleep(0.5)
                    else:
                        print(
                            f"[{steamid}] Playwright: Предмет #{i + 1} не бесплатен (цена: '{price_text}'). Пропускаю.")
                except PlaywrightTimeoutError:
                    print(f"[{steamid}] Playwright: Таймаут при обработке предмета #{i + 1}. Пропускаю.")
                    await _attempt_to_close_any_modal(page, steamid)
                except Exception as e:
                    print(f"[{steamid}] Playwright: Ошибка при обработке предмета #{i + 1}: {e}")
                    await _attempt_to_close_any_modal(page, steamid)

        except PlaywrightTimeoutError as e:
            print(f"[{steamid}] Playwright: Таймаут при загрузке страницы: {e}. Пропускаю.")
        except Exception as e:
            print(f"[{steamid}] Playwright: Общая ошибка при работе Playwright: {e}. Пропускаю.")
        finally:
            if browser:
                await browser.close()
            if context:
                await context.close()

        if newly_collected_protobufs:
            global_config_data["points_shop_protobufs"][app_id] = newly_collected_protobufs
            await update_config_data_in_file(global_config_data)
            print(
                f"[{steamid}] Собрано и сохранено {len(newly_collected_protobufs)} новых protobuf-идентификаторов для AppID {app_id}.")

    if protobuf_ids_to_use:
        print(f"[{steamid}] Отладка: Окончательные protobuf_ids_to_use для AppID {app_id}: {protobuf_ids_to_use}")
        print(f"[{steamid}] Начинаю выкуп {len(protobuf_ids_to_use)} предметов для AppID {app_id}.")

        redeem_points_base_url = "https://api.steampowered.com/ILoyaltyRewardsService/RedeemPoints/v1/"
        for item_protobuf_id in protobuf_ids_to_use:
            redeem_points_url_with_token = f"{redeem_points_base_url}?access_token={access_token}"

            payload_redeem_points = {
                "input_protobuf_encoded": item_protobuf_id
            }

            print(f"[{steamid}] Попытка купить предмет (protobuf ID: {item_protobuf_id}) за очки...")
            try:
                async with session.post(redeem_points_url_with_token, headers=headers,
                                        data=payload_redeem_points) as redeem_resp:
                    response_bytes = await redeem_resp.read()

                    if redeem_resp.status == 200 and not response_bytes:
                        print(f"[{steamid}] 🎁 Успешно куплен предмет (protobuf ID: {item_protobuf_id}) за очки!")
                    elif redeem_resp.status == 200 and response_bytes:
                        print(
                            f"[{steamid}] ℹ️ Покупка завершена, сервер вернул бинарный ответ (вероятно, подтверждение): {response_bytes.hex()}")
                    else:
                        print(f"[{steamid}] ❌ Ошибка: статус {redeem_resp.status}, ответ: {response_bytes.hex()}")

            except Exception as e:
                print(f"[{steamid}] Ошибка при попытке купить предмет: {e}")
    return newly_collected_protobufs

async def claim_free_game(steamid: str, cookies: dict, url: str):
    """
    Пытается получить бесплатную игру, используя только Playwright для имитации действий пользователя.
    """
    print(f"[{steamid}] Попытка получить бесплатную игру по ссылке: {url}")

    browser = None
    try:
        page, browser, context = await _setup_playwright_page(cookies, url, steamid)

        await _handle_age_verification(page, steamid)

        if await _check_if_game_owned(page, steamid):
            return

        print(f"[{steamid}] Использую Playwright для имитации нажатия кнопки.")
        action_type = await _check_and_click_add_button(page, steamid)
        if action_type == 'modal':
            await _handle_success_modal(page, steamid)
        elif action_type == 'redirect':
            print(f"[{steamid}] ✅ Игра успешно добавлена (переадресация на страницу подтверждения).")

    except PlaywrightTimeoutError as e:
        print(f"[{steamid}] Playwright: Таймаут при загрузке страницы: {e}. Пропускаю.")
    except Exception as e:
        print(f"[{steamid}] Playwright: Общая ошибка при работе Playwright: {e}. Пропускаю.")
    finally:
        if browser:
            await browser.close()


async def get_steam_client(mafile_data: dict):
    """Авторизация через aiosteampy для получения сессии и токенов."""
    username = mafile_data["account_name"]
    password = os.getenv(f'STEAM_PASS_{username}')
    shared_secret = mafile_data.get("shared_secret")
    steam_id = mafile_data["Session"]["SteamID"]

    if not password:
        print(f"[{steam_id}] ❌ Пароль для аккаунта '{username}' не найден в .env. Пропуск авторизации.")
        return None

    print(f"[{steam_id}] Попытка авторизации aiosteampy для аккаунта '{username}'...")
    try:
        client = SteamClient(
            steam_id=steam_id,
            username=username,
            password=password,
            shared_secret=shared_secret
        )
        await client.login()
        print(f"[{steam_id}] ✅ aiosteampy авторизация успешна.")
        return client
    except Exception as e:
        print(
            f"[{steam_id}] ❌ Общая ошибка aiosteampy авторизации: {e}. Убедитесь, что пароль в .env верен и maFile актуален. Пропускаю этот аккаунт.")
        return None

async def run_for_account(mafile_path: str, urls: list[str], config_data: dict) -> bool:
    """Авторизуется в аккаунт и проходит по ссылкам."""
    mafile_data = await load_mafile(mafile_path)
    username = mafile_data["account_name"]
    steamid = mafile_data["Session"]["SteamID"]

    saved_cookies = load_session_cookies(username)
    client = None
    use_saved_session = False

    if saved_cookies:
        print(f"[{steamid}] 🔎 Найдена сохраненная сессия. Проверяю валидность...")
        if await is_session_alive(saved_cookies):
            print(f"[{steamid}] ✅ Сессия валидна. Пропускаю вход по 2FA.")
            use_saved_session = True
        else:
            print(f"[{steamid}] ❌ Сессия истекла.")

    if not use_saved_session:
        client = await get_steam_client(mafile_data)
        if client is None:
            return False

        current_cookies = client.session.cookie_jar.filter_cookies(URL("https://store.steampowered.com"))
        save_session_cookies(username, current_cookies)
        session = client.session
    else:
        session = aiohttp.ClientSession(cookies=saved_cookies)

    access_token = None
    cookies_from_client = session.cookie_jar.filter_cookies(URL("https://store.steampowered.com"))

    for cookie_name, morsel in cookies_from_client.items():
        if cookie_name == "steamLoginSecure":
            val = morsel.value if hasattr(morsel, 'value') else morsel
            if '%7C%7C' in val:
                access_token = val.split('%7C%7C')[1]
            elif '||' in val:
                access_token = val.split('||')[1]
            break

    try:
        if not access_token:
            print(f"[{steamid}] ❌ Не удалось получить access_token.")
            return False

        for url in urls:
            print(f"[{steamid}] Обработка URL: {url}")
            if 'store.steampowered.com/points/shop' in url:
                await collect_points_items(session, steamid, cookies_from_client, url, access_token, config_data)
            elif '/app/' in url:
                await claim_free_game(steamid, cookies_from_client, url)
            else:
                print(f"[{steamid}] ⚠️ Неподдерживаемый URL: {url}. Пропускаю.")

        return True
    except Exception as e:
        print(f"[{steamid}] Ошибка: {e}")
        return False
    finally:
        if use_saved_session:
            await session.close()
        elif client:
            await client.session.close()

async def main():
    """Основная функция для запуска скрипта."""
    if not os.path.exists(URLS_FILE):
        print("Создайте файл urls.txt со ссылками на бесплатные предметы (по одной ссылке на строку).")
        return

    with open(URLS_FILE, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]

    if not urls:
        print("Файл urls.txt пуст. Добавьте хотя бы одну ссылку на магазин очков Steam.")
        return

    if not os.path.exists(MAFILES_DIR):
        print(f"Создайте папку '{MAFILES_DIR}' и поместите туда свои .maFile.")
        return

    mafiles = [os.path.join(MAFILES_DIR, f) for f in os.listdir(MAFILES_DIR) if f.endswith(".maFile")]
    if not mafiles:
        print(f"В папке '{MAFILES_DIR}' не найдено .maFile.")
        return

    print(f"Найдено {len(mafiles)} аккаунтов и {len(urls)} URL для обработки.")

    global CONFIG_DATA

    failed_accounts = []

    print("\n--- Запуск обработки аккаунтов (последовательно) ---")
    for mafile in mafiles:
        success = await run_for_account(mafile, urls, CONFIG_DATA)
        if not success:
            failed_accounts.append(mafile)

    if failed_accounts:
        print("\n--- Аккаунты, требующие повторной попытки авторизации ---")
        for mafile_path in failed_accounts:
            mafile_data = await load_mafile(mafile_path)
            print(f"- Аккаунт '{mafile_data['account_name']}' ({mafile_path})")
        print("Пожалуйста, попробуйте запустить скрипт снова позже для этих аккаунтов.")
    else:
        print("\nВсе аккаунты обработаны успешно или не требуют повторной попытки.")

    print("\nОбработка завершена.")


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())  # Fixed errors appearing after accounts were processed
