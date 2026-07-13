from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import json

import pandas as pd
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse

from .backtest import run_backtest
from .binance_client import BinanceFuturesClient, BinanceAPIError
from .config import Settings

app = FastAPI(title="Intraday Bot Control Panel", version="0.3")
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)


def page(body: str, title: str = "Trading Bot") -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html>
<html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{title}</title>
<style>
body{{font-family:Arial,sans-serif;background:#0b1020;color:#eef2ff;margin:0}}main{{max-width:1100px;margin:auto;padding:24px}}
.card{{background:#151c31;border:1px solid #2b3553;border-radius:16px;padding:20px;margin:16px 0}}h1,h2{{margin-top:0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px}}label{{font-size:13px;color:#b8c2df}}
input,select{{width:100%;box-sizing:border-box;margin-top:6px;padding:10px;border-radius:9px;border:1px solid #3a4668;background:#0e1528;color:white}}
button{{padding:12px 18px;border:0;border-radius:10px;font-weight:700;cursor:pointer}}.primary{{background:#f0b90b;color:#111827}}
.metric{{font-size:25px;font-weight:700}}.muted{{color:#9aa6c8}}.good{{color:#4ade80}}.bad{{color:#fb7185}}
table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{padding:8px;border-bottom:1px solid #303a58;text-align:right}}th:first-child,td:first-child{{text-align:left}}
.warn{{border-left:4px solid #f0b90b;padding-left:12px}}a{{color:#f0b90b}}
</style></head><body><main>{body}</main></body></html>""")


@app.get("/", response_class=HTMLResponse)
def home():
    s = Settings()
    return page(f"""
<h1>Binance Intraday Bot — панель управления v0.3</h1>
<div class='card warn'><b>Безопасный режим:</b> эта панель запускает анализ и backtest. Реальные ордера заблокированы.</div>
<div class='card'><h2>Запустить backtest</h2>
<form method='post' action='/backtest'>
<div class='grid'>
<div><label>Инструмент<input name='symbol' value='BTCUSDT'></label></div>
<div><label>Начальный баланс, USDT<input type='number' step='0.01' name='balance' value='1000'></label></div>
<div><label>Свечей для теста<input type='number' name='limit' min='300' max='1500' value='1500'></label></div>
<div><label>Плечо<input type='number' name='leverage' min='1' max='5' value='{s.leverage}'></label></div>
<div><label>Риск на сделку, %<input type='number' step='0.01' name='risk_pct' value='{s.risk_per_trade*100}'></label></div>
<div><label>Reward/Risk<input type='number' step='0.1' name='reward_risk' value='{s.reward_risk}'></label></div>
<div><label>EMA быстрая<input type='number' name='ema_fast' value='{s.ema_fast}'></label></div>
<div><label>EMA медленная<input type='number' name='ema_slow' value='{s.ema_slow}'></label></div>
<div><label>EMA тренда<input type='number' name='ema_trend' value='{s.ema_trend}'></label></div>
<div><label>ATR период<input type='number' name='atr_period' value='{s.atr_period}'></label></div>
<div><label>Мин. ATR, %<input type='number' step='0.01' name='atr_min_pct' value='{s.atr_min_pct*100}'></label></div>
<div><label>Множитель объёма<input type='number' step='0.05' name='volume_multiplier' value='{s.volume_multiplier}'></label></div>
<div><label>Комиссия taker, %<input type='number' step='0.001' name='fee_pct' value='0.05'></label></div>
<div><label>Проскальзывание, %<input type='number' step='0.001' name='slippage_pct' value='0.02'></label></div>
</div><p><button class='primary' type='submit'>Запустить тест</button></p></form></div>
<div class='card'><h2>Как будет работать готовая система</h2><p>1. Вы задаёте параметры в браузере. 2. Панель тестирует их на истории. 3. После проверки сохраняете профиль стратегии. 4. Testnet-модуль запускает бот. 5. Отдельный защищённый переключатель сможет включить реальные сделки.</p></div>
""")


@app.post("/backtest", response_class=HTMLResponse)
def backtest(
    symbol: str = Form("BTCUSDT"), balance: float = Form(1000), limit: int = Form(1500),
    leverage: int = Form(3), risk_pct: float = Form(0.25), reward_risk: float = Form(1.8),
    ema_fast: int = Form(20), ema_slow: int = Form(50), ema_trend: int = Form(200),
    atr_period: int = Form(14), atr_min_pct: float = Form(0.2),
    volume_multiplier: float = Form(1.15), fee_pct: float = Form(0.05),
    slippage_pct: float = Form(0.02),
):
    symbol = symbol.upper().strip()
    if not symbol.endswith("USDT"):
        raise HTTPException(400, "Пока поддерживаются пары USDT")
    base = Settings()
    settings = replace(base, leverage=leverage, risk_per_trade=risk_pct/100,
        reward_risk=reward_risk, ema_fast=ema_fast, ema_slow=ema_slow,
        ema_trend=ema_trend, atr_period=atr_period, atr_min_pct=atr_min_pct/100,
        volume_multiplier=volume_multiplier)
    settings.validate()
    try:
        client = BinanceFuturesClient(testnet=False)
        signal_df = client.klines(symbol, settings.signal_interval, min(limit, 1500))
        trend_df = client.klines(symbol, settings.trend_interval, min(limit, 1500))
    except BinanceAPIError as exc:
        return page(f"<h1>Не удалось загрузить данные</h1><div class='card bad'>{exc}</div><p><a href='/'>Назад</a></p>")
    result = run_backtest(signal_df, trend_df, settings, balance, fee_pct/100, slippage_pct/100)
    trades_path = DATA_DIR / f"{symbol}_web_trades.csv"
    summary_path = DATA_DIR / f"{symbol}_web_summary.json"
    pd.DataFrame([t.__dict__ for t in result.trade_log]).to_csv(trades_path, index=False)
    summary_path.write_text(json.dumps(result.summary(), ensure_ascii=False, indent=2), encoding="utf-8")
    cls = "good" if result.net_profit > 0 else "bad"
    rows = "".join(f"<tr><td>{t.side}</td><td>{t.entry_time[:16]}</td><td>{t.exit_time[:16]}</td><td>{t.entry:.2f}</td><td>{t.exit:.2f}</td><td>{t.pnl_usdt:.2f}</td><td>{t.reason}</td></tr>" for t in result.trade_log[-30:])
    pf = "∞" if result.profit_factor == float('inf') else f"{result.profit_factor:.2f}"
    return page(f"""
<h1>Результат: {symbol}</h1><p><a href='/'>← Изменить параметры</a></p>
<div class='grid'>
<div class='card'><div class='muted'>Итоговый баланс</div><div class='metric {cls}'>{result.final_balance:.2f} USDT</div></div>
<div class='card'><div class='muted'>Доходность</div><div class='metric {cls}'>{result.return_pct:.2f}%</div></div>
<div class='card'><div class='muted'>Сделок</div><div class='metric'>{result.trades}</div></div>
<div class='card'><div class='muted'>Win rate</div><div class='metric'>{result.win_rate:.1f}%</div></div>
<div class='card'><div class='muted'>Profit Factor</div><div class='metric'>{pf}</div></div>
<div class='card'><div class='muted'>Макс. просадка</div><div class='metric'>{result.max_drawdown_pct:.2f}%</div></div>
</div>
<div class='card'><b>Издержки:</b> комиссии {result.total_fees:.2f} USDT, моделируемое проскальзывание {result.total_slippage:.2f} USDT.<br><a href='/download/{trades_path.name}'>Скачать сделки CSV</a> · <a href='/download/{summary_path.name}'>Скачать отчёт JSON</a></div>
<div class='card'><h2>Последние сделки</h2><div style='overflow:auto'><table><tr><th>Side</th><th>Вход</th><th>Выход</th><th>Entry</th><th>Exit</th><th>PnL</th><th>Причина</th></tr>{rows}</table></div></div>
""", f"Backtest {symbol}")


@app.get("/download/{filename}")
def download(filename: str):
    path = DATA_DIR / Path(filename).name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, filename=path.name)
