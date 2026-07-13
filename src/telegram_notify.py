from __future__ import annotations

import logging
import threading
from typing import Callable

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"


class TelegramNotifier:
    """Уведомления в Telegram. Если токен/чат не заданы — молча выключен.

    Секреты берутся только из .env; в интерфейсе не отображаются.
    """

    def __init__(self, token: str = "", chat_id: str = "",
                 session: requests.Session | None = None):
        self.enabled = bool(token and chat_id)
        self._url = f"{API_BASE}/bot{token}"
        self.chat_id = chat_id
        self.session = session or requests.Session()

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        try:
            response = self.session.post(
                f"{self._url}/sendMessage",
                json={"chat_id": self.chat_id, "text": text[:4000]},
                timeout=10,
            )
            if not response.ok:
                log.warning("Telegram sendMessage HTTP %s: %s",
                            response.status_code, response.text[:200])
            return response.ok
        except requests.RequestException as exc:
            # уведомления не должны ломать торговый цикл
            log.warning("Telegram недоступен: %s", exc)
            return False


class TelegramCommandListener:
    """Long-polling слушатель команд. Принимает команды ТОЛЬКО из chat_id владельца.

    /stop, /panic — аварийная остановка (callback on_stop);
    /status — краткий статус (строка из on_status).
    """

    def __init__(self, token: str, chat_id: str,
                 on_stop: Callable[[], None],
                 on_status: Callable[[], str],
                 session: requests.Session | None = None,
                 poll_timeout: int = 25):
        self.enabled = bool(token and chat_id)
        self._url = f"{API_BASE}/bot{token}"
        self.chat_id = str(chat_id)
        self.on_stop = on_stop
        self.on_status = on_status
        self.session = session or requests.Session()
        self.poll_timeout = poll_timeout
        self._offset = 0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # --- обработка (выделена для тестов) ---------------------------------

    def handle_update(self, update: dict) -> str | None:
        """Возвращает имя обработанной команды или None."""
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("id")) != self.chat_id:
            return None  # чужие чаты игнорируются полностью
        text = (message.get("text") or "").strip().lower()
        command = text.split("@")[0]
        if command in ("/stop", "/panic"):
            self._reply("Аварийная остановка: закрываю позиции и останавливаю engine.")
            try:
                self.on_stop()
                self._reply("Готово: engine остановлен, позиции закрыты.")
            except Exception as exc:  # ошибку обязательно доносим владельцу
                self._reply(f"ОШИБКА аварийной остановки: {exc}. Проверьте биржу вручную!")
            return "stop"
        if command == "/status":
            self._reply(self.on_status())
            return "status"
        return None

    def _reply(self, text: str) -> None:
        try:
            self.session.post(
                f"{self._url}/sendMessage",
                json={"chat_id": self.chat_id, "text": text[:4000]},
                timeout=10,
            )
        except requests.RequestException as exc:
            log.warning("Telegram недоступен: %s", exc)

    # --- жизненный цикл ------------------------------------------------------

    def _poll_once(self) -> None:
        response = self.session.get(
            f"{self._url}/getUpdates",
            params={"offset": self._offset, "timeout": self.poll_timeout,
                    "allowed_updates": '["message"]'},
            timeout=self.poll_timeout + 10,
        )
        if not response.ok:
            self._stop_event.wait(5)
            return
        for update in response.json().get("result", []):
            self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
            self.handle_update(update)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except requests.RequestException:
                self._stop_event.wait(5)
            except Exception:
                log.exception("Ошибка Telegram-листенера")
                self._stop_event.wait(5)

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
