import logging
import time
import os
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from config import GOOGLE_USERNAME, GOOGLE_PASSWORD, TEMP_COOKIE_PATH, YOUTUBE_COOKIES_CONTENT

logger = logging.getLogger(__name__)

def _get_chrome_options():
    """Настройка опций для headless Chrome в Docker."""
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36")
    return chrome_options

def _format_cookies_for_netscape(cookies):
    """Форматирует cookies из Selenium в Netscape-формат, понятный yt-dlp."""
    netscape_cookies = ["# Netscape HTTP Cookie File\n# https://curl.haxx.se/rfc/cookie_spec.html\n# This is a generated file! Do not edit.\n\n"]
    for cookie in cookies:
        domain = cookie.get('domain', '')
        path = cookie.get('path', '/')
        secure = str(cookie.get('secure', 'FALSE')).upper()
        expiry = str(int(cookie.get('expiry', 0)))
        name = cookie.get('name', '')
        value = cookie.get('value', '')

        # Гарантируем, что у домена есть ведущая точка, если это необходимо
        if not domain.startswith('.') and domain.count('.') == 1:
            domain = '.' + domain

        line = f"{domain}\tTRUE\t{path}\t{secure}\t{expiry}\t{name}\t{value}\n"
        netscape_cookies.append(line)
    return "".join(netscape_cookies)

def refresh_cookies() -> bool:
    """
    Автоматически входит в аккаунт Google с помощью Selenium, переходит на YouTube,
    извлекает свежие cookies и сохраняет их.
    """
    # Если cookies были переданы через переменную окружения, нет смысла их обновлять.
    # Эта логика предназначена для случая, когда бот работает полностью автономно без ручного ввода cookies.
    if YOUTUBE_COOKIES_CONTENT:
        logger.info("YOUTUBE_COOKIES_CONTENT установлена. Пропускаем автоматическое обновление cookies.")
        # В будущем можно добавить логику, чтобы даже при наличии переменной обновлять куки, если они устарели.
        return True # Считаем, что раз куки есть, то все хорошо.

    if not GOOGLE_USERNAME or not GOOGLE_PASSWORD:
        logger.error("GOOGLE_USERNAME или GOOGLE_PASSWORD не установлены. Обновление cookies невозможно.")
        return False

    cookie_file_path = TEMP_COOKIE_PATH if TEMP_COOKIE_PATH else "youtube_cookies.txt"

    logger.info("Начинается процесс автоматического обновления cookies...")
    driver = None
    try:
        service = Service()
        options = _get_chrome_options()
        driver = webdriver.Chrome(service=service, options=options)
        wait = WebDriverWait(driver, 20)

        logger.info("Шаг 1/6: Переход на страницу входа Google...")
        driver.get("https://accounts.google.com/signin/v2/identifier?flowName=GlifWebSignIn&flowEntry=ServiceLogin")

        logger.info("Шаг 2/6: Ввод email...")
        email_field = wait.until(EC.visibility_of_element_located((By.NAME, "identifier")))
        email_field.send_keys(GOOGLE_USERNAME)
        driver.find_element(By.ID, "identifierNext").click()

        logger.info("Шаг 3/6: Ввод пароля...")
        password_field = wait.until(EC.visibility_of_element_located((By.NAME, "Passwd")))
        password_field.send_keys(GOOGLE_PASSWORD)
        driver.find_element(By.ID, "passwordNext").click()

        logger.info("Шаг 4/6: Ожидание завершения входа...")
        # Ждем появления элемента, который точно есть на странице после входа,
        # например, иконки профиля. Используем более надежный CSS селектор.
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[href^='https://myaccount.google.com']")))

        logger.info("Шаг 5/6: Переход на YouTube...")
        driver.get("https://www.youtube.com")
        # Ждем загрузки основной части страницы
        wait.until(EC.presence_of_element_located((By.ID, "content")))
        
        logger.info("Шаг 6/6: Извлечение и сохранение cookies...")
        cookies = driver.get_cookies()
        if not cookies:
            logger.error("Не удалось извлечь cookies после входа.")
            return False
            
        netscape_formatted_cookies = _format_cookies_for_netscape(cookies)

        with open(cookie_file_path, 'w', encoding='utf-8') as f:
            f.write(netscape_formatted_cookies)
        
        logger.info(f"Cookies успешно обновлены и сохранены в {cookie_file_path}")
        return True

    except Exception as e:
        logger.error(f"Произошла ошибка во время обновления cookies: {e}", exc_info=True)
        # Попытка сделать скриншот для отладки
        if driver:
            try:
                screenshot_path = "/app/cookie_error_screenshot.png"
                driver.save_screenshot(screenshot_path)
                logger.info(f"Скриншот ошибки сохранен в {screenshot_path}")
            except Exception as screenshot_e:
                logger.error(f"Не удалось сохранить скриншот: {screenshot_e}")
        return False
    finally:
        if driver:
            driver.quit()
            logger.info("Драйвер Selenium закрыт.")
