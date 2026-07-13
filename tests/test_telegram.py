import json

from src.telegram_notify import TelegramCommandListener, TelegramNotifier


class FakeResponse:
    def __init__(self, ok=True, payload=None, status_code=200):
        self.ok = ok
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.posts: list[dict] = []
        self.fail = False

    def post(self, url, json=None, timeout=None):
        if self.fail:
            import requests
            raise requests.ConnectionError("down")
        self.posts.append({"url": url, "json": json})
        return FakeResponse()

    def get(self, url, params=None, timeout=None):
        return FakeResponse(payload={"result": []})


def test_notifier_disabled_without_config():
    notifier = TelegramNotifier("", "", session=FakeSession())
    assert notifier.enabled is False
    assert notifier.send("test") is False


def test_notifier_sends_message():
    session = FakeSession()
    notifier = TelegramNotifier("token123", "42", session=session)
    assert notifier.send("привет") is True
    assert len(session.posts) == 1
    assert session.posts[0]["json"]["chat_id"] == "42"
    assert session.posts[0]["json"]["text"] == "привет"
    assert "bottoken123/sendMessage" in session.posts[0]["url"]


def test_notifier_survives_network_failure():
    session = FakeSession()
    session.fail = True
    notifier = TelegramNotifier("token123", "42", session=session)
    assert notifier.send("msg") is False  # не бросает исключение


def make_listener(session=None, stopped=None, chat_id="42"):
    stopped = stopped if stopped is not None else []
    listener = TelegramCommandListener(
        "token123", chat_id,
        on_stop=lambda: stopped.append(True),
        on_status=lambda: "статус ок",
        session=session or FakeSession(),
    )
    return listener, stopped


def update(chat_id, text, uid=1):
    return {"update_id": uid, "message": {"chat": {"id": chat_id}, "text": text}}


def test_listener_stop_command_from_owner():
    session = FakeSession()
    listener, stopped = make_listener(session)
    assert listener.handle_update(update(42, "/stop")) == "stop"
    assert stopped == [True]
    texts = [p["json"]["text"] for p in session.posts]
    assert any("останавливаю" in t for t in texts)
    assert any("Готово" in t for t in texts)


def test_listener_panic_alias():
    listener, stopped = make_listener()
    assert listener.handle_update(update(42, "/PANIC")) == "stop"
    assert stopped == [True]


def test_listener_ignores_foreign_chat():
    listener, stopped = make_listener()
    assert listener.handle_update(update(999, "/stop")) is None
    assert stopped == []


def test_listener_status_command():
    session = FakeSession()
    listener, stopped = make_listener(session)
    assert listener.handle_update(update(42, "/status")) == "status"
    assert stopped == []
    assert session.posts[-1]["json"]["text"] == "статус ок"


def test_listener_reports_stop_failure():
    session = FakeSession()
    listener = TelegramCommandListener(
        "token123", "42",
        on_stop=lambda: (_ for _ in ()).throw(RuntimeError("биржа недоступна")),
        on_status=lambda: "",
        session=session,
    )
    assert listener.handle_update(update(42, "/stop")) == "stop"
    texts = [p["json"]["text"] for p in session.posts]
    assert any("ОШИБКА" in t for t in texts)


def test_engine_sends_notifications(tmp_path):
    """Engine шлёт уведомления об открытии позиции и аварийном закрытии."""
    from tests.test_engine import FakeClient, make_testnet_settings
    from src.journal import Journal
    from src.testnet_engine import TestnetEngine

    class CollectingNotifier:
        def __init__(self):
            self.messages: list[str] = []

        def send(self, text):
            self.messages.append(text)
            return True

    notifier = CollectingNotifier()
    client = FakeClient()
    engine = TestnetEngine(make_testnet_settings(), client,
                           Journal(str(tmp_path / "t.db")), notifier=notifier)
    engine.prepare()
    assert engine.process_symbol("BTCUSDT") == "OPENED"
    assert any("открыта позиция" in m for m in notifier.messages)

    notifier2 = CollectingNotifier()
    client2 = FakeClient(fail_sl=True)
    engine2 = TestnetEngine(make_testnet_settings(), client2,
                            Journal(str(tmp_path / "t2.db")), notifier=notifier2)
    engine2.prepare()
    assert engine2.process_symbol("BTCUSDT") == "EMERGENCY_CLOSED"
    assert any("CRITICAL" in m for m in notifier2.messages)


def test_engine_does_not_spam_blocked_signal(tmp_path):
    """Сигнал, заблокированный лимитами, обрабатывается один раз, не каждый цикл."""
    from tests.test_engine import FakeClient, make_testnet_settings
    from src.journal import Journal
    from src.testnet_engine import TestnetEngine, _utc_day

    client = FakeClient()
    journal = Journal(str(tmp_path / "t.db"))
    engine = TestnetEngine(make_testnet_settings(), client, journal)
    engine.prepare()
    day = _utc_day()
    journal.record_trade_open(day, "BTCUSDT", "LONG", "tb-x-1", 1, 100, 99, 102)
    journal.close_trade("tb-x-1", "CLOSED", realized_pnl=-100.0)

    assert engine.process_symbol("BTCUSDT") == "LIMITS"
    assert engine.process_symbol("BTCUSDT") == "DUPLICATE_SKIPPED"
    events = [e for e in journal.recent_events(50) if "вход пропущен" in e["message"]]
    assert len(events) == 1
