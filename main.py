import asyncio
import json
import os
import re
from aiosteampy.client import SteamClient
from bs4 import BeautifulSoup
from yarl import URL
import aiohttp
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, Browser, BrowserContext, TimeoutError as PlaywrightTimeoutError

# Импортируем CONFIG_DATA из config.py
# Убедитесь, что у вас есть файл config.py с пустым словарем CONFIG_DATA = {"points_shop_protobufs": {}, "free_game_params": {}}
try:
    from config import CONFIG_DATA
except ImportError:
    CONFIG_DATA = {"points_shop_protobufs": {}, "free_game_params": {}}
    print("Файл config.py не найден или не содержит CONFIG_DATA. Создаю временный пустой словарь.")

load_dotenv()

# Путь к файлу config.py для записи
CONFIG_FILE_PATH = 'config.py'


async def load_mafile(mafile_path: str):
    """Загружает данные mafile из указанного пути."""
    with open(mafile_path, "r", encoding="utf-8") as f:
        return json.load(f)


async def get_steam_client(mafile_data: dict):
    """Инициализирует и возвращает объект SteamClient, а также выполняет авторизацию."""
    username = mafile_data["account_name"]
    password = os.getenv(f'STEAM_PASS_{username}')
    shared_secret = mafile_data.get("shared_secret")
    steam_id = mafile_data["Session"]["SteamID"]

    if not password:
        raise RuntimeError(f"Пароль для аккаунта '{username}' не найден в .env")

    client = SteamClient(
        steam_id=steam_id,
        username=username,
        password=password,
        shared_secret=shared_secret
    )

    print(f"[{steam_id}] Попытка авторизации aiosteampy для аккаунта '{username}'...")
    try:
        await client.login()
        print(f"[{steam_id}] ✅ aiosteampy авторизация успешна.")
    except Exception as e:
        print(
            f"[{steam_id}] ❌ Ошибка aiosteampy авторизации: {e}. Убедитесь, что пароль в .env верен и maFile актуален.")
        raise  # Пробрасываем ошибку, так как без авторизации дальше нет смысла

    return client


async def update_config_data_in_file(config_data: dict):
    """Обновляет словарь CONFIG_DATA в config.py."""
    print(f"Обновляю {CONFIG_FILE_PATH} с новыми данными.")
    try:
        # Полностью перезаписываем файл, чтобы избежать дублирования
        with open(CONFIG_FILE_PATH, 'w', encoding='utf-8') as f:
            f.write(f"CONFIG_DATA = {json.dumps(config_data, indent=4)}\n")
        print(f"✅ {CONFIG_FILE_PATH} успешно обновлен.")
    except Exception as e:
        print(f"❌ Ошибка при обновлении {CONFIG_FILE_PATH}: {e}")


async def _setup_playwright_page(cookies: dict, initial_url: str, steamid: str) -> tuple[Page, Browser, BrowserContext]:
    """
    Настраивает Playwright, создает контекст, внедряет куки и переходит на начальный URL.
    Возвращает объект страницы, браузера и контекста.
    """
    # ИСПРАВЛЕНО: Правильное использование async_playwright() с async with
    p = await async_playwright().start()  # Запускаем Playwright
    browser = await p.chromium.launch(headless=False)
    context = await browser.new_context()

    playwright_cookies = []
    for morsel in cookies.values():
        cookie_dict = {
            "name": morsel.key,
            "value": morsel.value,
            "httpOnly": "httponly" in morsel,
            "secure": "secure" in morsel,
        }
        same_site_value = morsel.get("samesite")
        if same_site_value:
            same_site_value = same_site_value.capitalize()
            if same_site_value not in ["Strict", "Lax", "None"]:
                same_site_value = "Lax"
        else:
            same_site_value = "Lax"
        cookie_dict["sameSite"] = same_site_value

        morsel_domain = morsel.get("domain")
        morsel_path = morsel.get("path")

        if morsel_domain and morsel_domain.startswith('.'):
            effective_domain = morsel_domain.lstrip('.')
            cookie_dict["url"] = f"https://{effective_domain}{morsel_path if morsel_path else '/'}"
        elif morsel_domain and morsel_path:
            cookie_dict["domain"] = morsel_domain
            cookie_dict["path"] = morsel_path
        else:
            cookie_dict["domain"] = URL(initial_url).host
            cookie_dict["path"] = "/"
        playwright_cookies.append(cookie_dict)
    await context.add_cookies(playwright_cookies)

    page = await context.new_page()
    print(f"[{steamid}] Playwright: Перехожу на страницу: {initial_url}...")
    await page.goto(initial_url, wait_until="load", timeout=60000)
    return page, browser, context


async def _attempt_to_close_any_modal(page: Page, steamid: str):
    """Попытка закрыть любое открытое модальное окно или оверлей."""
    print(f"[{steamid}] Playwright: Попытка закрыть любое открытое модальное окно.")
    try:
        # Приоритет кнопки "Позже" / "Later", затем общие кнопки закрытия
        close_button_selectors = [
            'button._3Ju8vy_foEPg9ILmy2-htb._1hcJa9ylImmFKuHsfilos.Focusable:has-text("Позже")',
            'button._3Ju8vy_foEPg9ILmy2-htb._1hcJa9ylImmFKuHsfilos.Focusable:has-text("Later")',
            'button[aria-label="Close"]',  # Общая кнопка закрытия для диалогов
            'div[class*="ModalPosition_TopBar"] button',  # Кнопка закрытия в верхней панели
            'button:has-text("Отмена")',
            'button:has-text("Cancel")'
        ]

        for selector in close_button_selectors:
            close_button = await page.query_selector(selector)
            if close_button and await close_button.is_visible():
                print(f"[{steamid}] Playwright: Найдена кнопка закрытия/отмены по селектору '{selector}'. Кликаю.")
                await close_button.click()
                await asyncio.sleep(0.2)  # Уменьшенная задержка
                return True  # Успешно закрыли
        print(f"[{steamid}] Playwright: Кнопка закрытия/отмены не найдена.")
        return False
    except Exception as e:
        print(f"[{steamid}] Playwright: Ошибка при попытке закрыть модальное окно: {e}")
        return False


async def _parse_multipart_field(multipart_string: str, field_name: str) -> str | None:
    """
    Парсит строку multipart/form-data для извлечения значения конкретного поля.
    """
    pattern = rf'name="{re.escape(field_name)}"\r\n\r\n(.*?)\r\n(?:--|$)'
    match = re.search(pattern, multipart_string, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


async def _handle_age_verification(page: Page, steamid: str) -> bool:
    """
    Обрабатывает страницу проверки возраста, если она появляется.
    Возвращает True, если проверка возраста была пройдена и страница перенаправлена, False иначе.
    """
    if "/agecheck/app/" in page.url:
        print(f"[{steamid}] Playwright: Обнаружена страница проверки возраста. Устанавливаю дату рождения.")
        try:
            # Устанавливаем год рождения (например, 1999)
            await page.select_option('#ageYear', '1999')
            # Можно также установить день и месяц, но для обхода достаточно года
            await page.select_option('#ageMonth', 'January')
            await page.select_option('#ageDay', '1')

            # Нажимаем кнопку "Открыть страницу"
            await page.click('#view_product_page_btn')
            print(
                f"[{steamid}] Playwright: Дата рождения установлена, кнопка 'Открыть страницу' нажата. Ожидаю редирект.")
            # Ждем, пока страница загрузится после редиректа
            await page.wait_for_load_state('load', timeout=30000)
            return True
        except PlaywrightTimeoutError:
            print(
                f"[{steamid}] Playwright: Таймаут при обработке страницы проверки возраста. Возможно, не удалось перейти на страницу игры.")
            return False
        except Exception as e:
            print(f"[{steamid}] Playwright: Ошибка при обработке страницы проверки возраста: {e}")
            return False
    return False


async def _check_if_game_owned(page: Page, steamid: str) -> bool:
    """
    Проверяет, куплена ли игра, ища кнопки "Играть".
    Возвращает True, если игра куплена, False в противном случае.
    """
    play_button_selectors = [
        'div.game_area_already_owned_btn a:has(span:has-text("Играть"))',
        'div.game_area_already_owned_btn a:has(span:has-text("Play"))',
        'div.btn_addtocart a[href*="steam://run/"]:has(span:has-text("Играть"))',
        'div.btn_addtocart a[href*="steam://run/"]:has(span:has-text("Play"))'
    ]

    for selector in play_button_selectors:
        play_button = await page.query_selector(selector)
        if play_button and await play_button.is_visible():
            print(f"[{steamid}] ✅ Игра уже на аккаунте (найдена кнопка 'Играть').")
            return True
    return False


async def _send_game_claim_request(page: Page, payload: dict, steamid: str, referer_url: str) -> bool:
    """
    Отправляет POST-запрос на добавление бесплатной лицензии игры.
    Возвращает True в случае успеха, False в случае ошибки.
    """
    ADD_LICENSE_URL = "https://store.steampowered.com/freelicense/addfreelicense/"
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
        'Referer': referer_url,
        'Accept': '*/*',
        'X-Requested-With': 'XMLHttpRequest',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
    }

    print(
        f"[{steamid}] Отправляю POST-запрос на {ADD_LICENSE_URL} с subid: {payload.get('subid')} и payload: {payload}...")
    try:
        resp = await page.request.post(ADD_LICENSE_URL, form=payload, headers=HEADERS)

        if resp.status == 200:
            print(f"[{steamid}] ✅ Бесплатная игра (subid: {payload.get('subid')}) успешно добавлена на аккаунт!")
            try:
                response_json = await resp.json()
                if response_json.get('success') != 1:  # Если это JSON, проверяем на явную ошибку
                    print(f"[{steamid}] ⚠️ Сервер вернул JSON, но с неуспешным статусом: {response_json}")
            except json.JSONDecodeError:
                print(f"[{steamid}] ℹ️ Сервер вернул не-JSON ответ. Считаем успешным на основе статуса 200.")
            return True
        else:
            print(
                f"[{steamid}] ❌ Ошибка: Неуспешный HTTP-статус: {resp.status} ({resp.status_text}) при добавлении игры.")
            return False
    except Exception as e:
        print(f"[{steamid}] Ошибка при отправке POST-запроса для добавления игры: {e}")
        return False


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
    protobuf_ids_to_use = []  # Инициализация для использования в выкупе

    if protobufs_for_app and len(protobufs_for_app) > 0:
        print(
            f"[{steamid}] Для AppID {app_id}: Использую уже собранные protobuf-идентификаторы для ускоренного выкупа. Идентификаторы: {protobufs_for_app}")
        protobuf_ids_to_use = protobufs_for_app
    else:
        print(f"[{steamid}] Для AppID {app_id}: Запущен проход (с Playwright) для сбора protobuf-идентификаторов.")
        newly_collected_protobufs = []  # Для сбора в текущем Playwright запуске
        browser = None  # Инициализация для finally
        context = None  # Инициализация context для finally
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
                                print(f"[{steamid}] Playwright: Найдена кнопка 'Бесплатно' в модальном окне. Кликаю...")
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
            if context:  # Закрываем контекст, если он был создан
                await context.close()

        # Если были собраны новые protobufs, сохраняем их
        if newly_collected_protobufs:
            global_config_data["points_shop_protobufs"][app_id] = newly_collected_protobufs
            await update_config_data_in_file(global_config_data)
            print(
                f"[{steamid}] Собрано и сохранено {len(newly_collected_protobufs)} новых protobuf-идентификаторов для AppID {app_id}.")
        protobuf_ids_to_use = newly_collected_protobufs  # Присваиваем для использования в выкупе

    # Логика выкупа, всегда выполняется, если protobuf_ids_to_use заполнен
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


async def claim_free_game(steamid: str, cookies: dict, url: str, app_id: str, global_config_data: dict):
    """
    Пытается получить бесплатную игру.
    1. Обрабатывает проверку возраста.
    2. Проверяет, не добавлена ли игра уже.
    3. Проверяет, является ли игра бесплатной для получения лицензии (наличие формы addfreelicense).
    4. Использует сохраненные параметры (subid/bundleid), если они есть, иначе парсит страницу.
    5. Отправляет запрос на добавление игры.
    """
    print(f"[{steamid}] Попытка получить бесплатную игру по ссылке: {url}")

    # Статические параметры для POST-запроса
    STATIC_PAYLOAD_PARAMS = {
        'action': 'add_to_cart',
        'snr': '1_5_9__403',
        'originating_snr': ''
    }

    page, browser, context = None, None, None  # Инициализация для finally
    try:
        page, browser, context = await _setup_playwright_page(cookies, url, steamid)

        # 1. Обработка проверки возраста
        if await _handle_age_verification(page, steamid):
            pass  # Продолжаем выполнение, так как страница должна быть загружена

        # 2. Проверка на уже купленную игру (кнопка "Играть")
        if await _check_if_game_owned(page, steamid):
            return  # Игра уже куплена, выходим

        # 3. Проверка на наличие формы addfreelicense (является ли игра бесплатной для получения лицензии)
        free_license_form_selector = 'form[action="https://store.steampowered.com/freelicense/addfreelicense/"][method="POST"]'
        free_license_form = await page.query_selector(free_license_form_selector)

        if not free_license_form:
            print(
                f"[{steamid}] ℹ️ Игра по ссылке {url} не является бесплатной или недоступна для получения лицензии (не найдена форма 'addfreelicense'). Пропускаю.")
            return

        # Всегда получаем актуальный sessionid из куки текущей сессии Playwright
        cookies_from_page = await context.cookies()
        current_sessionid = next((c['value'] for c in cookies_from_page if c['name'] == 'sessionid'), None)

        if not current_sessionid:
            print(f"[{steamid}] ❌ Не удалось получить текущий sessionid из куки Playwright. Невозможно добавить игру.")
            return

        # 4. Сначала пробуем использовать сохраненные параметры, если они есть.
        stored_game_params = global_config_data["free_game_params"].get(app_id)

        if stored_game_params:
            print(f"[{steamid}] Для AppID {app_id}: Использую уже собранные параметры игры для ускоренного добавления.")

            # Собираем полный payload для запроса, используя сохраненные subid/bundleid и статические/динамические поля
            payload_for_request = {
                **STATIC_PAYLOAD_PARAMS,  # Добавляем статические параметры
                **stored_game_params['payload'],  # Добавляем сохраненные subid/bundleid
                'sessionid': current_sessionid  # Добавляем актуальный sessionid
            }

            if await _send_game_claim_request(page, payload_for_request, steamid, url):
                return  # Успешно добавлено с использованием сохраненных параметров

        # Если мы дошли сюда, значит, сохраненных параметров не было или они не сработали.
        # Продолжаем извлечение параметров со страницы через Playwright.
        print(f"[{steamid}] Извлекаю параметры игры со страницы через Playwright.")
        add_to_account_button_selector = 'a.btn_green_steamui.btn_medium:has(span:has-text("Добавить на аккаунт")), a.btn_green_steamui.btn_medium:has(span:has-text("Add to Account"))'
        add_button = await page.wait_for_selector(add_to_account_button_selector,
                                                  timeout=10000)  # Уменьшенный таймаут, так как страница уже загружена

        if add_button:
            href = await add_button.get_attribute('href')
            subid_match = re.search(r'addToCart\( (\d+)', href)

            if subid_match:
                subid = subid_match.group(1)
                print(f"[{steamid}] Найден subid: {subid} из ссылки кнопки.")

                # Собираем payload для сохранения (только subid и bundleid)
                payload_to_save = {'subid': subid}

                # Проверяем наличие bundleid, если это бандл
                # Ищем bundleid в скрытых полях формы, так как он может быть там
                bundleid_element = await page.query_selector(
                    'div.game_area_purchase_game_wrapper input[type="hidden"][name="bundleid"]')
                if bundleid_element:
                    bundleid = await bundleid_element.get_attribute('value')
                    if bundleid:
                        payload_to_save['bundleid'] = bundleid
                        print(f"[{steamid}] Найден bundleid: {payload_to_save['bundleid']} из скрытого поля.")
                else:
                    # Также проверяем, если bundleid может быть в href, хотя это менее вероятно для free licenses
                    bundleid_match_from_href = re.search(r'addBundleToCart\( (\d+)', href)
                    if bundleid_match_from_href:
                        payload_to_save['bundleid'] = bundleid_match_from_href.group(1)
                        print(f"[{steamid}] Найден bundleid: {payload_to_save['bundleid']} из ссылки addBundleToCart.")

                # Собираем полный payload для текущего запроса
                payload_for_request = {
                    **STATIC_PAYLOAD_PARAMS,
                    **payload_to_save,  # Добавляем subid/bundleid
                    'sessionid': current_sessionid  # Добавляем актуальный sessionid
                }

                if await _send_game_claim_request(page, payload_for_request, steamid, url):
                    # Сохраняем параметры для будущих запусков (только subid и bundleid)
                    global_config_data["free_game_params"][app_id] = {
                        'payload': payload_to_save  # Сохраняем payload без sessionid и статических полей
                    }
                    await update_config_data_in_file(global_config_data)
                    print(
                        f"[{steamid}] Отладка: Сохраненный payload для AppID {app_id}: {payload_to_save}")  # Отладочный вывод
                    print(f"[{steamid}] Параметры для AppID {app_id} сохранены в config.py (только subid/bundleid).")

            else:
                print(f"[{steamid}] Не удалось извлечь subid из кнопки 'Добавить на аккаунт' на странице: {url}")
        else:
            print(f"[{steamid}] Кнопка 'Добавить на аккаунт' не найдена или игра уже есть на аккаунте: {url}")

    except PlaywrightTimeoutError:
        print(
            f"[{steamid}] Playwright: Таймаут ожидания элементов на странице: {url}. Возможно, игра уже добавлена или страница загрузилась некорректно.")
    except Exception as e:
        print(f"[{steamid}] Playwright: Общая ошибка при обработке бесплатной игры: {e}")
    finally:
        if browser:
            await browser.close()
        if context:  # Закрываем контекст, если он был создан
            await context.close()


async def run_for_account(mafile_path: str, urls: list[str], global_config_data: dict):
    """Запускает процесс сбора для одного аккаунта и одного URL."""
    mafile_data = await load_mafile(mafile_path)
    client = await get_steam_client(mafile_data)
    session = client.session
    steamid = mafile_data["Session"]["SteamID"]

    access_token = None
    cookies_from_client = session.cookie_jar.filter_cookies(URL("https://store.steampowered.com"))

    for cookie_name, morsel in cookies_from_client.items():
        if cookie_name == "steamLoginSecure":
            match = re.search(r'%7C%7C(.+)', morsel.value)
            if match:
                access_token = match.group(1)
                print(f"[{steamid}] ✅ Получен access_token из steamLoginSecure.")
                break

    if not access_token:
        print(f"[{steamid}] ❌ Не удалось получить access_token для аккаунта. Пропуск.")
        await session.close()
        return None

    try:
        for url in urls:
            print(f"\n[{steamid}] Обработка URL: {url}")

            app_id_match = re.search(r'/app/(\d+)', url)
            app_id = app_id_match.group(1) if app_id_match else "unknown_app"

            if "/points/shop/app/" in url:
                print(
                    f"[{steamid}] Для URL '{url}': Это страница магазина очков. Будет вызвана функция collect_points_items.")
                await collect_points_items(session, steamid, cookies_from_client, url, access_token, CONFIG_DATA)
            elif "/app/" in url:
                print(f"[{steamid}] Для URL '{url}': Это страница игры. Будет вызвана функция claim_free_game.")
                await claim_free_game(steamid, cookies_from_client, url, app_id, CONFIG_DATA)
            else:
                print(f"[{steamid}] Неизвестный формат ссылки: {url}. Пропускаю.")

    except Exception as e:
        print(f"[{mafile_data.get('Session', {}).get('SteamID', 'N/A')}] Общая ошибка при обработке аккаунта: {e}")
    finally:
        await session.close()


async def main():
    """Основная функция для запуска скрипта."""
    urls_file = "urls.txt"
    if not os.path.exists(urls_file):
        print("Создайте файл urls.txt со ссылками на бесплатные предметы (по одной ссылке на строку).")
        return

    with open(urls_file, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]

    if not urls:
        print("Файл urls.txt пуст. Добавьте хотя бы одну ссылку на магазин очков Steam.")
        return

    mafiles_dir = "./maFiles"
    if not os.path.exists(mafiles_dir):
        print(f"Создайте папку '{mafiles_dir}' и поместите туда свои .maFile.")
        return

    mafiles = [os.path.join(mafiles_dir, f) for f in os.listdir(mafiles_dir) if f.endswith(".maFile")]
    if not mafiles:
        print(f"В папке '{mafiles_dir}' не найдено .maFile.")
        return

    print(f"Найдено {len(mafiles)} аккаунтов и {len(urls)} URL для обработки.")

    global CONFIG_DATA

    print("\n--- Запуск обработки аккаунтов (последовательно) ---")
    for mafile in mafiles:
        mafile_data = await load_mafile(mafile)
        client = None
        try:
            client = await get_steam_client(mafile_data)
            session = client.session
            steamid = mafile_data["Session"]["SteamID"]

            access_token = None
            cookies_from_client = session.cookie_jar.filter_cookies(URL("https://store.steampowered.com"))

            for cookie_name, morsel in cookies_from_client.items():
                if cookie_name == "steamLoginSecure":
                    match = re.search(r'%7C%7C(.+)', morsel.value)
                    if match:
                        access_token = match.group(1)
                        print(f"[{steamid}] ✅ Получен access_token из steamLoginSecure.")
                        break

            if not access_token:
                print(
                    f"[{steamid}] ❌ Не удалось получить access_token для аккаунта. Пропуск обработки URL для этого аккаунта.")
                continue

            for url in urls:
                print(f"\n[{steamid}] Обработка URL: {url}")

                app_id_match = re.search(r'/app/(\d+)', url)
                app_id = app_id_match.group(1) if app_id_match else "unknown_app"

                if "/points/shop/app/" in url:
                    print(
                        f"[{steamid}] Для URL '{url}': Это страница магазина очков. Будет вызвана функция collect_points_items.")
                    await collect_points_items(session, steamid, cookies_from_client, url, access_token, CONFIG_DATA)
                elif "/app/" in url:
                    print(f"[{steamid}] Для URL '{url}': Это страница игры. Будет вызвана функция claim_free_game.")
                    await claim_free_game(steamid, cookies_from_client, url, app_id, CONFIG_DATA)
                else:
                    print(f"[{steamid}] Неизвестный формат ссылки: {url}. Пропускаю.")

        except Exception as e:
            print(f"[{mafile_data.get('Session', {}).get('SteamID', 'N/A')}] Общая ошибка при обработке аккаунта: {e}")
        finally:
            if client:
                await client.session.close()

    print("\nОбработка завершена.")


if __name__ == "__main__":
    asyncio.run(main())
