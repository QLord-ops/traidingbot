"""Сторож торгового бота: поднимает панель и engine после сбоя/перезагрузки.

Логика: раз в CHECK_SECONDS проверяет /health. Если панель мертва —
запускает run_web.py, ждёт, стартует engine (POST /testnet/start) и шлёт
Telegram-алерт. Если панель жива — ничего не трогает (намеренная остановка
engine кнопкой «Остановить» уважается: сторож перезапускает engine только
вместе с самой панелью).

Запуск (незаметно, из автозагрузки): pythonw.exe scripts/watchdog.py
Одновременно работает только один экземпляр (лок через локальный порт).
"""
from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

PROJECT = Path(__file__).resolve().parent.parent
PYTHON = PROJECT / ".venv" / "Scripts" / "python.exe"
BASE = "http://127.0.0.1:8000"
CHECK_SECONDS = 300
LOCK_PORT = 8123  # занят — значит, другой сторож уже работает

logging.basicConfig(
    filename=PROJECT / "data" / "watchdog.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("watchdog")


def telegram(text: str) -> None:
    try:
        env = (PROJECT / ".env").read_text(encoding="utf-8")
        values = dict(
            line.split("=", 1) for line in env.splitlines()
            if "=" in line and not line.startswith("#")
        )
        token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat = values.get("TELEGRAM_CHAT_ID", "").strip()
        if token and chat:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text[:4000]}, timeout=10,
            )
    except Exception as exc:  # алерт не должен ронять сторожа
        log.warning("Telegram недоступен: %s", exc)


def web_alive() -> bool:
    try:
        return requests.get(f"{BASE}/health", timeout=5).ok
    except requests.RequestException:
        return False


def revive() -> None:
    log.warning("Панель не отвечает — перезапуск")
    subprocess.Popen(
        [str(PYTHON), "run_web.py"], cwd=str(PROJECT),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    for _ in range(12):
        time.sleep(5)
        if web_alive():
            break
    else:
        log.error("Панель не поднялась за 60 секунд")
        telegram("ВНИМАНИЕ: панель traidingbot не поднялась после перезапуска — "
                 "нужен ручной разбор.")
        return
    try:
        requests.post(f"{BASE}/testnet/start", timeout=180)
        log.info("Engine стартован")
        telegram("traidingbot: панель и engine перезапущены сторожем "
                 "(перезагрузка машины или сбой процесса).")
    except requests.RequestException as exc:
        log.error("Не удалось стартовать engine: %s", exc)
        telegram(f"ВНИМАНИЕ: панель traidingbot поднята, но engine не стартовал: {exc}")


def main() -> None:
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        sys.exit(0)  # сторож уже работает
    lock.listen(1)
    log.info("Сторож запущен (проверка каждые %d сек)", CHECK_SECONDS)
    while True:
        try:
            if not web_alive():
                time.sleep(10)  # защита от ложного срабатывания на миг рестарта
                if not web_alive():
                    revive()
        except Exception:
            log.exception("Ошибка цикла сторожа")
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    main()
