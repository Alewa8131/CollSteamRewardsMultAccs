import asyncio
import json
import os
import re
from aiosteampy.client import SteamClient
from bs4 import BeautifulSoup
from yarl import URL
import aiohttp
from dotenv import load_dotenv
import base64
import functools
from playwright.async_api import async_playwright

load_dotenv()


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


async def claim_free_game(session: aiohttp.ClientSession, steamid: str, cookies: dict, url: str):
    """
    Пытается получить бесплатную игру.
    Этот метод требует доработки для перехвата POST-запроса на добавление игры.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
        'Referer': url,
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'X-Requested-With': 'XMLHttpRequest',
    }

    print(f"[{steamid}] Попытка получить бесплатную игру по ссылке: {url}")
    print(f"[{steamid}] ВНИМАНИЕ: Для бесплатных игр требуется перехват POST-запроса 'addfreelicense' или 'addapp'.")
    print(f"[{steamid}] Текущая реализация может не работать, так как полагается на HTML-парсинг.")


async def extract_token_and_protobuf_from_browser(cookies: dict, target_url: str, steamid: str):
    """
    Использует Playwright для загрузки страницы и перехвата access_token
    и input_protobuf_encoded из нужных запросов.
    """
    access_token = None
    input_protobuf_encoded_for_loyalty = None
    captured_requests_list = []  # Список для хранения всех перехваченных запросов

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # Оставляем видимым для отладки
        context = await browser.new_context()

        playwright_cookies = []
        for cookie_name, morsel in cookies.items():
            cookie_domain = morsel.get("domain", "")
            cookie_path = morsel.get("path", "")

            if not cookie_domain:
                try:
                    parsed_target_url = URL(target_url)
                    cookie_domain = parsed_target_url.host
                except Exception:
                    print(f"[{steamid}] ⚠️ Не удалось определить домен для куки '{morsel.key}'. Пропускаю.")
                    continue

            if not cookie_path:
                cookie_path = "/"

            http_only = bool(morsel.get("httponly", False))
            secure = bool(morsel.get("secure", False))

            same_site_value = morsel.get("samesite", "").lower()
            if same_site_value == "lax":
                same_site = "Lax"
            elif same_site_value == "strict":
                same_site = "Strict"
            elif same_site_value == "none":
                same_site = "None"
            else:
                same_site = "Lax"

            playwright_cookies.append({
                "name": morsel.key,
                "value": morsel.value,
                "domain": cookie_domain,
                "path": cookie_path,
                "httpOnly": http_only,
                "secure": secure,
                "sameSite": same_site,
            })
        await context.add_cookies(playwright_cookies)

        page = await context.new_page()

        # --- Перехватываем все запросы к API Steam для отладки и сбора ---
        async def route_handler(route):
            nonlocal access_token, input_protobuf_encoded_for_loyalty
            request = route.request
            url = request.url
            method = request.method
            headers = request.headers
            post_data = request.post_data_buffer

            # Добавляем запрос в список для последующего анализа
            captured_requests_list.append(request)

            print(f"[{steamid}] [DEBUG_REQUEST] {method} {url}")
            if post_data:
                try:
                    print(f"[{steamid}] [DEBUG_REQUEST] POST Data: {post_data.decode('utf-8')}")
                except UnicodeDecodeError:
                    print(f"[{steamid}] [DEBUG_REQUEST] POST Data (binary): {post_data.hex()}")

            if 'authorization' in headers:
                auth_header = headers['authorization']
                if auth_header.startswith("Bearer "):
                    current_token = auth_header.split(" ")[1]
                    if current_token and not access_token:
                        access_token = current_token
                        print(f"[{steamid}] ✅ Перехвачен access_token из заголовка Authorization в {url}")

            # --- Перехватываем loyalty_protobuf из GetSummary ИЛИ GetParentalSettings ---
            if ("ILoyaltyRewardsService/GetSummary/v1" in url or
                    "IParentalService/GetParentalSettings/v1" in url):
                parsed = URL(url)
                if 'access_token' in parsed.query:  # Обновляем access_token, если он есть в URL
                    access_token = parsed.query["access_token"]
                if 'input_protobuf_encoded' in parsed.query:
                    input_protobuf_encoded_for_loyalty = parsed.query["input_protobuf_encoded"]

                if access_token and input_protobuf_encoded_for_loyalty:
                    print(
                        f"[{steamid}] ✅ Перехвачен access_token и input_protobuf_encoded ИЗ LOYALTY SUMMARY/PARENTAL SETTINGS: {parsed.path}")

            await route.continue_()

        await page.route("**/api.steampowered.com/**", route_handler)
        # --- КОНЕЦ НОВОГО БЛОКА ---

        try:
            print(f"[{steamid}] Перехожу на страницу {target_url} в Playwright...")
            await page.goto(target_url, wait_until="load", timeout=60000)

            # --- Анализируем собранные запросы после загрузки страницы ---
            print(f"[{steamid}] Анализирую собранные запросы для поиска access_token и input_protobuf_encoded...")
            for req in captured_requests_list:
                url_parsed = URL(req.url)

                # Проверяем URL-параметры
                if 'access_token' in url_parsed.query:
                    access_token = url_parsed.query["access_token"]
                if 'input_protobuf_encoded' in url_parsed.query:
                    # Убедимся, что это нужный protobuf из GetSummary/GetParentalSettings
                    if ("ILoyaltyRewardsService/GetSummary/v1" in req.url or
                            "IParentalService/GetParentalSettings/v1" in req.url):
                        input_protobuf_encoded_for_loyalty = url_parsed.query["input_protobuf_encoded"]

                # Проверяем заголовки Authorization для access_token
                if 'authorization' in req.headers:
                    auth_header = req.headers['authorization']
                    if auth_header.startswith("Bearer "):
                        current_token = auth_header.split(" ")[1]
                        if current_token and not access_token:
                            access_token = current_token

                if access_token and input_protobuf_encoded_for_loyalty:
                    print(f"[{steamid}] ✅ access_token и input_protobuf_encoded найдены после анализа логов.")
                    break  # Если нашли оба, можно выйти

            # Даем дополнительное время для завершения всех фоновых запросов
            await asyncio.sleep(5)

        except Exception as e:
            print(f"[{steamid}] Ошибка при загрузке страницы в Playwright: {e}")

        await context.close()
        await browser.close()

        if not access_token or not input_protobuf_encoded_for_loyalty:
            print(f"[{steamid}] ⚠️ Не удалось найти access_token или input_protobuf_encoded после всех попыток.")
            print(f"[{steamid}] Текущий access_token: {access_token[:10]}..." if access_token else "None")
            print(
                f"[{steamid}] Текущий input_protobuf_encoded: {input_protobuf_encoded_for_loyalty[:10]}..." if input_protobuf_encoded_for_loyalty else "None")

        return access_token, input_protobuf_encoded_for_loyalty


async def collect_points_items(session: aiohttp.ClientSession, steamid: str, cookies: dict, shop_url: str):
    """
    Собирает бесплатные предметы за очки Steam.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
        'Origin': 'https://store.steampowered.com',
        'Referer': shop_url,
    }

    access_token, loyalty_protobuf = await extract_token_and_protobuf_from_browser(cookies, shop_url, steamid)

    if not access_token or not loyalty_protobuf:
        print(
            f"[{steamid}] ❌ Не удалось извлечь access_token или loyalty_protobuf из браузера для SteamID {steamid}. Пропуск сбора предметов за очки.")
        return

    print(f"[{steamid}] ✅ access_token: {access_token[:10]}..." if access_token else "None")
    print(f"[{steamid}] ✅ loyalty_protobuf: {loyalty_protobuf[:10]}..." if loyalty_protobuf else "None")

    # --- ВНИМАНИЕ: Нам всё ещё нужен способ получить список уникальных ID предметов ---
    # AppID 3300150 - это ID вкладки/страницы, а не конкретного предмета.
    # HTML-парсинг не выявил явных уникальных item_id.
    # GetEligibleAppRewardItems/v1 выдает 404.
    print(f"[{steamid}] ⚠️ НЕИЗВЕСТЕН СПОСОБ ПОЛУЧЕНИЯ СПИСКА УНИКАЛЬНЫХ ID ПРЕДМЕТОВ со страницы {shop_url}.")
    print(
        f"[{steamid}] Чтобы автоматизировать покупку всех предметов, нам нужен API-запрос (или другой метод), который возвращает ID каждого отдельного предмета на этой странице.")

    # --- Временная заглушка для item_id, чтобы протестировать RedeemPoints/v1 ---
    # Используем AppID из URL для логирования, но понимаем, что это не ID конкретного предмета.
    try:
        parsed_shop_url = URL(shop_url)
        path_segments = parsed_shop_url.path.split('/')
        placeholder_item_id = int(path_segments[-1]) if path_segments and path_segments[-1].isdigit() else "UNKNOWN"
    except Exception:
        placeholder_item_id = "UNKNOWN"

    print(f"[{steamid}] (Временно) Попытка купить предмет с AppID (или ID вкладки): {placeholder_item_id}")

    # --- Запрос на покупку предмета за очки ---
    redeem_points_base_url = "https://api.steampowered.com/ILoyaltyRewardsService/RedeemPoints/v1/"

    # access_token передается в URL, input_protobuf_encoded в теле POST-запроса
    redeem_points_url_with_token = f"{redeem_points_base_url}?access_token={access_token}"

    # --- ИСПРАВЛЕНИЕ: Используем предоставленный тобой CLK0GBAA в качестве input_protobuf_encoded ---
    # Этот protobuf будет отправлен в Form Data.
    payload_redeem_points = {
        "input_protobuf_encoded": "CLK0GBAA"
    }

    print(
        f"[{steamid}] Попытка купить предмет (AppID: {placeholder_item_id}) за очки с protobuf '{payload_redeem_points['input_protobuf_encoded']}'...")
    try:
        async with session.post(redeem_points_url_with_token, headers=headers,
                                data=payload_redeem_points) as redeem_resp:
            # --- Читаем ответ как сырые байты, а не JSON ---
            response_bytes = await redeem_resp.read()

            if redeem_resp.status == 200 and not response_bytes:
                print(f"[{steamid}] 🎁 Успешно куплен предмет (AppID: {placeholder_item_id}) за очки!")
            elif redeem_resp.status == 200 and response_bytes:
                print(
                    f"[{steamid}] ⚠️ Покупка предмета (AppID: {placeholder_item_id}) завершилась успешно, но сервер вернул бинарный ответ: {response_bytes.hex()}")
            else:
                print(
                    f"[{steamid}] ❌ Ошибка при покупке предмета (AppID: {placeholder_item_id}): статус {redeem_resp.status}, ответ: {response_bytes.hex()}")

    except Exception as e:
        print(f"[{steamid}] Ошибка при попытке купить предмет (AppID: {placeholder_item_id}): {e}")


async def run_for_account(mafile_path: str, urls: list[str]):
    """Запускает процесс сбора для одного аккаунта."""
    mafile_data = await load_mafile(mafile_path)
    client = await get_steam_client(mafile_data)  # Теперь get_steam_client выполняет логин
    session = client.session
    steamid = mafile_data["Session"]["SteamID"]

    # Получаем куки из сессии aiosteampy для Playwright
    cookies = session.cookie_jar.filter_cookies(URL("https://store.steampowered.com"))

    try:
        for url in urls:
            if "/points/shop/app/" in url:
                await collect_points_items(session, steamid, cookies, url)
            # elif "/app/" in url: # Временно закомментировано по запросу пользователя
            #     await claim_free_game(session, steamid, cookies, url)
            else:
                print(f"[{steamid}] Неизвестный формат ссылки: {url}")
    finally:
        await session.close()


async def main():
    """Основная функция для запуска скрипта."""
    urls_file = "urls.txt"
    if not os.path.exists(urls_file):
        print("Создай файл urls.txt со ссылками на бесплатные предметы (по одной ссылке на строку).")
        return

    with open(urls_file, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]

    mafiles_dir = "./maFiles"
    if not os.path.exists(mafiles_dir):
        print(f"Создай папку '{mafiles_dir}' и помести туда свои .maFile.")
        return

    mafiles = [os.path.join(mafiles_dir, f) for f in os.listdir(mafiles_dir) if f.endswith(".maFile")]
    if not mafiles:
        print(f"В папке '{mafiles_dir}' не найдено .maFile.")
        return

    print(f"Найдено {len(mafiles)} аккаунтов и {len(urls)} URL для обработки.")
    await asyncio.gather(*(run_for_account(mafile, urls) for mafile in mafiles))
    print("Обработка завершена.")


if __name__ == "__main__":
    asyncio.run(main())
