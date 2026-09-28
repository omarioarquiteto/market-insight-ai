# AGENTS.md — Market Insight AI

Ferramenta educacional de análise de opções binárias (IQ Option): indicadores técnicos, sinais por expiração, calendário econômico, backtest walk-forward e gestão de entradas (SQLite). Backend **FastAPI** + frontend estático SPA. Todo o código, comentários e texto de UI em **pt-BR**.

## Project

- Stack: Python 3.11 (`Dockerfile`/`requirements.txt`), FastAPI + uvicorn, pandas/numpy, `iqair` (cliente IQ Option), OpenAI-compatível opcional.
- Entry point: `app/main:app` (FastAPI) — subida via `python run.py` (bootstrap: cria `.env`, instala deps, inicia uvicorn com `--app-dir app`) ou `uvicorn main:app` a partir de `app/` (Docker usa `PYTHONPATH=/app/app`).
- Config via `.env` (ver `.env.example`): `IQ_EMAIL`/`IQ_PASSWORD`, `IQ_ACCOUNT_TYPE` (`PRACTICE`/`REAL`), `MARKET_DB_PATH`, `OPENAI_API_KEY`, `AI_BASE_URL`, `HOST`, `PORT`.
- Layout: código em `app/` (pacote com `__init__.py`), testes em `tests/`, documentação em `README.md`, infra na raiz (`Dockerfile`, `docker-compose.yml`, `setup.sh`, `setup.bat`).

## Commands

```bash
bash setup.sh / setup.bat              # cria .venv, instala deps, copia .env.example → .env
source .venv/bin/activate && python run.py          # roda o servidor (http://127.0.0.1:8000)
.venv/bin/python -m unittest discover -s tests -p "test_*.py"   # testes (usam sys.path para app/)
docker compose up --build                           # alternativa containerizada
```

Observações: não há linter/type-check configurado. `tests/test_iq.py` é script manual de conexão (exige credenciais reais no `.env`), não teste unitário. O SQLite local fica na raiz (`market.db`, gitignored); em serverless cai para `/tmp` ou memória.

## Architecture

- `app/main.py` — API FastAPI (`/api/*`): connect/login/status, oportunidades, candles, análise, backtest, radar, entradas/relatório, indicadores/parâmetros de estratégia, IA. Orquestra os demais módulos do pacote `app/`.
- `app/iq_service.py` — wrapper global da IQ Option (`iqoptionapi`): conexão única (`_api`), troca de conta, candles, compra binária, payout, `get_market_status` (aberto/fechado via `get_asset_metadata`, cache 60s). Estado em singleton com `RLock`.
- `app/analysis.py` — motor de análise: `TIMEFRAMES` (1/5/15min), `STRATEGIES` (trend_pullback, breakout, mean_reversion, support_resistance, momentum), indicadores (EMA, ADX, RSI, MACD, Bollinger…), cache `_SIGNAL_CACHE`, acerto histórico por janela rolante, `walkforward_asset` (backtest).
- `app/news_service.py` — calendário econômico Biquote (cache 60s + lock), `get_news_risk` bloqueia sinais perto de eventos de alto impacto.
- `app/trade_manager.py` — gestão de entradas: config (`set_config`/`get_estado_completo`), histórico/CRUD em SQLite (`market.db` na raiz via `_resolver_db_path`; fallback `/tmp`/memória em serverless), worker de apuração em thread, relatórios por período.
- `app/ai_advisor.py` — segunda opinião via LLM (OpenAI ou endpoint compatível via `AI_BASE_URL`); desativada sem `OPENAI_API_KEY`.
- `app/static/` — SPA (index.html, app.js, style.css) servida em `/` e `/static` (caminho absoluto relativo ao módulo).

## Conventions

- Idioma: código, comentários, mensagens de erro e UI em **pt-BR**; sinais em maiúsculo (`CALL`/`PUT`/`AGUARDAR`).
- Módulos carregam `.env` com python-dotenv na importação, em `try/except` defensivo.
- Serviços expõem funções que retornam `(ok: bool, msg: str)`; `main.py` traduz em `HTTPException` (400/404/500/503).
- Estado global em singletons de módulo (`iq_service._api`, `analysis._SIGNAL_CACHE`, `news._calendar_cache`); thread-safe com locks. Cleanup explícito ao resetar caches.
- Testes: `unittest` + `unittest.mock.patch` (sem pytest), em `tests/` com `sys.path` apontando para `app/`. Nenhuma chamada real à IQ Option nos testes — mock de `analysis.iq_service`, `news_service`, `time`.
- Ao mudar a estrutura de pastas, atualizar também `run.py`/`Dockerfile` (app-dir/`PYTHONPATH`), o mount de `static/` em `app/main.py` e os imports dos testes.
- Não commitar `.env`, `market.db`, `.venv`, `__pycache__`, `.idea/` (ver `.gitignore`).
- Python 3.10+ syntax (`X | None`, `list[str]`, `from __future__ import annotations`).

## Notes

- (vazio — adicione aqui descobertas futuras por sessão)