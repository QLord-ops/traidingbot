from __future__ import annotations

import html
import json
import re
import threading
import uuid
from dataclasses import replace
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse

from .backtest import BacktestParams, BacktestResult, run_backtest, walk_forward
from .binance_client import BinanceFuturesClient, BinanceAPIError
from .config import Settings
from .data import get_klines, get_funding_rates
from .journal import Journal
from .telegram_notify import TelegramCommandListener, TelegramNotifier
from .testnet_engine import TestnetEngine, EngineError

app = FastAPI(title="Intraday Bot Control Panel", version="0.6")
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
PROFILES_PATH = DATA_DIR / "profiles.json"
RUNS_PATH = DATA_DIR / "runs_history.json"

SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")

_engine: TestnetEngine | None = None
_listener: TelegramCommandListener | None = None
_engine_lock = threading.Lock()
_files_lock = threading.Lock()


def esc(value) -> str:
    return html.escape(str(value), quote=True)


# ---------------------------------------------------------------- шаблон ----

def page(body: str, title: str = "Trading Bot") -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html>
<html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{esc(title)}</title>
<style>
body{{font-family:Arial,sans-serif;background:#0b1020;color:#eef2ff;margin:0}}main{{max-width:1150px;margin:auto;padding:24px}}
.card{{background:#151c31;border:1px solid #2b3553;border-radius:16px;padding:20px;margin:16px 0}}h1,h2{{margin-top:0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px}}label{{font-size:13px;color:#b8c2df}}
input,select{{width:100%;box-sizing:border-box;margin-top:6px;padding:10px;border-radius:9px;border:1px solid #3a4668;background:#0e1528;color:white}}
button{{padding:12px 18px;border:0;border-radius:10px;font-weight:700;cursor:pointer;margin-right:8px}}
.primary{{background:#f0b90b;color:#111827}}.danger{{background:#dc2626;color:white}}.secondary{{background:#334155;color:#e2e8f0}}
.metric{{font-size:24px;font-weight:700}}.muted{{color:#9aa6c8}}.good{{color:#4ade80}}.bad{{color:#fb7185}}
table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{padding:8px;border-bottom:1px solid #303a58;text-align:right}}th:first-child,td:first-child{{text-align:left}}
.warn{{border-left:4px solid #f0b90b;padding-left:12px}}.crit{{border-left:4px solid #dc2626;padding-left:12px}}a{{color:#f0b90b}}
nav a{{margin-right:16px}}svg{{width:100%;height:auto;display:block}}
</style></head><body><main>
<nav><a href='/'>Backtest</a><a href='/compare'>Сравнение прогонов</a><a href='/testnet'>Testnet</a></nav>
{body}</main></body></html>""")


def svg_chart(points: list[tuple[str, float]], color: str = "#4ade80",
              height: int = 220, invert: bool = False) -> str:
    """Простой линейный график без внешних зависимостей."""
    if len(points) < 2:
        return "<p class='muted'>Недостаточно данных для графика</p>"
    step = max(1, len(points) // 800)
    pts = points[::step]
    values = [v for _, v in pts]
    vmin, vmax = min(values), max(values)
    spread = (vmax - vmin) or 1.0
    width = 1000
    coords = []
    for idx, (_, v) in enumerate(pts):
        x = idx / (len(pts) - 1) * width
        norm = (v - vmin) / spread
        y = norm * (height - 20) + 10 if invert else (1 - norm) * (height - 20) + 10
        coords.append(f"{x:.1f},{y:.1f}")
    first_t, last_t = pts[0][0][:10], pts[-1][0][:10]
    return f"""<svg viewBox='0 0 {width} {height}' preserveAspectRatio='none'>
<polyline fill='none' stroke='{color}' stroke-width='2' points='{" ".join(coords)}'/>
</svg><div class='muted' style='display:flex;justify-content:space-between'>
<span>{esc(first_t)}</span><span>мин {vmin:.2f} · макс {vmax:.2f}</span><span>{esc(last_t)}</span></div>"""


# ------------------------------------------------------------- профили ----

def load_profiles() -> dict:
    if PROFILES_PATH.exists():
        try:
            return json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_profiles(profiles: dict) -> None:
    with _files_lock:
        PROFILES_PATH.write_text(
            json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def load_runs() -> list[dict]:
    if RUNS_PATH.exists():
        try:
            return json.loads(RUNS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def append_run(entry: dict) -> None:
    with _files_lock:
        runs = load_runs()
        runs.append(entry)
        RUNS_PATH.write_text(
            json.dumps(runs[-30:], ensure_ascii=False, indent=2), encoding="utf-8"
        )


FORM_FIELDS = [
    "symbol", "balance", "start_date", "end_date", "leverage", "risk_pct",
    "reward_risk", "ema_fast", "ema_slow", "ema_trend", "atr_period",
    "atr_min_pct", "volume_multiplier", "taker_fee_pct", "maker_fee_pct",
    "slippage_pct", "use_funding", "wf_windows",
]


def form_html(values: dict, profiles: dict) -> str:
    v = {
        "symbol": "BTCUSDT", "balance": 1000, "start_date": "", "end_date": "",
        "leverage": 3, "risk_pct": 0.25, "reward_risk": 1.8, "ema_fast": 20,
        "ema_slow": 50, "ema_trend": 200, "atr_period": 14, "atr_min_pct": 0.2,
        "volume_multiplier": 1.15, "taker_fee_pct": 0.05, "maker_fee_pct": 0.02,
        "slippage_pct": 0.02, "use_funding": "on", "wf_windows": 0,
    }
    v.update({k: values[k] for k in values if k in v and values[k] is not None})
    profile_options = "".join(
        f"<option value='{esc(name)}'>{esc(name)}</option>" for name in sorted(profiles)
    )
    funding_checked = "checked" if str(v["use_funding"]).lower() in ("on", "true", "1") else ""
    return f"""
<div class='card'><h2>Профили стратегии</h2>
<form method='post' action='/profile/load' style='display:flex;gap:10px;align-items:end;flex-wrap:wrap'>
<div style='flex:1;min-width:200px'><label>Сохранённый профиль<select name='name'>{profile_options or "<option value=''>нет профилей</option>"}</select></label></div>
<button class='secondary' type='submit'>Загрузить</button>
<button class='danger' formaction='/profile/delete' type='submit'>Удалить</button>
</form></div>
<div class='card'><h2>Параметры backtest</h2>
<form method='post' action='/backtest'>
<div class='grid'>
<div><label>Инструмент<input name='symbol' value='{esc(v["symbol"])}' pattern='[A-Za-z0-9]+' required></label></div>
<div><label>Начальный баланс, USDT<input type='number' step='0.01' min='10' name='balance' value='{esc(v["balance"])}'></label></div>
<div><label>Дата начала (UTC)<input type='date' name='start_date' value='{esc(v["start_date"])}'></label></div>
<div><label>Дата конца (UTC)<input type='date' name='end_date' value='{esc(v["end_date"])}'></label></div>
<div><label>Плечо (1–3)<input type='number' name='leverage' min='1' max='3' value='{esc(v["leverage"])}'></label></div>
<div><label>Риск на сделку, %<input type='number' step='0.01' min='0.01' max='1' name='risk_pct' value='{esc(v["risk_pct"])}'></label></div>
<div><label>Reward/Risk<input type='number' step='0.1' min='0.1' name='reward_risk' value='{esc(v["reward_risk"])}'></label></div>
<div><label>EMA быстрая<input type='number' name='ema_fast' min='2' value='{esc(v["ema_fast"])}'></label></div>
<div><label>EMA медленная<input type='number' name='ema_slow' min='3' value='{esc(v["ema_slow"])}'></label></div>
<div><label>EMA тренда<input type='number' name='ema_trend' min='10' value='{esc(v["ema_trend"])}'></label></div>
<div><label>ATR период<input type='number' name='atr_period' min='2' value='{esc(v["atr_period"])}'></label></div>
<div><label>Мин. ATR, %<input type='number' step='0.01' min='0' name='atr_min_pct' value='{esc(v["atr_min_pct"])}'></label></div>
<div><label>Множитель объёма<input type='number' step='0.05' min='0.1' name='volume_multiplier' value='{esc(v["volume_multiplier"])}'></label></div>
<div><label>Комиссия taker, %<input type='number' step='0.001' min='0' name='taker_fee_pct' value='{esc(v["taker_fee_pct"])}'></label></div>
<div><label>Комиссия maker, %<input type='number' step='0.001' min='0' name='maker_fee_pct' value='{esc(v["maker_fee_pct"])}'></label></div>
<div><label>Проскальзывание, %<input type='number' step='0.001' min='0' name='slippage_pct' value='{esc(v["slippage_pct"])}'></label></div>
<div><label>Walk-forward окон (0 = выкл)<input type='number' name='wf_windows' min='0' max='8' value='{esc(v["wf_windows"])}'></label></div>
<div><label>Funding fees<br><input type='checkbox' name='use_funding' style='width:auto' {funding_checked}> учитывать</label></div>
</div>
<p>
<button class='primary' type='submit'>Запустить backtest</button>
<input name='profile_name' placeholder='имя профиля' style='width:180px;display:inline-block'>
<button class='secondary' formaction='/profile/save' type='submit'>Сохранить профиль</button>
</p></form></div>"""


def parse_form_settings(base: Settings, leverage: int, risk_pct: float,
                        reward_risk: float, ema_fast: int, ema_slow: int,
                        ema_trend: int, atr_period: int, atr_min_pct: float,
                        volume_multiplier: float) -> Settings:
    settings = replace(
        base, leverage=leverage, risk_per_trade=risk_pct / 100,
        reward_risk=reward_risk, ema_fast=ema_fast, ema_slow=ema_slow,
        ema_trend=ema_trend, atr_period=atr_period, atr_min_pct=atr_min_pct / 100,
        volume_multiplier=volume_multiplier,
    )
    settings.validate()
    return settings


def overfitting_warnings(result: BacktestResult, wf: list | None) -> str:
    warnings = []
    if result.trades < 30:
        warnings.append(
            f"Всего {result.trades} сделок — статистически ничтожная выборка. "
            "Любой вывод о прибыльности недостоверен."
        )
    if result.profit_factor > 3 and result.trades < 100:
        warnings.append(
            "Слишком высокий Profit Factor на малой выборке — типичный признак "
            "переоптимизации под конкретный период."
        )
    if wf:
        profitable = sum(1 for w in wf if w.result.net_profit > 0)
        if profitable < len(wf):
            warnings.append(
                f"Walk-forward: прибыльны только {profitable} из {len(wf)} окон. "
                "Стратегия неустойчива по периодам."
            )
    warnings.append(
        "Результат прошлого не гарантирует будущего. Реальное исполнение "
        "(спред, ликвидность, funding) обычно хуже модели."
    )
    return "".join(f"<div class='card warn'>{esc(w)}</div>" for w in warnings)


# --------------------------------------------------------------- страницы ----

@app.get("/health")
def health():
    return {"status": "ok", "version": app.version}


@app.get("/", response_class=HTMLResponse)
def home():
    profiles = load_profiles()
    return page(f"""
<h1>Binance Intraday Bot — панель управления v0.6</h1>
<div class='card warn'><b>Безопасный режим:</b> панель выполняет backtest по публичным данным
и управляет Testnet (Demo Trading). Реальные ордера жёстко заблокированы.</div>
{form_html({}, profiles)}
""")


@app.post("/backtest", response_class=HTMLResponse)
def backtest_endpoint(
    symbol: str = Form("BTCUSDT"), balance: float = Form(1000),
    start_date: str = Form(""), end_date: str = Form(""),
    leverage: int = Form(3), risk_pct: float = Form(0.25),
    reward_risk: float = Form(1.8), ema_fast: int = Form(20),
    ema_slow: int = Form(50), ema_trend: int = Form(200),
    atr_period: int = Form(14), atr_min_pct: float = Form(0.2),
    volume_multiplier: float = Form(1.15), taker_fee_pct: float = Form(0.05),
    maker_fee_pct: float = Form(0.02), slippage_pct: float = Form(0.02),
    use_funding: str = Form(""), wf_windows: int = Form(0),
):
    symbol = symbol.upper().strip()
    if not SYMBOL_RE.match(symbol):
        raise HTTPException(400, "Недопустимый символ: ожидается например BTCUSDT")
    if balance < 10:
        raise HTTPException(400, "Баланс слишком мал")
    try:
        settings = parse_form_settings(
            Settings(), leverage, risk_pct, reward_risk, ema_fast, ema_slow,
            ema_trend, atr_period, atr_min_pct, volume_multiplier,
        )
    except ValueError as exc:
        return page(f"<h1>Ошибка параметров</h1><div class='card crit'>{esc(exc)}</div>"
                    "<p><a href='/'>Назад</a></p>")

    now = pd.Timestamp.now(tz="UTC")
    end_ts = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1) if end_date else now
    start_ts = pd.Timestamp(start_date, tz="UTC") if start_date else end_ts - pd.Timedelta(days=180)
    if start_ts >= end_ts:
        raise HTTPException(400, "Дата начала должна быть раньше даты конца")
    # запас для прогрева EMA тренда
    warmup = pd.Timedelta(minutes=15 * (settings.ema_trend + settings.volume_period + 10))
    fetch_start = int((start_ts - warmup * 4).timestamp() * 1000)
    fetch_end = int(end_ts.timestamp() * 1000)

    params = BacktestParams(
        initial_balance=balance,
        taker_fee_rate=taker_fee_pct / 100,
        maker_fee_rate=maker_fee_pct / 100,
        slippage_rate=slippage_pct / 100,
        apply_funding=bool(use_funding),
    )
    try:
        client = BinanceFuturesClient(testnet=False)  # только публичные данные
        signal_df = get_klines(client, symbol, settings.signal_interval, fetch_start, fetch_end)
        trend_df = get_klines(client, symbol, settings.trend_interval, fetch_start, fetch_end)
        funding_df = (
            get_funding_rates(client, symbol, fetch_start, fetch_end)
            if params.apply_funding else None
        )
    except BinanceAPIError as exc:
        return page(f"<h1>Не удалось загрузить данные</h1><div class='card crit'>{esc(exc)}</div>"
                    "<p><a href='/'>Назад</a></p>")
    if len(signal_df) < settings.ema_trend + 20:
        return page("<h1>Недостаточно данных</h1><div class='card crit'>"
                    "Слишком короткий диапазон для выбранных периодов EMA.</div>"
                    "<p><a href='/'>Назад</a></p>")

    result = run_backtest(signal_df, trend_df, settings, params, funding_df,
                          trade_start=start_ts, trade_end=end_ts)
    wf = None
    if wf_windows >= 2:
        wf = walk_forward(signal_df, trend_df, settings, params, funding_df,
                          n_windows=wf_windows)

    run_id = uuid.uuid4().hex[:8]
    trades_path = DATA_DIR / f"{symbol}_{run_id}_trades.csv"
    summary_path = DATA_DIR / f"{symbol}_{run_id}_summary.json"
    pd.DataFrame([t.__dict__ for t in result.trade_log]).to_csv(trades_path, index=False)
    summary_path.write_text(
        json.dumps(result.summary(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    append_run({
        "run_id": run_id, "symbol": symbol,
        "period": f"{start_ts.date()} — {(end_ts - pd.Timedelta(days=1)).date()}",
        "params": {
            "leverage": leverage, "risk_pct": risk_pct, "reward_risk": reward_risk,
            "ema": f"{ema_fast}/{ema_slow}/{ema_trend}", "atr_min_pct": atr_min_pct,
            "vol_mult": volume_multiplier, "funding": bool(use_funding),
        },
        "return_pct": round(result.return_pct, 2),
        "max_dd_pct": round(result.max_drawdown_pct, 2),
        "trades": result.trades, "win_rate": round(result.win_rate, 1),
        "profit_factor": (round(result.profit_factor, 2)
                          if result.profit_factor != float("inf") else "inf"),
        "avg_r": round(result.avg_r, 2),
    })

    cls = "good" if result.net_profit > 0 else "bad"
    pf = "∞" if result.profit_factor == float("inf") else f"{result.profit_factor:.2f}"
    rows = "".join(
        f"<tr><td>{esc(t.side)}</td><td>{esc(t.entry_time[:16])}</td>"
        f"<td>{esc(t.exit_time[:16])}</td><td>{t.entry:.2f}</td><td>{t.exit:.2f}</td>"
        f"<td>{t.qty:.4f}</td><td class='{ "good" if t.pnl_usdt > 0 else "bad" }'>{t.pnl_usdt:.2f}</td>"
        f"<td>{t.r_multiple:.2f}R</td><td>{esc(t.reason)}</td></tr>"
        for t in result.trade_log[-40:]
    )
    monthly_rows = "".join(
        f"<tr><td>{esc(month)}</td><td class='{ "good" if ret > 0 else "bad" }'>{ret:.2f}%</td></tr>"
        for month, ret in result.monthly_returns.items()
    )
    wf_html = ""
    if wf:
        wf_rows = "".join(
            f"<tr><td>{esc(w.label)}</td><td>{esc(w.start[:10])} — {esc(w.end[:10])}</td>"
            f"<td class='{ "good" if w.result.net_profit > 0 else "bad" }'>{w.result.return_pct:.2f}%</td>"
            f"<td>{w.result.trades}</td><td>{w.result.win_rate:.1f}%</td>"
            f"<td>{w.result.max_drawdown_pct:.2f}%</td></tr>"
            for w in wf
        )
        wf_html = f"""<div class='card'><h2>Walk-forward по окнам</h2>
<table><tr><th>Окно</th><th>Период</th><th>Доходность</th><th>Сделок</th><th>Win rate</th><th>Просадка</th></tr>{wf_rows}</table>
<p class='muted'>Каждое окно тестируется независимо. Устойчивая стратегия должна быть
близка к нулю или в плюсе в большинстве окон, а не только суммарно.</p></div>"""

    return page(f"""
<h1>Результат: {esc(symbol)} <span class='muted'>({esc(start_ts.date())} — {esc((end_ts - pd.Timedelta(days=1)).date())})</span></h1>
<p><a href='/'>← Изменить параметры</a></p>
{overfitting_warnings(result, wf)}
<div class='grid'>
<div class='card'><div class='muted'>Итоговый баланс</div><div class='metric {cls}'>{result.final_balance:.2f} USDT</div></div>
<div class='card'><div class='muted'>Доходность</div><div class='metric {cls}'>{result.return_pct:.2f}%</div></div>
<div class='card'><div class='muted'>Сделок</div><div class='metric'>{result.trades}</div></div>
<div class='card'><div class='muted'>Win rate</div><div class='metric'>{result.win_rate:.1f}%</div></div>
<div class='card'><div class='muted'>Profit Factor</div><div class='metric'>{pf}</div></div>
<div class='card'><div class='muted'>Макс. просадка</div><div class='metric'>{result.max_drawdown_pct:.2f}%</div></div>
<div class='card'><div class='muted'>Expectancy</div><div class='metric'>{result.expectancy_usdt:.2f} USDT</div></div>
<div class='card'><div class='muted'>Средний R</div><div class='metric'>{result.avg_r:.2f}R</div></div>
<div class='card'><div class='muted'>Серия убытков (макс)</div><div class='metric'>{result.max_consecutive_losses}</div></div>
</div>
<div class='card'><h2>Кривая баланса (equity)</h2>{svg_chart(result.equity_curve, "#4ade80")}</div>
<div class='card'><h2>Просадка, %</h2>{svg_chart(result.drawdown_curve, "#fb7185", invert=True)}</div>
<div class='card'><h2>Доходность по месяцам</h2><table><tr><th>Месяц</th><th>Доходность</th></tr>{monthly_rows or "<tr><td colspan='2'>нет данных</td></tr>"}</table></div>
{wf_html}
<div class='card'><b>Издержки:</b> комиссии {result.total_fees:.2f} USDT ·
проскальзывание {result.total_slippage:.2f} USDT · funding {result.total_funding:+.2f} USDT<br>
<b>Лимиты:</b> входов пропущено из-за дневного стопа: {result.skipped_by_daily_loss},
из-за лимита сделок в день: {result.skipped_by_trade_limit}<br>
<a href='/download/{esc(trades_path.name)}'>Скачать сделки CSV</a> ·
<a href='/download/{esc(summary_path.name)}'>Скачать отчёт JSON</a></div>
<div class='card'><h2>Последние сделки</h2><div style='overflow:auto'>
<table><tr><th>Side</th><th>Вход</th><th>Выход</th><th>Entry</th><th>Exit</th><th>Кол-во</th><th>PnL</th><th>R</th><th>Причина</th></tr>{rows}</table></div></div>
""", f"Backtest {symbol}")


# --------------------------------------------------------------- профили ----

def _collect_profile_fields(form: dict) -> dict:
    return {k: form.get(k) for k in FORM_FIELDS}


@app.post("/profile/save", response_class=HTMLResponse)
async def profile_save(request: Request):
    form = dict(await request.form())
    name = (form.get("profile_name") or "").strip()
    if not name or len(name) > 60:
        return page("<h1>Укажите имя профиля (до 60 символов)</h1><p><a href='/'>Назад</a></p>")
    profiles = load_profiles()
    profiles[name] = _collect_profile_fields(form)
    save_profiles(profiles)
    return page(f"<h1>Профиль «{esc(name)}» сохранён</h1><p><a href='/'>← Назад</a></p>")


@app.post("/profile/load", response_class=HTMLResponse)
async def profile_load(request: Request):
    form = dict(await request.form())
    name = form.get("name", "")
    profiles = load_profiles()
    if name not in profiles:
        return page("<h1>Профиль не найден</h1><p><a href='/'>Назад</a></p>")
    return page(f"<h1>Профиль «{esc(name)}»</h1>{form_html(profiles[name], profiles)}")


@app.post("/profile/delete", response_class=HTMLResponse)
async def profile_delete(request: Request):
    form = dict(await request.form())
    name = form.get("name", "")
    profiles = load_profiles()
    if name in profiles:
        del profiles[name]
        save_profiles(profiles)
    return RedirectResponse("/", status_code=303)


# -------------------------------------------------------------- сравнение ----

@app.get("/compare", response_class=HTMLResponse)
def compare():
    runs = load_runs()
    if not runs:
        return page("<h1>Сравнение прогонов</h1><div class='card'>Пока нет сохранённых "
                    "прогонов. Запустите backtest.</div>")
    rows = "".join(
        f"<tr><td>{esc(r['symbol'])}</td><td>{esc(r['period'])}</td>"
        f"<td>{esc(r['params']['ema'])}</td><td>{esc(r['params']['risk_pct'])}%</td>"
        f"<td>{esc(r['params']['reward_risk'])}</td>"
        f"<td>{'да' if r['params'].get('funding') else 'нет'}</td>"
        f"<td class='{ 'good' if r['return_pct'] > 0 else 'bad' }'>{esc(r['return_pct'])}%</td>"
        f"<td>{esc(r['max_dd_pct'])}%</td><td>{esc(r['trades'])}</td>"
        f"<td>{esc(r['win_rate'])}%</td><td>{esc(r['profit_factor'])}</td>"
        f"<td>{esc(r['avg_r'])}R</td></tr>"
        for r in reversed(load_runs())
    )
    return page(f"""
<h1>Сравнение прогонов</h1>
<div class='card warn'>Сравнение множества параметров на одном и том же периоде —
это подбор под историю. Выбранный набор обязан подтвердиться на out-of-sample
периоде и walk-forward, иначе он не значит ничего.</div>
<div class='card'><div style='overflow:auto'><table>
<tr><th>Символ</th><th>Период</th><th>EMA</th><th>Риск</th><th>R/R</th><th>Funding</th>
<th>Доходность</th><th>Просадка</th><th>Сделок</th><th>Win rate</th><th>PF</th><th>Avg R</th></tr>
{rows}</table></div></div>
""")


# ---------------------------------------------------------------- testnet ----

def get_engine() -> TestnetEngine | None:
    return _engine


@app.get("/testnet", response_class=HTMLResponse)
def testnet_page():
    settings = Settings()
    engine = get_engine()
    journal = Journal()
    events = journal.recent_events(30)
    keys_ok = bool(settings.api_key and settings.api_secret)
    mode_ok = settings.trading_mode == "testnet"

    if engine and engine.status.running:
        st = engine.status
        positions = "".join(
            f"<tr><td>{esc(p['symbol'])}</td><td>{esc(p['amt'])}</td>"
            f"<td>{esc(p['entry'])}</td><td>{esc(p['unrealized'])}</td></tr>"
            for p in st.positions
        ) or "<tr><td colspan='4'>нет открытых позиций</td></tr>"
        status_html = f"""
<div class='card'><h2>Статус: <span class='good'>работает</span></h2>
<p class='muted'>Запущен: {esc(st.started_at or '—')} · Последний цикл: {esc(st.last_cycle_at or '—')}</p>
<p>Баланс: <b>{st.balance_usdt:.2f} USDT</b> · Сделок сегодня: {st.trades_today} ·
PnL сегодня: {st.realized_pnl_today:+.2f} USDT ·
Дневная блокировка: {"<span class='bad'>ДА</span>" if st.day_locked else "нет"}</p>
{f"<p class='bad'>Последняя ошибка: {esc(st.last_error)}</p>" if st.last_error else ""}
<table><tr><th>Символ</th><th>Кол-во</th><th>Вход</th><th>Unrealized PnL</th></tr>{positions}</table>
<form method='post' action='/testnet/stop' style='margin-top:12px'>
<button class='secondary' type='submit'>Остановить</button>
<button class='danger' formaction='/testnet/emergency' type='submit'
onclick="return confirm('Закрыть все позиции и отменить все ордера?')">STOP — аварийное закрытие всего</button>
</form></div>"""
    else:
        problems = []
        if not mode_ok:
            problems.append("В .env установите TRADING_MODE=testnet")
        if not keys_ok:
            problems.append("В .env нужны BINANCE_API_KEY и BINANCE_API_SECRET от "
                            "Demo Trading (без права вывода средств)")
        problems_html = "".join(f"<div class='card crit'>{esc(p)}</div>" for p in problems)
        disabled = "disabled" if problems else ""
        status_html = f"""
<div class='card'><h2>Статус: <span class='muted'>остановлен</span></h2>
{problems_html}
<form method='post' action='/testnet/start'>
<button class='primary' type='submit' {disabled}>Запустить Testnet engine</button>
</form></div>"""

    tg_configured = bool(settings.telegram_bot_token and settings.telegram_chat_id)
    tg_html = (
        "<div class='card'>Telegram: <span class='good'>настроен</span> — уведомления "
        "о сделках и командах <code>/stop</code>, <code>/status</code> активны при "
        "работающем engine.</div>"
        if tg_configured else
        "<div class='card muted'>Telegram не настроен (опционально): задайте "
        "TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env, чтобы получать уведомления "
        "и иметь аварийную команду /stop из мессенджера.</div>"
    )

    event_rows = "".join(
        f"<tr><td>{esc(e['created_at'])}</td><td>{esc(e['level'])}</td>"
        f"<td style='text-align:left'>{esc(e['message'])}</td></tr>"
        for e in events
    ) or "<tr><td colspan='3'>журнал пуст</td></tr>"

    return page(f"""
<h1>Testnet (Demo Trading)</h1>
<div class='card warn'>Работает только с <b>demo-fapi.binance.com</b>. Реальный режим
заблокирован на уровне кода: engine отказывается стартовать с боевым API,
а <code>ENABLE_LIVE_ORDERS=true</code> приводит к ошибке конфигурации.</div>
{status_html}
{tg_html}
<div class='card'><h2>Журнал событий</h2><div style='overflow:auto'>
<table><tr><th>Время (UTC)</th><th>Уровень</th><th>Сообщение</th></tr>{event_rows}</table></div></div>
""", "Testnet")


def _emergency_stop_all() -> None:
    """Общий путь аварийной остановки для веб-кнопки и Telegram-команды."""
    engine = get_engine()
    if engine:
        engine.stop()
        engine.emergency_close_all()
    _stop_listener()


def _stop_listener() -> None:
    global _listener
    if _listener:
        _listener.stop()
        _listener = None


@app.post("/testnet/start")
def testnet_start():
    global _engine, _listener
    with _engine_lock:
        if _engine and _engine.status.running:
            return RedirectResponse("/testnet", status_code=303)
        settings = Settings()
        try:
            settings.validate()
            if settings.trading_mode != "testnet":
                raise EngineError("TRADING_MODE должен быть testnet")
            client = BinanceFuturesClient(
                api_key=settings.api_key, api_secret=settings.api_secret, testnet=True
            )
            notifier = TelegramNotifier(settings.telegram_bot_token,
                                        settings.telegram_chat_id)
            _engine = TestnetEngine(settings, client, Journal(), notifier=notifier)
            _engine.start()
            if notifier.enabled:
                _stop_listener()
                _listener = TelegramCommandListener(
                    settings.telegram_bot_token, settings.telegram_chat_id,
                    on_stop=_emergency_stop_all,
                    on_status=lambda: (_engine.status_text() if _engine
                                       else "Engine не запущен"),
                )
                _listener.start()
        except (EngineError, ValueError, BinanceAPIError) as exc:
            return page(f"<h1>Не удалось запустить engine</h1>"
                        f"<div class='card crit'>{esc(exc)}</div>"
                        "<p><a href='/testnet'>← Назад</a></p>")
    return RedirectResponse("/testnet", status_code=303)


@app.post("/testnet/stop")
def testnet_stop():
    engine = get_engine()
    if engine:
        engine.stop()
    _stop_listener()
    return RedirectResponse("/testnet", status_code=303)


@app.post("/testnet/emergency")
def testnet_emergency():
    _emergency_stop_all()
    return RedirectResponse("/testnet", status_code=303)


@app.get("/download/{filename}")
def download(filename: str):
    path = DATA_DIR / Path(filename).name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, filename=path.name)
