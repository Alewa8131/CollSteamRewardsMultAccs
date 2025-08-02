import asyncio
import json
import os
import re
from aiosteampy.client import SteamClient
from bs4 import BeautifulSoup
from yarl import URL
import aiohttp
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# Импортируем список из config.py
# Убедитесь, что у вас есть файл config.py с пустым словарем PROTOBUF_LIST = {}
# или с уже известными вам protobuf-идентификаторами
try:
    from config import PROTOBUF_LIST
except ImportError:
    PROTOBUF_LIST = {}
    print("Файл config.py не найден или не содержит PROTOBUF_LIST. Создаю временный пустой словарь.")

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


async def update_protobuf_list_in_config(new_protobuf_dict: dict):
    """Обновляет словарь PROTOBUF_LIST в config.py."""
    print(f"Обновляю {CONFIG_FILE_PATH} с новыми protobuf-идентификаторами.")
    try:
        # Читаем текущее содержимое файла
        if os.path.exists(CONFIG_FILE_PATH):
            with open(CONFIG_FILE_PATH, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        else:
            lines = []

        # Находим строку с PROTOBUF_LIST и заменяем ее
        updated_lines = []
        found = False
        for line in lines:
            if line.strip().startswith("PROTOBUF_LIST ="):
                updated_lines.append(f"PROTOBUF_LIST = {json.dumps(new_protobuf_dict, indent=4)}\n")
                found = True
            else:
                updated_lines.append(line)

        if not found:
            # Если строка не найдена, добавляем в конец файла
            updated_lines.append(f"\nPROTOBUF_LIST = {json.dumps(new_protobuf_dict, indent=4)}\n")

        # Записываем обновленное содержимое обратно в файл
        with open(CONFIG_FILE_PATH, 'w', encoding='utf-8') as f:
            f.writelines(updated_lines)
        print(f"✅ {CONFIG_FILE_PATH} успешно обновлен.")
    except Exception as e:
        print(f"❌ Ошибка при обновлении {CONFIG_FILE_PATH}: {e}")


async def _attempt_to_close_any_modal(page, steamid):
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


# Вспомогательная функция для парсинга строки multipart/form-data для конкретного поля
def _parse_multipart_field(multipart_string: str, field_name: str) -> str | None:
    """
    Парсит строку multipart/form-data для извлечения значения конкретного поля.
    """
    # Это регулярное выражение ищет имя поля и захватывает все до следующего разделителя или конца строки.
    # Оно предполагает структуру: Content-Disposition: form-data; name="field_name"\r\n\r\nfield_value\r\n
    pattern = rf'name="{re.escape(field_name)}"\r\n\r\n(.*?)\r\n(?:--|$)'
    match = re.search(pattern, multipart_string, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


async def collect_points_items(session: aiohttp.ClientSession, steamid: str, cookies: dict, shop_url: str,
                               access_token: str, protobufs_for_app: list = None):
    """
    Собирает бесплатные предметы за очки Steam.
    Если protobufs_for_app предоставлен, использует его для выкупа.
    Иначе, использует Playwright для сбора protobuf-идентификаторов.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36',
        'Origin': 'https://store.steampowered.com',
        'Referer': shop_url,
    }

    # Извлекаем appId из URL
    app_id_match = re.search(r'/app/(\d+)', shop_url)
    app_id = app_id_match.group(1) if app_id_match else "unknown_app"

    newly_collected_protobufs = []  # Для сбора в текущем Playwright запуске, если он произойдет

    if protobufs_for_app:
        print(
            f"[{steamid}] Для AppID {app_id}: Использую уже собранные protobuf-идентификаторы для ускоренного выкупа.")
        protobuf_ids_to_use = protobufs_for_app
    else:
        print(f"[{steamid}] Для AppID {app_id}: Запущен проход (с Playwright) для сбора protobuf-идентификаторов.")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)  # Оставляем видимым для отладки
            context = await browser.new_context()

            playwright_cookies = []
            # Итерируем по куки из aiohttp.CookieJar
            for morsel in cookies.values():
                cookie_dict = {
                    "name": morsel.key,
                    "value": morsel.value,
                    "httpOnly": "httponly" in morsel,
                    "secure": "secure" in morsel,
                }

                # Обработка sameSite для Playwright
                same_site_value = morsel.get("samesite")
                if same_site_value:
                    same_site_value = same_site_value.capitalize()
                    if same_site_value not in ["Strict", "Lax", "None"]:
                        same_site_value = "Lax"
                else:
                    same_site_value = "Lax"
                cookie_dict["sameSite"] = same_site_value

                # Приоритет 'url' для куки супердоменов или если домен/путь не определены
                morsel_domain = morsel.get("domain")
                morsel_path = morsel.get("path")

                if morsel_domain and morsel_domain.startswith('.'):
                    # Для куки супердоменов (например, .steampowered.com) Playwright предпочитает URL
                    effective_domain = morsel_domain.lstrip('.')  # Удаляем ведущую точку для построения URL
                    # Предполагаем HTTPS, так как это Steam Store
                    cookie_dict["url"] = f"https://{effective_domain}{morsel_path if morsel_path else '/'}"
                elif morsel_domain and morsel_path:
                    # Если домен и путь явно указаны и это не супердомен
                    cookie_dict["domain"] = morsel_domain
                    cookie_dict["path"] = morsel_path
                else:
                    # Fallback к хосту и корневому пути shop_url
                    cookie_dict["domain"] = URL(shop_url).host
                    cookie_dict["path"] = "/"

                playwright_cookies.append(cookie_dict)
            await context.add_cookies(playwright_cookies)

            page = await context.new_page()

            # Перехватываем protobuf для RedeemPoints
            async def route_handler(route):
                request = route.request
                url = request.url
                method = request.method

                # Перехватываем protobuf для RedeemPoints
                if "ILoyaltyRewardsService/RedeemPoints/v1" in url and method == "POST":
                    post_data_str = request.post_data  # Это сырое строковое содержимое

                    if post_data_str:
                        # Используем вспомогательную функцию для парсинга multipart/form-data
                        redeem_protobuf = _parse_multipart_field(post_data_str, "input_protobuf_encoded")

                        if redeem_protobuf and redeem_protobuf not in newly_collected_protobufs:  # Изменено на local list
                            newly_collected_protobufs.append(redeem_protobuf)
                            print(f"[{steamid}] ✅ Playwright: Перехвачен Redeem Protobuf: {redeem_protobuf}")
                        elif not redeem_protobuf:
                            print(
                                f"[{steamid}] Playwright: Не удалось извлечь input_protobuf_encoded из post_data (multipart). Начало сырых данных: {post_data_str[:100]}...")

                await route.continue_()

            await page.route("**/api.steampowered.com/**", route_handler)

            try:
                print(f"[{steamid}] Playwright: Перехожу на страницу {shop_url}...")
                await page.goto(shop_url, wait_until="load", timeout=60000)

                # Ждем, пока все элементы загрузятся, используя более точный селектор
                await page.wait_for_selector('div.skI5tVFxF4zkY8z56LALc', timeout=30000)  # Увеличенный таймаут
                await asyncio.sleep(2)  # Уменьшенная задержка для загрузки скриптов и рендеринга

                # Находим все элементы предметов
                item_elements = await page.query_selector_all('div.skI5tVFxF4zkY8z56LALc')
                print(f"[{steamid}] Playwright: Найдено {len(item_elements)} потенциальных элементов предметов.")

                for i, item_el in enumerate(item_elements):
                    print(f"[{steamid}] Playwright: --- Обработка предмета #{i + 1} ---")
                    try:
                        # Используем точный класс для элемента цены
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

                            # 1. Кликаем по элементу предмета, чтобы открыть модальное окно
                            await item_el.click()
                            print(f"[{steamid}] Playwright: Кликнул по элементу предмета.")

                            # 2. Ждем появления основного контейнера модального окна
                            modal_container_selector = 'dialog._32QRvPPBL733SpNR9x0Gp3'
                            try:
                                modal_container = await page.wait_for_selector(modal_container_selector, timeout=10000)
                                print(
                                    f"[{steamid}] Playwright: Главный контейнер модального окна появился (селектор: '{modal_container_selector}').")

                                # Теперь ждем активного содержимого внутри этого контейнера
                                modal_overlay_content_selector = 'div.ModalOverlayContent.active'
                                purchase_modal_content = await modal_container.wait_for_selector(
                                    modal_overlay_content_selector, timeout=5000)
                                print(
                                    f"[{steamid}] Playwright: Активное содержимое модального окна появилось (селектор: '{modal_overlay_content_selector}').")

                                # 3. Попытка найти и кликнуть по кнопке "Бесплатно" для покупки
                                free_purchase_button = await purchase_modal_content.query_selector(
                                    'div[role="button"]._19X6AbdPOUHqSxNz3mm18i:has(div._2pwsWXANIuk8w8cZ8wvNz:has-text("Бесплатно")), div[role="button"]._19X6AbdPOUHqSxNz3mm18i:has(div._2pwsWXANIuk8w8cZ8wvNz:has-text("Free"))'
                                )

                                # 4. Попытка найти и кликнуть по кнопке "Использовать сейчас" / "Equip now" для уже имеющихся предметов
                                equip_now_button = await purchase_modal_content.query_selector(
                                    'button.SRxqV4jytIuP55fxgfpD1._1hcJa9ylImmFKuHsfilos.Focusable:has-text("Использовать сейчас"), button.SRxqV4jytIuP55fxgfpD1._1hcJa9ylImmFKuHsfilos.Focusable:has-text("Equip now")'
                                )

                                # 5. Новая логика: Попытка найти и кликнуть по кнопке "Позже" / "Later" для уже купленных предметов
                                later_button_in_modal = await purchase_modal_content.query_selector(
                                    'button._3Ju8vy_foEPg9ILmy2-htb._1hcJa9ylImmFKuHsfilos.Focusable:has-text("Позже"), button._3Ju8vy_foEPg9ILmy2-htb._1hcJa9ylImmFKuHsfilos.Focusable:has-text("Later")'
                                )

                                if free_purchase_button and await free_purchase_button.is_visible():
                                    print(
                                        f"[{steamid}] Playwright: Найдена кнопка 'Бесплатно' в модальном окне. Кликаю...")
                                    await free_purchase_button.click()
                                    await asyncio.sleep(0.5)  # Короткая задержка после клика по покупке

                                    # Теперь ждем кнопку "Позже" (модальное окно после покупки)
                                    try:
                                        later_button_selector = 'button._3Ju8vy_foEPg9ILmy2-htb._1hcJa9ylImmFKuHsfilos.Focusable:has-text("Позже"), button._3Ju8vy_foEPg9ILmy2-htb._1hcJa9ylImmFKuHsfilos.Focusable:has-text("Later")'
                                        later_button = await page.wait_for_selector(later_button_selector,
                                                                                    timeout=5000)  # Меньший таймаут для кнопки "Позже", так как она должна появиться быстро
                                        if later_button and await later_button.is_visible():
                                            print(f"[{steamid}] Playwright: Найдена кнопка 'Позже'. Кликаю.")
                                            await later_button.click()
                                            print(
                                                f"[{steamid}] Playwright: ✅ Предмет #{i + 1} успешно куплен и модальное окно закрыто.")
                                            await asyncio.sleep(0.2)  # Окончательная короткая задержка
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
                                    # Предмет уже куплен, просто кликаем "Позже" или закрываем модальное окно
                                    print(
                                        f"[{steamid}] Playwright: Предмет #{i + 1} уже куплен (обнаружена кнопка 'Использовать сейчас'). Попытка закрыть модальное окно.")
                                    await _attempt_to_close_any_modal(page, steamid)
                                    print(
                                        f"[{steamid}] Playwright: ✅ Предмет #{i + 1} был уже куплен. Модальное окно закрыто.")

                                elif later_button_in_modal and await later_button_in_modal.is_visible():
                                    # Предмет уже куплен, обнаружена кнопка "Позже"
                                    print(
                                        f"[{steamid}] Playwright: Предмет #{i + 1} уже куплен (обнаружена кнопка 'Позже'). Попытка закрыть модальное окно.")
                                    await later_button_in_modal.click()  # Кликаем по кнопке "Позже"
                                    await asyncio.sleep(0.2)  # Короткая задержка
                                    print(
                                        f"[{steamid}] Playwright: ✅ Предмет #{i + 1} был уже куплен. Модальное окно закрыто.")

                                else:
                                    print(
                                        f"[{steamid}] Playwright: В модальном окне не найдена кнопка 'Бесплатно', 'Использовать сейчас' или 'Позже'. Возможно, произошла ошибка или неожиданное состояние.")
                                    try:
                                        # Используем уже найденный purchase_modal_content ElementHandle для получения его innerHTML
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
                                await _attempt_to_close_any_modal(page, steamid)  # Всегда пытаемся закрыть
                            except Exception as modal_e:
                                print(
                                    f"[{steamid}] Playwright: Ошибка при работе с модальным окном (после клика по предмету): {modal_e}. Пропускаю этот предмет.")
                                await _attempt_to_close_any_modal(page, steamid)  # Всегда пытаемся закрыть

                            await asyncio.sleep(0.5)  # Уменьшенная задержка перед следующим предметом
                        else:
                            print(
                                f"[{steamid}] Playwright: Предмет #{i + 1} не бесплатен (цена: '{price_text}'). Пропускаю.")
                    except PlaywrightTimeoutError:
                        print(f"[{steamid}] Playwright: Таймаут при обработке предмета #{i + 1}. Пропускаю.")
                        await _attempt_to_close_any_modal(page,
                                                          steamid)  # Убедитесь, что модальное окно закрыто, если произошел таймаут во время обработки предмета
                    except Exception as e:
                        print(f"[{steamid}] Playwright: Ошибка при обработке предмета #{i + 1}: {e}")
                        await _attempt_to_close_any_modal(page,
                                                          steamid)  # Убедитесь, что модальное окно закрыто, если произошла ошибка во время обработки предмета

            except PlaywrightTimeoutError as e:
                print(f"[{steamid}] Playwright: Таймаут при загрузке страницы: {e}. Пропускаю.")
            except Exception as e:
                print(f"[{steamid}] Playwright: Общая ошибка при работе Playwright: {e}. Пропускаю.")
            finally:
                await context.close()
                await browser.close()
            return newly_collected_protobufs  # Возвращаем только что собранные protobufs

    # Если мы здесь, значит, Playwright не запускался, и у нас есть protobufs_for_app
    # Или Playwright запустился и собрал newly_collected_protobufs
    protobuf_ids_to_use = protobufs_for_app if protobufs_for_app else newly_collected_protobufs

    if not protobuf_ids_to_use:
        print(f"[{steamid}] ❌ Список предметов для выкупа пуст для AppID {app_id}. Пропуск.")
        return None  # Возвращаем None, так как ничего не было выкуплено

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
                        f"[{steamid}] ⚠️ Покупка завершена, но сервер вернул бинарный ответ: {response_bytes.hex()}")
                else:
                    print(f"[{steamid}] ❌ Ошибка: статус {redeem_resp.status}, ответ: {response_bytes.hex()}")

        except Exception as e:
            print(f"[{steamid}] Ошибка при попытке купить предмет: {e}")
    return newly_collected_protobufs  # Возвращаем собранные protobufs (если Playwright был использован)


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

    # Используем глобальный PROTOBUF_LIST из config.py
    global PROTOBUF_LIST

    # --- Фаза: Обработка всех аккаунтов (последовательно) ---
    print("\n--- Запуск обработки аккаунтов (последовательно) ---")
    for mafile in mafiles:  # Итерируем по ma-файлам
        mafile_data = await load_mafile(mafile)
        client = None  # Инициализируем client вне try, чтобы он был доступен в finally
        try:
            client = await get_steam_client(mafile_data)  # Логинимся один раз для аккаунта
            session = client.session
            steamid = mafile_data["Session"]["SteamID"]

            # Получаем access_token после client.login() из куки
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
                continue  # Переходим к следующему аккаунту

            for url in urls:  # Теперь обрабатываем все URL для этого ОДНОГО авторизованного аккаунта
                print(f"\n[{steamid}] Обработка URL: {url}")

                # Извлекаем appId из URL
                app_id_match = re.search(r'/app/(\d+)', url)
                app_id = app_id_match.group(1) if app_id_match else "unknown_app"

                # Проверяем, есть ли уже protobufs для этого appId в глобальном словаре
                protobufs_for_current_app = PROTOBUF_LIST.get(app_id)

                collected_protobufs_from_run = await collect_points_items(
                    session, steamid, cookies_from_client, url, access_token,
                    protobufs_for_app=protobufs_for_current_app
                )

                # Если были собраны новые protobufs (т.е. Playwright был использован для этого appId)
                if collected_protobufs_from_run:
                    # Обновляем глобальный словарь и сохраняем его
                    PROTOBUF_LIST[app_id] = collected_protobufs_from_run
                    await update_protobuf_list_in_config(PROTOBUF_LIST)
                    print(
                        f"[{steamid}] Собрано и сохранено {len(collected_protobufs_from_run)} новых protobuf-идентификаторов для AppID {app_id}.")

        except Exception as e:
            print(f"[{mafile_data.get('Session', {}).get('SteamID', 'N/A')}] Общая ошибка при обработке аккаунта: {e}")
        finally:
            if client:
                await client.session.close()  # Закрываем сессию только после обработки всех URL для аккаунта

    print("\nОбработка завершена.")


if __name__ == "__main__":
    asyncio.run(main())
