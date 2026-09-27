"""
Motor de análise para opções binárias.
Fonte: IQ Option via iq_service.
"""
from __future__ import annotations

import json
import time
import numpy as np
import pandas as pd

import iq_service
import news_service
import trade_manager


# Timeframes em segundos (o que a IQ Option usa internamente)
TIMEFRAMES = {
    "1min":  {"label": "1 minuto",   "interval": 60},
    "5min":  {"label": "5 minutos",  "interval": 300},
    "15min": {"label": "15 minutos", "interval": 900},
    "30min": {"label": "30 minutos", "interval": 1800},
}

STRATEGIES = {
    "trend_pullback": {
        "name": "Retração na tendência",
        "description": "Busca uma correção até a média em uma tendência confirmada.",
        "max_score": 4,
        "indicadores": ["EMA20/EMA50", "ADX", "RSI", "candle de rejeição"],
    },
    "breakout": {
        "name": "Rompimento de faixa",
        "description": "Exige fechamento além da máxima ou mínima recente com expansão de volatilidade.",
        "max_score": 3,
        "indicadores": ["máxima/mínima de 20 candles", "ATR", "candle de expansão"],
    },
    "mean_reversion": {
        "name": "Reversão à média",
        "description": "Procura exaustão nas bandas e confirmação de retorno pelo RSI.",
        "max_score": 3,
        "indicadores": ["Bollinger", "RSI", "reversão do RSI"],
    },
    "support_resistance": {
        "name": "Suporte e resistência",
        "description": "Procura rejeição clara em zonas extremas recentes.",
        "max_score": 3,
        "indicadores": ["zona de 20 candles", "pavio", "candle de rejeição"],
    },
    "momentum": {
        "name": "Momentum",
        "description": "Exige alinhamento de médias, MACD e força direcional.",
        "max_score": 4,
        "indicadores": ["EMA9/EMA21", "MACD", "ADX", "RSI"],
    },
    "stoch_adx": {
        "name": "Tendência com estocástico",
        "description": "Tendência confirmada por EMA e ADX, com estocástico na mesma direção.",
        "max_score": 3,
        "indicadores": ["EMA9/EMA21", "ADX", "Stochastic", "vela direcional"],
    },
    "banda_stoch": {
        "name": "Banda com estocástico",
        "description": "Reversão na banda de Bollinger confirmada por virada do estocástico.",
        "max_score": 3,
        "indicadores": ["Bollinger", "Stochastic", "reversão do Stoch"],
    },
    "rsi_divergencia": {
        "name": "Divergência de RSI",
        "description": "Preço faz extremo mas o RSI não acompanha; reversão na divergência.",
        "max_score": 3,
        "indicadores": ["RSI", "preço", "divergência"],
    },
}

_SIGNAL_CACHE: dict[tuple[str, str, str], dict] = {}
MIN_ACCURACY_SAMPLE = 20     # mínimo de sinais históricos comparáveis
MIN_ACCURACY_RATE = 70.0     # só libera CALL/PUT acima de 70%
ACCURACY_WINDOW = 40         # janela rolante: últimos N sinais avaliados

# Parâmetros configuráveis por estratégia (limiares usados em _strategy_signal).
# Ex.: adx_min, tolerâncias de RSI, zonas de ATR, min_score. Sem overrides,
# valem estes valores padrão (iguais aos originais do código).
STRATEGY_PARAMS_DEFAULT = {
    "trend_pullback": {
        "adx_min": 20, "near_ema_atr": 0.8,
        "rsi_call_min": 45, "rsi_call_max": 65,
        "rsi_put_min": 35, "rsi_put_max": 55,
        "min_score": 3,
    },
    "breakout": {
        "expansao_atr": 1.1, "min_score": 2,
    },
    "mean_reversion": {
        "rsi_sobrevenda": 35, "rsi_sobrecompra": 65, "min_score": 2,
    },
    "support_resistance": {
        "zona_atr": 0.15, "pavio_ratio": 1.2, "min_score": 2,
    },
    "momentum": {
        "adx_min": 22, "rsi_call": 52, "rsi_put": 48, "min_score": 3,
    },
    "stoch_adx": {
        "adx_min": 20, "stoch_limite": 30, "min_score": 2,
    },
    "banda_stoch": {
        "stoch_min": 20, "stoch_max": 80, "min_score": 2,
    },
    "rsi_divergencia": {
        "rsi_limite": 45, "min_score": 2,
    },
}

# Cache de curta duração dos parâmetros efetivos por estratégia
_PARAMS_CACHE: dict[str, tuple[float, dict]] = {}


def candles_to_df(candles: list[dict]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles)
    required = {"time", "open", "high", "low", "close", "volume"}
    if not required.issubset(df.columns):
        return pd.DataFrame()
    for column in required:
        df.loc[:, column] = pd.to_numeric(df[column], errors="coerce")
    finite = np.isfinite(df[list(required)].astype(float)).all(axis=1)
    valid_ohlc = (
        (df["time"] > 0)
        & (df[["open", "high", "low", "close"]] > 0).all(axis=1)
        & (df["high"] >= df[["open", "close"]].max(axis=1))
        & (df["low"] <= df[["open", "close"]].min(axis=1))
        & (df["high"] >= df["low"])
    )
    df = df.loc[finite & valid_ohlc].copy()
    if df.empty:
        return pd.DataFrame()
    df.loc[:, "datetime"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("datetime").sort_index()
    df = df.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    })
    # Remove duplicatas (stream pode repetir)
    df = df[~df.index.duplicated(keep="last")]
    return df


def _drop_incomplete_candle(candles: list[dict], interval: int) -> list[dict]:
    """Remove o candle correspondente ao intervalo que ainda está aberto."""
    if not candles or interval <= 0:
        return candles

    current_bucket = int(time.time()) // interval * interval
    return [candle for candle in candles if int(candle.get("time", 0)) < current_bucket]


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close, high, low = df["Close"], df["High"], df["Low"]

    for p in (5, 10, 20):
        df.loc[:, f"EMA{p}"] = close.ewm(span=p, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.mask((gain > 0) & (loss == 0), 100.0)
    rsi = rsi.mask((gain == 0) & (loss > 0), 0.0)
    rsi = rsi.mask((gain == 0) & (loss == 0), 50.0)
    df.loc[:, "RSI"] = rsi

    low14 = low.rolling(14).min()
    high14 = high.rolling(14).max()
    df.loc[:, "Stoch_K"] = 100 * (close - low14) / (high14 - low14).replace(0, np.nan)
    df.loc[:, "Stoch_D"] = df["Stoch_K"].rolling(3).mean()

    rsi = df["RSI"]
    rmin, rmax = rsi.rolling(14).min(), rsi.rolling(14).max()
    df.loc[:, "StochRSI"] = (rsi - rmin) / (rmax - rmin).replace(0, np.nan) * 100

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df.loc[:, "MACD"] = ema12 - ema26
    df.loc[:, "MACD_signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df.loc[:, "MACD_hist"] = df["MACD"] - df["MACD_signal"]

    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    df.loc[:, "BB_upper"] = mid + 2 * std
    df.loc[:, "BB_lower"] = mid - 2 * std

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    df.loc[:, "ATR"] = tr.rolling(14).mean()

    up, down = high.diff(), -low.diff()
    plus_dm = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down
    atr14 = tr.rolling(14).mean()
    plus_di = 100 * (plus_dm.rolling(14).mean() / atr14.replace(0, np.nan))
    minus_di = 100 * (minus_dm.rolling(14).mean() / atr14.replace(0, np.nan))
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df.loc[:, "ADX"] = dx.rolling(14).mean()
    df.loc[:, "PLUS_DI"] = plus_di
    df.loc[:, "MINUS_DI"] = minus_di

    tp = (high + low + close) / 3
    sma_tp = tp.rolling(20).mean()
    mad = tp.rolling(20).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    df.loc[:, "CCI"] = (tp - sma_tp) / (0.015 * mad.replace(0, np.nan))

    df.loc[:, "WilliamsR"] = -100 * (high14 - close) / (high14 - low14).replace(0, np.nan)
    df.loc[:, "EMA50"] = close.ewm(span=50, adjust=False).mean()
    df.loc[:, "EMA9"] = close.ewm(span=9, adjust=False).mean()
    df.loc[:, "EMA21"] = close.ewm(span=21, adjust=False).mean()
    df.loc[:, "range_high_20"] = high.shift(1).rolling(20).max()
    df.loc[:, "range_low_20"] = low.shift(1).rolling(20).min()
    df.loc[:, "body"] = (close - df["Open"]).abs()
    df.loc[:, "upper_wick"] = high - pd.concat([df["Open"], close], axis=1).max(axis=1)
    df.loc[:, "lower_wick"] = pd.concat([df["Open"], close], axis=1).min(axis=1) - low
    return df


def _signal(direction: str, score: int, reason: str, indicators: list[str], min_score: int = 3) -> dict:
    score = int(score)
    if score < min_score:
        direction = "AGUARDAR"
    return {
        "signal": direction,
        "score": score,
        "reason": reason,
        "indicators": indicators,
    }


def _strategy_signal(df: pd.DataFrame, strategy: str) -> dict:
    """Aplica uma estrategia isolada; indicadores de outras estrategias nao votam.

    Limiares são lidos dos parâmetros configuráveis (STRATEGY_PARAMS_DEFAULT +
    overrides persistidos via /api/strategy-params).
    """
    if len(df) < 60:
        return _signal("AGUARDAR", 0, "Dados insuficientes para esta estrategia.", [])
    row, prev = df.iloc[-1], df.iloc[-2]
    close = float(row["Close"])
    atr = float(row["ATR"])
    if not np.isfinite(atr) or atr <= 0:
        return _signal("AGUARDAR", 0, "Volatilidade invalida ou insuficiente.", [])
    p = get_params_estrategia(strategy)

    if strategy == "trend_pullback":
        up = row["EMA20"] > row["EMA50"] and row["ADX"] >= p["adx_min"]
        down = row["EMA20"] < row["EMA50"] and row["ADX"] >= p["adx_min"]
        near_ema = abs(close - row["EMA20"]) <= atr * p["near_ema_atr"]
        call_score = sum((up, near_ema, p["rsi_call_min"] <= row["RSI"] <= p["rsi_call_max"], close > row["Open"]))
        put_score = sum((down, near_ema, p["rsi_put_min"] <= row["RSI"] <= p["rsi_put_max"], close < row["Open"]))
        min_score = int(p["min_score"])
        call = call_score >= min_score and call_score > put_score
        put = put_score >= min_score and put_score > call_score
        direction = "CALL" if call else "PUT" if put else "AGUARDAR"
        score = max(call_score, put_score)
        return _signal(direction, score, "Retração confirmada na EMA20 dentro de tendência." if direction != "AGUARDAR" else "Retração sem confirmação suficiente.", ["EMA20/EMA50", "ADX", "RSI", "candle de rejeição"])

    if strategy == "breakout":
        expansion = row["High"] - row["Low"] >= atr * p["expansao_atr"]
        call_score = sum((close > row["range_high_20"], expansion, close > row["Open"]))
        put_score = sum((close < row["range_low_20"], expansion, close < row["Open"]))
        min_score = int(p["min_score"])
        call = call_score >= min_score and call_score > put_score
        put = put_score >= min_score and put_score > call_score
        direction = "CALL" if call else "PUT" if put else "AGUARDAR"
        return _signal(direction, max(call_score, put_score), "Rompimento confirmado por evidências de faixa e expansão." if direction != "AGUARDAR" else "Rompimento ainda sem confirmação suficiente.", ["máxima/mínima de 20 candles", "ATR", "candle de expansão"], min_score=min_score)

    if strategy == "mean_reversion":
        call_score = sum((close <= row["BB_lower"], row["RSI"] < p["rsi_sobrevenda"], row["RSI"] > prev["RSI"]))
        put_score = sum((close >= row["BB_upper"], row["RSI"] > p["rsi_sobrecompra"], row["RSI"] < prev["RSI"]))
        min_score = int(p["min_score"])
        call = call_score >= min_score and call_score > put_score
        put = put_score >= min_score and put_score > call_score
        direction = "CALL" if call else "PUT" if put else "AGUARDAR"
        return _signal(direction, max(call_score, put_score), "Reversão confirmada por evidências de banda e RSI." if direction != "AGUARDAR" else "Reversão sem confirmação suficiente.", ["Bollinger", "RSI", "reversão do RSI"], min_score=min_score)

    if strategy == "support_resistance":
        support = df["Low"].iloc[-21:-1].min()
        resistance = df["High"].iloc[-21:-1].max()
        min_score = int(p["min_score"])
        call_score = sum((row["Low"] <= support + atr * p["zona_atr"], row["lower_wick"] > row["body"] * p["pavio_ratio"], close > row["Open"]))
        put_score = sum((row["High"] >= resistance - atr * p["zona_atr"], row["upper_wick"] > row["body"] * p["pavio_ratio"], close < row["Open"]))
        call = call_score >= min_score and call_score > put_score
        put = put_score >= min_score and put_score > call_score
        direction = "CALL" if call else "PUT" if put else "AGUARDAR"
        return _signal(direction, max(call_score, put_score), "Rejeição confirmada por evidências de zona e candle." if direction != "AGUARDAR" else "Rejeição sem confirmação suficiente.", ["zona de 20 candles", "pavio", "candle de rejeição"], min_score=min_score)

    if strategy == "momentum":
        call_score = sum((row["EMA9"] > row["EMA21"], row["MACD"] > row["MACD_signal"], row["ADX"] >= p["adx_min"], row["RSI"] > p["rsi_call"]))
        put_score = sum((row["EMA9"] < row["EMA21"], row["MACD"] < row["MACD_signal"], row["ADX"] >= p["adx_min"], row["RSI"] < p["rsi_put"]))
        min_score = int(p["min_score"])
        call = call_score >= min_score and call_score > put_score
        put = put_score >= min_score and put_score > call_score
        direction = "CALL" if call else "PUT" if put else "AGUARDAR"
        score = max(call_score, put_score)
        return _signal(direction, score, "Momentum alinhado entre médias, MACD e ADX." if direction != "AGUARDAR" else "Momentum sem alinhamento suficiente.", ["EMA9/EMA21", "MACD", "ADX", "RSI"])

    if strategy == "stoch_adx":
        trend_up = row["EMA9"] > row["EMA21"] and row["ADX"] >= p["adx_min"]
        trend_down = row["EMA9"] < row["EMA21"] and row["ADX"] >= p["adx_min"]
        stoch_call = row["Stoch_K"] > row["Stoch_D"] and row["Stoch_K"] < p["stoch_limite"]
        stoch_put = row["Stoch_K"] < row["Stoch_D"] and row["Stoch_K"] > 100 - p["stoch_limite"]
        call_score = sum((trend_up, stoch_call, close > row["Open"]))
        put_score = sum((trend_down, stoch_put, close < row["Open"]))
        min_score = int(p["min_score"])
        call = call_score >= min_score and call_score > put_score
        put = put_score >= min_score and put_score > call_score
        direction = "CALL" if call else "PUT" if put else "AGUARDAR"
        return _signal(direction, max(call_score, put_score), "Tendência e estocástico alinhados na mesma direção." if direction != "AGUARDAR" else "Tendência/estocástico sem alinhamento.", ["EMA9/EMA21", "ADX", "Stochastic", "vela direcional"], min_score=min_score)

    if strategy == "banda_stoch":
        call_score = sum((close <= row["BB_lower"], row["Stoch_K"] < p["stoch_min"], row["Stoch_K"] > prev["Stoch_K"]))
        put_score = sum((close >= row["BB_upper"], row["Stoch_K"] > p["stoch_max"], row["Stoch_K"] < prev["Stoch_K"]))
        min_score = int(p["min_score"])
        call = call_score >= min_score and call_score > put_score
        put = put_score >= min_score and put_score > call_score
        direction = "CALL" if call else "PUT" if put else "AGUARDAR"
        return _signal(direction, max(call_score, put_score), "Banda e estocástico confirmando a reversão." if direction != "AGUARDAR" else "Reversão de banda sem confirmação do estocástico.", ["Bollinger", "Stochastic", "reversão do Stoch"], min_score=min_score)

    if strategy == "rsi_divergencia":
        p2 = df.iloc[-3]
        call_score = sum((close < p2["Close"] and row["RSI"] > p2["RSI"], row["RSI"] < p["rsi_limite"], close > row["Open"]))
        put_score = sum((close > p2["Close"] and row["RSI"] < p2["RSI"], row["RSI"] > 100 - p["rsi_limite"], close < row["Open"]))
        min_score = int(p["min_score"])
        call = call_score >= min_score and call_score > put_score
        put = put_score >= min_score and put_score > call_score
        direction = "CALL" if call else "PUT" if put else "AGUARDAR"
        return _signal(direction, max(call_score, put_score), "Divergência de RSI favorecendo a reversão." if direction != "AGUARDAR" else "Divergência de RSI sem confirmação.", ["RSI", "preço", "divergência"], min_score=min_score)

    raise ValueError(f"Estratégia desconhecida: {strategy}")


def _proximity(score: int, max_score: int = 4) -> dict:
    ratio = max(0.0, min(1.0, score / max_score))
    if ratio >= 0.75:
        label = "SINAL MUITO PRÓXIMO"
    elif ratio >= 0.5:
        label = "ATENÇÃO"
    else:
        label = "AGUARDAR"
    return {"label": label, "percent": round(ratio * 100, 1)}


def estimate_historical_accuracy(df: pd.DataFrame, strategy: str, horizon: int = 1) -> dict:
    """Calcula a taxa de acerto somente com candles já fechados.

    A taxa é usada como filtro do sinal: menos de 20 casos ou taxa de acerto
    igual/abaixo de 70% não libera CALL/PUT.
    """
    if len(df) < 40:
        return {
            "rate": None, "sample_size": 0, "wins": 0, "ultimos": [],
            "qualified": False, "label": "Amostra insuficiente"
        }

    resultados: list[bool] = []
    start = max(40, len(df) - ACCURACY_WINDOW - horizon)
    for end in range(start, len(df) - horizon + 1):
        decision = _strategy_signal(df.iloc[:end], strategy)
        if decision["signal"] not in ("CALL", "PUT"):
            continue
        entry = float(df["Close"].iloc[end - 1])
        exit_price = float(df["Close"].iloc[end + horizon - 1])
        acertou = (decision["signal"] == "CALL" and exit_price > entry) or (
            decision["signal"] == "PUT" and exit_price < entry
        )
        resultados.append(acertou)

    total = len(resultados)
    wins = sum(1 for r in resultados if r)
    rate = round(wins / total * 100, 1) if total else None
    qualified = bool(total >= MIN_ACCURACY_SAMPLE and rate is not None and rate > MIN_ACCURACY_RATE)
    ultimos = [("OK" if r else "ERRO") for r in reversed(resultados[-12:])]

    if total < MIN_ACCURACY_SAMPLE:
        label = "Amostra insuficiente"
    elif rate is not None and rate <= MIN_ACCURACY_RATE:
        label = "Taxa de acerto abaixo do mínimo"
    else:
        label = "Taxa de acerto qualificada"

    return {
        "rate": rate,
        "sample_size": total,
        "wins": wins,
        "ultimos": ultimos,
        "qualified": qualified,
        "label": label,
    }


def set_params_estrategia(strategy: str, params: dict) -> tuple[bool, str]:
    """Salva parâmetros customizados para uma estratégia (limiares, min_score)."""
    if strategy not in STRATEGY_PARAMS_DEFAULT:
        return False, f"Estratégia desconhecida: {strategy}"
    for chave, valor in params.items():
        if chave not in STRATEGY_PARAMS_DEFAULT[strategy]:
            continue
        try:
            numero = float(valor)
        except (TypeError, ValueError):
            return False, f"Parâmetro '{chave}' inválido para {strategy}."
        trade_manager.set_config_raw(f"sp_{strategy}_{chave}", f"{numero:.4g}")
    _PARAMS_CACHE.pop(strategy, None)
    return True, "Parâmetros da estratégia salvos."


def get_params_estrategia(strategy: str) -> dict:
    """Parâmetros efetivos da estratégia (overrides persistidos sobre defaults)."""
    agora = time.time()
    cached = _PARAMS_CACHE.get(strategy)
    if cached and agora - cached[0] < 2.0:
        return cached[1]
    params = dict(STRATEGY_PARAMS_DEFAULT.get(strategy, {}))
    for chave in params:
        bruto = trade_manager.get_config_valor(f"sp_{strategy}_{chave}", None)
        if bruto is not None and str(bruto).strip() != "":
            try:
                params[chave] = float(bruto)
            except (TypeError, ValueError):
                pass
    _PARAMS_CACHE[strategy] = (agora, params)
    return params


def set_usar_votos(ativado: bool) -> None:
    """Habilita/desabilita o filtro dos votos dos indicadores no sinal."""
    trade_manager.set_config_raw("usar_votos", "1" if ativado else "0")


def _usar_votos() -> bool:
    return str(trade_manager.get_config_valor("usar_votos", "0")) == "1"


# --------- Votos ---------
def _vote_rsi(df, i):
    rsi = df["RSI"].iloc[i]
    prev = df["RSI"].iloc[i-1] if i >= 1 else rsi
    if pd.isna(rsi) or pd.isna(prev): return 0, "RSI sem dados"
    if rsi < 30 and rsi > prev: return 1, f"RSI {rsi:.1f} saindo de sobrevenda — CALL"
    if rsi > 70 and rsi < prev: return -1, f"RSI {rsi:.1f} saindo de sobrecompra — PUT"
    if rsi < 30: return 1, f"RSI {rsi:.1f} sobrevenda — CALL"
    if rsi > 70: return -1, f"RSI {rsi:.1f} sobrecompra — PUT"
    if rsi > 55 and rsi > prev: return 1, f"RSI {rsi:.1f} viés de alta — CALL"
    if rsi < 45 and rsi < prev: return -1, f"RSI {rsi:.1f} viés de baixa — PUT"
    return 0, f"RSI {rsi:.1f} neutro"


def _vote_stoch(df, i):
    k, d = df["Stoch_K"].iloc[i], df["Stoch_D"].iloc[i]
    pk = df["Stoch_K"].iloc[i-1] if i >= 1 else k
    pdd = df["Stoch_D"].iloc[i-1] if i >= 1 else d
    if pd.isna(k) or pd.isna(d) or pd.isna(pk) or pd.isna(pdd): return 0, "Stoch sem dados"
    if pk <= pdd and k > d and k < 30: return 1, f"Stoch cruzou p/ cima em sobrevenda ({k:.1f}) — CALL"
    if pk >= pdd and k < d and k > 70: return -1, f"Stoch cruzou p/ baixo em sobrecompra ({k:.1f}) — PUT"
    if k < 20: return 1, f"Stoch {k:.1f} sobrevenda — CALL"
    if k > 80: return -1, f"Stoch {k:.1f} sobrecompra — PUT"
    return 0, f"Stoch {k:.1f} neutro"


def _vote_stoch_rsi(df, i):
    sr = df["StochRSI"].iloc[i]
    if pd.isna(sr): return 0, "Stoch RSI sem dados"
    if sr < 20: return 1, f"Stoch RSI {sr:.1f} sobrevenda — CALL"
    if sr > 80: return -1, f"Stoch RSI {sr:.1f} sobrecompra — PUT"
    return 0, f"Stoch RSI {sr:.1f} neutro"


def _vote_macd(df, i):
    m, s = df["MACD"].iloc[i], df["MACD_signal"].iloc[i]
    h = df["MACD_hist"].iloc[i]
    ph = df["MACD_hist"].iloc[i - 1] if i >= 1 else h
    if pd.isna(m) or pd.isna(s) or pd.isna(h) or pd.isna(ph):
        return 0, "MACD sem dados"
    if m > s and h > ph:
        return 1, "MACD acima do sinal com histograma subindo — CALL"
    if m < s and h < ph:
        return -1, "MACD abaixo do sinal com histograma caindo — PUT"
    if m > s:
        return 1, "MACD acima da linha de sinal — CALL"
    if m < s:
        return -1, "MACD abaixo da linha de sinal — PUT"
    return 0, "MACD neutro"


def _vote_ema(df, i, fast_column, slow_column):
    fast = df[fast_column].iloc[i]
    slow = df[slow_column].iloc[i]
    if pd.isna(fast) or pd.isna(slow):
        return 0, "EMAs sem dados"
    if fast > slow:
        return 1, f"{fast_column} acima da {slow_column} — CALL"
    if fast < slow:
        return -1, f"{fast_column} abaixo da {slow_column} — PUT"
    return 0, "EMAs sem direção"


def _vote_bollinger(df, i):
    close = df["Close"].iloc[i]
    upper = df["BB_upper"].iloc[i]
    lower = df["BB_lower"].iloc[i]
    if pd.isna(upper) or pd.isna(lower):
        return 0, "Bollinger sem dados"
    if close <= lower:
        return 1, "Preço na banda inferior — CALL"
    if close >= upper:
        return -1, "Preço na banda superior — PUT"
    return 0, "Preço dentro das bandas"


def _vote_adx(df, i):
    adx = df["ADX"].iloc[i]
    plus_di = df["PLUS_DI"].iloc[i]
    minus_di = df["MINUS_DI"].iloc[i]
    if pd.isna(adx) or pd.isna(plus_di) or pd.isna(minus_di) or adx < 20:
        return 0, "ADX sem tendência forte"
    if plus_di > minus_di:
        return 1, f"ADX {adx:.1f} com +DI dominante — CALL"
    if minus_di > plus_di:
        return -1, f"ADX {adx:.1f} com -DI dominante — PUT"
    return 0, "ADX sem direção"


def _vote_cci(df, i):
    cci = df["CCI"].iloc[i]
    if pd.isna(cci):
        return 0, "CCI sem dados"
    if cci < -100:
        return 1, f"CCI {cci:.1f} em sobrevenda — CALL"
    if cci > 100:
        return -1, f"CCI {cci:.1f} em sobrecompra — PUT"
    return 0, f"CCI {cci:.1f} neutro"


def _vote_williams(df, i):
    williams = df["WilliamsR"].iloc[i]
    if pd.isna(williams):
        return 0, "Williams %R sem dados"
    if williams < -80:
        return 1, f"Williams %R {williams:.1f} em sobrevenda — CALL"
    if williams > -20:
        return -1, f"Williams %R {williams:.1f} em sobrecompra — PUT"
    return 0, f"Williams %R {williams:.1f} neutro"


def _preparar_mfi(df):
    """Money Flow Index (14): fluxo monetário com volume e preço típico."""
    if "MFI" not in df.columns:
        típico = (df["High"] + df["Low"] + df["Close"]) / 3
        raw = típico * df["Volume"]
        diff = típico.diff()
        positivo = raw.where(diff > 0, 0.0).rolling(14).sum()
        negativo = raw.where(diff < 0, 0.0).rolling(14).sum()
        mfi = 100 - (100 / (1 + positivo / negativo.replace(0, np.nan)))
        df = df.assign(MFI=mfi)
    return df


def _vote_mfi(df, i):
    mfi = df["MFI"].iloc[i]
    prev = df["MFI"].iloc[i - 1] if i >= 1 else mfi
    if pd.isna(mfi) or pd.isna(prev):
        return 0, "MFI sem dados"
    if mfi < 20 and mfi > prev:
        return 1, f"MFI {mfi:.1f} saindo de sobrevenda — CALL"
    if mfi > 80 and mfi < prev:
        return -1, f"MFI {mfi:.1f} saindo de sobrecompra — PUT"
    if mfi < 20:
        return 1, f"MFI {mfi:.1f} sobrevenda — CALL"
    if mfi > 80:
        return -1, f"MFI {mfi:.1f} sobrecompra — PUT"
    return 0, f"MFI {mfi:.1f} neutro"


def _preparar_roc(df):
    """Rate of Change (10): variação percentual do preço de fechamento."""
    if "ROC" not in df.columns:
        df = df.assign(ROC=df["Close"].pct_change(periods=10) * 100)
    return df


def _vote_roc(df, i):
    roc = df["ROC"].iloc[i]
    if pd.isna(roc):
        return 0, "ROC sem dados"
    if roc > 0.5:
        return 1, f"ROC {roc:.2f}% momentum de alta — CALL"
    if roc < -0.5:
        return -1, f"ROC {roc:.2f}% momentum de baixa — PUT"
    return 0, f"ROC {roc:.2f}% neutro"


def _preparar_sar(df):
    """Parabolic SAR (0.02 / 0.2): ponto de reversão que segue o preço."""
    if "SAR" not in df.columns:
        closes = df["Close"].to_numpy()
        highs = df["High"].to_numpy()
        lows = df["Low"].to_numpy()
        n = len(df)
        sar = np.full(n, np.nan)
        af = 0.02
        af_max = 0.2
        is_long = True
        ext = lows[0]
        valor = closes[0]
        for k in range(1, n):
            valor = valor + af * (ext - valor)
            if is_long:
                valor = min(valor, lows[k - 1], lows[k])
                if closes[k] < valor:
                    is_long = False
                    valor = ext
                    ext = highs[k]
                    af = 0.02
                elif highs[k] > ext:
                    ext = highs[k]
                    af = min(af + 0.02, af_max)
            else:
                valor = max(valor, highs[k - 1], highs[k])
                if closes[k] > valor:
                    is_long = True
                    valor = ext
                    ext = lows[k]
                    af = 0.02
                elif lows[k] < ext:
                    ext = lows[k]
                    af = min(af + 0.02, af_max)
            sar[k] = valor
        df = df.assign(SAR=sar)
    return df


def _vote_sar(df, i):
    close = df["Close"].iloc[i]
    sar = df["SAR"].iloc[i]
    if pd.isna(sar):
        return 0, "SAR sem dados"
    if close > sar:
        return 1, f"Preço acima do SAR ({sar:.5f}) — CALL"
    if close < sar:
        return -1, f"Preço abaixo do SAR ({sar:.5f}) — PUT"
    return 0, "SAR na direção indefinida"


def _preparar_obv(df):
    """On-Balance Volume: volume acumulado conforme a direção do close."""
    if "OBV" not in df.columns:
        direcao = np.sign(df["Close"].diff().fillna(0))
        df = df.assign(OBV=(direcao * df["Volume"]).cumsum())
    return df


def _vote_obv(df, i):
    obv = df["OBV"].iloc[i]
    if pd.isna(obv) or i < 10:
        return 0, "OBV sem histórico"
    media = df["OBV"].iloc[max(0, i - 10):i + 1].mean()
    if obv > media:
        return 1, "OBV acima da média — pressão compradora — CALL"
    if obv < media:
        return -1, "OBV abaixo da média — pressão vendedora — PUT"
    return 0, "OBV neutro"


def _vote_engolfo(df, i):
    """Padrão de vela: corpo atual engole o corpo anterior (rejeição)."""
    if i < 1:
        return 0, "Engolfo sem vela anterior"
    o, c = df["Open"].iloc[i], df["Close"].iloc[i]
    po, pc = df["Open"].iloc[i - 1], df["Close"].iloc[i - 1]
    corpo = abs(c - o)
    corpo_prev = abs(pc - po)
    if pd.isna(o) or pd.isna(c) or corpo < 1e-12 or corpo_prev < 1e-12:
        return 0, "Engolfo sem corpo"
    if c > o and c >= po and o <= pc and corpo > corpo_prev * 1.2:
        return 1, "Candle de alta engolfa o anterior — CALL"
    if c < o and c <= po and o >= pc and corpo > corpo_prev * 1.2:
        return -1, "Candle de baixa engolfa o anterior — PUT"
    return 0, "Sem engolfo"


def _preparar_atr(df):
    """ATR (14): média do True Range; expansão direcional do candle."""
    if "ATR" not in df.columns:
        tr = pd.concat([
            df["High"] - df["Low"],
            (df["High"] - df["Close"].shift()).abs(),
            (df["Low"] - df["Close"].shift()).abs(),
        ], axis=1).max(axis=1)
        df = df.assign(ATR=tr.rolling(14).mean())
    return df


def _vote_atr(df, i):
    a = df["ATR"].iloc[i]
    o, c = df["Open"].iloc[i], df["Close"].iloc[i]
    if pd.isna(a) or a <= 0:
        return 0, "ATR sem dados"
    corpo = abs(c - o)
    if corpo > 2.0 * a:
        if c > o:
            return 1, f"Expansão ({corpo / a:.1f}x ATR) de alta — CALL"
        return -1, f"Expansão ({corpo / a:.1f}x ATR) de baixa — PUT"
    return 0, "Volatilidade normal"


def _preparar_momentum(df):
    """Momentum (10): diferença do fechamento em relação a 10 velas atrás."""
    if "Momentum" not in df.columns:
        df = df.assign(Momentum=df["Close"] - df["Close"].shift(10))
    return df


def _vote_momentum(df, i):
    m = df["Momentum"].iloc[i]
    if pd.isna(m):
        return 0, "Momentum sem dados"
    if m > 0:
        return 1, f"Momentum +{m:.5f} — CALL"
    if m < 0:
        return -1, f"Momentum {m:.5f} — PUT"
    return 0, "Momentum neutro"


def _preparar_cmf(df):
    """Chaikin Money Flow (20): fluxo monetário com posição no range."""
    if "CMF" not in df.columns:
        faixa = (df["High"] - df["Low"]).replace(0, np.nan)
        mfm = ((df["Close"] - df["Low"]) - (df["High"] - df["Close"])) / faixa
        vlr = mfm * df["Volume"]
        vol20 = df["Volume"].rolling(20).sum().replace(0, np.nan)
        df = df.assign(CMF=vlr.rolling(20).sum() / vol20)
    return df


def _vote_cmf(df, i):
    c = df["CMF"].iloc[i]
    if pd.isna(c):
        return 0, "CMF sem dados"
    if c > 0.05:
        return 1, f"CMF {c:.3f} fluxo comprador — CALL"
    if c < -0.05:
        return -1, f"CMF {c:.3f} fluxo vendedor — PUT"
    return 0, f"CMF {c:.3f} neutro"


def _preparar_donchian(df):
    """Donchian (20): máximas e mínimas da janela para detecção de rompimento."""
    if "DC_high" not in df.columns:
        df = df.assign(
            DC_high=df["High"].rolling(20).max(),
            DC_low=df["Low"].rolling(20).min(),
        )
    return df


def _vote_donchian(df, i):
    c = df["Close"].iloc[i]
    dh = df["DC_high"].iloc[i]
    dl = df["DC_low"].iloc[i]
    if pd.isna(dh) or pd.isna(dl):
        return 0, "Donchian sem dados"
    if c >= dh:
        return 1, f"Acima da máxima de 20 velas ({dh:.5f}) — CALL"
    if c <= dl:
        return -1, f"Abaixo da mínima de 20 velas ({dl:.5f}) — PUT"
    return 0, "Dentro da faixa de 20 velas"


def _vote_rejeicao(df, i):
    """Vela de rejeição: sombra longa no topo (PUT) ou na base (CALL)."""
    o, h, l, c = df["Open"].iloc[i], df["High"].iloc[i], df["Low"].iloc[i], df["Close"].iloc[i]
    if pd.isna(o) or pd.isna(h) or pd.isna(l) or pd.isna(c):
        return 0, "Sem dados"
    faixa = max(h - l, 1e-12)
    corpo = abs(c - o)
    pavio_sup = h - max(o, c)
    pavio_inf = min(o, c) - l
    if pavio_sup > corpo * 1.5 and pavio_sup > faixa * 0.5:
        return -1, "Pavio superior longo — rejeição de alta — PUT"
    if pavio_inf > corpo * 1.5 and pavio_inf > faixa * 0.5:
        return 1, "Pavio inferior longo — rejeição de baixa — CALL"
    return 0, "Sem rejeição"


# ===========================================================================
# Registro declarativo de indicadores
# ===========================================================================
# Para adicionar um indicador novo:
#   1) Escreva uma função `def _vote_seu(df, i) -> (voto, motivo)` que retorna
#      +1 (CALL/alta), -1 (PUT/baixa) ou 0 (neutro) e um texto explicativo.
#      Se precisar de colunas novas, use o campo `preparar` para calculá-las
#      (recebe o DataFrame e retorna o mesmo DataFrame ampliado).
#   2) Adicione um registro no INDICADORES_PADRAO abaixo com `id`, `nome`
#      (rótulo exibido), `descricao`, `peso` e `votar`.
#   3) Ative/desative pela aba Estratégias (ou via PUT /api/indicators).
INDICADORES_PADRAO = [
    {
        "id": "rsi",
        "nome": "RSI (14)",
        "descricao": "Oscilador de momentum; sobrevenda/sobrecompra e viés direcional.",
        "peso": 1.5,
        "votar": _vote_rsi,
    },
    {
        "id": "stoch",
        "nome": "Stochastic",
        "descricao": "Posição do preço na faixa recente, com cruzamentos.",
        "peso": 1.3,
        "votar": _vote_stoch,
    },
    {
        "id": "stochrsi",
        "nome": "Stoch RSI",
        "descricao": "RSI dentro da própria faixa; extremos de sobrevenda/sobrecompra.",
        "peso": 1.0,
        "votar": _vote_stoch_rsi,
    },
    {
        "id": "macd",
        "nome": "MACD",
        "descricao": "Convergência/divergência de médias com histograma.",
        "peso": 1.5,
        "votar": _vote_macd,
    },
    {
        "id": "ema510",
        "nome": "EMA 5/10",
        "descricao": "Médias curtas: tendência de curtíssimo prazo.",
        "peso": 1.0,
        "votar": lambda d, i: _vote_ema(d, i, "EMA5", "EMA10"),
    },
    {
        "id": "ema1020",
        "nome": "EMA 10/20",
        "descricao": "Médias médias: direção da tendência recente.",
        "peso": 1.0,
        "votar": lambda d, i: _vote_ema(d, i, "EMA10", "EMA20"),
    },
    {
        "id": "bollinger",
        "nome": "Bollinger",
        "descricao": "Bandas de volatilidade; extremos sugerem reversão.",
        "peso": 1.2,
        "votar": _vote_bollinger,
    },
    {
        "id": "adx",
        "nome": "ADX / DI",
        "descricao": "Força direcional da tendência (+DI/−DI).",
        "peso": 1.3,
        "votar": _vote_adx,
    },
    {
        "id": "cci",
        "nome": "CCI",
        "descricao": "Desvio do preço típico em relação à média; extremos.",
        "peso": 0.8,
        "votar": _vote_cci,
    },
    {
        "id": "williams",
        "nome": "Williams %R",
        "descricao": "Oscilador de momento; extremos de sobrevenda/sobrecompra.",
        "peso": 0.8,
        "votar": _vote_williams,
    },
    # ---- Novos indicadores (padrão DESMARCADO; ative na aba Estratégias) ----
    {
        "id": "mfi",
        "nome": "MFI (14)",
        "descricao": "Money Flow Index: fluxo monetário com volume; extremos de sobrevenda/sobrecompra.",
        "peso": 1.0,
        "padrao": True,
        "preparar": _preparar_mfi,
        "votar": _vote_mfi,
    },
    {
        "id": "roc",
        "nome": "ROC (10)",
        "descricao": "Rate of Change: momentum percentual do preço.",
        "peso": 0.8,
        "padrao": True,
        "preparar": _preparar_roc,
        "votar": _vote_roc,
    },
    {
        "id": "sar",
        "nome": "Parabolic SAR",
        "descricao": "Ponto de reversão que segue o preço; direção do SAR.",
        "peso": 1.0,
        "padrao": True,
        "preparar": _preparar_sar,
        "votar": _vote_sar,
    },
    {
        "id": "obv",
        "nome": "OBV",
        "descricao": "On-Balance Volume: pressão compradora/vendedora acumulada.",
        "peso": 0.8,
        "padrao": True,
        "preparar": _preparar_obv,
        "votar": _vote_obv,
    },
    {
        "id": "engolfo",
        "nome": "Candle de engolfo",
        "descricao": "Padrão de vela: o corpo atual engole o anterior (rejeição).",
        "peso": 0.9,
        "padrao": False,
        "votar": _vote_engolfo,
    },
    {
        "id": "atr",
        "nome": "ATR (14)",
        "descricao": "Expansão de volatilidade: corpo do candle acima de 2× ATR na direção.",
        "peso": 0.9,
        "padrao": False,
        "preparar": _preparar_atr,
        "votar": _vote_atr,
    },
    {
        "id": "momentum",
        "nome": "Momentum (10)",
        "descricao": "Diferença do fechamento em relação a 10 velas atrás.",
        "peso": 0.8,
        "padrao": False,
        "preparar": _preparar_momentum,
        "votar": _vote_momentum,
    },
    {
        "id": "cmf",
        "nome": "CMF (20)",
        "descricao": "Chaikin Money Flow: fluxo monetário acumulado com volume.",
        "peso": 0.9,
        "padrao": True,
        "preparar": _preparar_cmf,
        "votar": _vote_cmf,
    },
    {
        "id": "donchian",
        "nome": "Donchian (20)",
        "descricao": "Rompimento da máxima/mínima de 20 velas.",
        "peso": 1.0,
        "padrao": True,
        "preparar": _preparar_donchian,
        "votar": _vote_donchian,
    },
    {
        "id": "rejeicao",
        "nome": "Vela de rejeição",
        "descricao": "Sombra longa no topo (PUT) ou na base (CALL).",
        "peso": 0.8,
        "padrao": False,
        "votar": _vote_rejeicao,
    },
]

_INDICADORES_POR_ID = {reg["id"]: reg for reg in INDICADORES_PADRAO}


def listar_indicadores() -> list[dict]:
    """Catálogo de indicadores com o status ativo e peso EFETIVO (persistido no SQLite)."""
    ativos = set(_ids_indicadores_ativos())
    return [
        {
            "id": reg["id"],
            "nome": reg["nome"],
            "descricao": reg["descricao"],
            "peso": _peso_indicador(reg["id"]),
            "peso_padrao": reg["peso"],
            "ativo": reg["id"] in ativos,
        }
        for reg in INDICADORES_PADRAO
    ]


def _pesos_customizados() -> dict:
    """Pesos salvos pelo usuário no SQLite (config 'indicadores_pesos')."""
    bruto = trade_manager.get_config_valor("indicadores_pesos", "")
    if not bruto:
        return {}
    try:
        dados = json.loads(bruto)
    except Exception:
        return {}
    if not isinstance(dados, dict):
        return {}
    return {str(k).lower(): v for k, v in dados.items()}


def _peso_indicador(reg_id: str) -> float:
    """Peso efetivo: o customizado (se válido) ou o do registro."""
    custom = _pesos_customizados().get(str(reg_id).lower())
    if custom is not None:
        try:
            peso = float(custom)
            if 0 < peso <= 10:
                return round(peso, 2)
        except (TypeError, ValueError):
            pass
    return _INDICADORES_POR_ID[reg_id]["peso"]


def set_indicadores_pesos(pesos: dict) -> tuple[bool, str]:
    """Persiste pesos customizados (id → número entre 0 e 10)."""
    validos: dict[str, float] = {}
    for i, peso in (pesos or {}).items():
        i = str(i or "").strip().lower()
        if i not in _INDICADORES_POR_ID:
            continue
        try:
            p = float(peso)
        except (TypeError, ValueError):
            return False, f"Peso inválido para {i}: {peso}"
        if not 0 < p <= 10:
            return False, f"O peso de {i} deve estar entre 0 e 10."
        validos[i] = round(p, 2)
    trade_manager.set_config_raw("indicadores_pesos", json.dumps(validos, ensure_ascii=False))
    return True, "Pesos dos indicadores salvos."


def _ids_indicadores_ativos() -> list[str]:
    bruto = trade_manager.get_config_valor("indicadores_ativos", "")
    ids = [parte.strip() for parte in str(bruto or "").split(",") if parte.strip()]
    validos = [i for i in ids if i in _INDICADORES_POR_ID]
    # Se nada foi configurado ainda, apenas os indicadores de PADRÃO participam;
    # os indicadores novos (padrao=False) entram somente quando marcados na aba Estratégias.
    return validos or [
        "rsi", "macd", "ema510", "ema1020", "bollinger", "adx",
        "mfi", "roc", "sar", "obv", "cmf", "donchian"
    ]


def set_indicadores_ativos(ids: list[str]) -> tuple[bool, str]:
    """Persiste a lista de indicadores ativos; ids inválidos são ignorados."""
    validos = []
    for i in ids:
        i = (i or "").strip().lower()
        if i in _INDICADORES_POR_ID and i not in validos:
            validos.append(i)
    if not validos:
        return False, "Nenhum indicador válido foi informado."
    trade_manager.set_config_raw("indicadores_ativos", ",".join(validos))
    return True, "Indicadores ativos salvos."


def _votar(df: pd.DataFrame, i: int) -> list[dict]:
    """Aplica os indicadores ATIVOS no candle i e devolve os votos."""
    votos = []
    for reg in INDICADORES_PADRAO:
        if reg["id"] not in _ids_indicadores_ativos():
            continue
        try:
            fn_preparar = reg.get("preparar")
            if fn_preparar:
                df = fn_preparar(df)
            voto, motivo = reg["votar"](df, i)
        except Exception:
            voto, motivo = 0, f"{reg['nome']} indisponível"
        votos.append({
            "id": reg["id"],
            "name": reg["nome"],
            "vote": voto,
            "reason": motivo,
            "peso": _peso_indicador(reg["id"]),
        })
    return votos


# Quórum mínimo para o sinal ser considerado acionável
MIN_DIRECTIONAL_VOTES = 5    # pelo menos 5 dos indicadores precisam votar
MIN_MARGIN = 3               # diferença mínima entre bulls e bears
MIN_COVERAGE = 60.0          # 60% dos indicadores precisam ter opinião


def aggregate_votes(votes):
    total = len(votes)

    # Soma ponderada dos votos
    weighted_bull = sum(v.get("peso", 1.0) for v in votes if v["vote"] == 1)
    weighted_bear = sum(v.get("peso", 1.0) for v in votes if v["vote"] == -1)
    bulls = sum(1 for v in votes if v["vote"] == 1)
    bears = sum(1 for v in votes if v["vote"] == -1)
    neutrals = sum(1 for v in votes if v["vote"] == 0)

    directional = bulls + bears
    coverage = (directional / total * 100) if total else 0.0

    # Direção majoritária (por peso)
    if weighted_bull > weighted_bear:
        direction = "CALL"
        winner = bulls
        loser = bears
    elif weighted_bear > weighted_bull:
        direction = "PUT"
        winner = bears
        loser = bulls
    else:
        direction = "NEUTRAL"
        winner = max(bulls, bears)
        loser = min(bulls, bears)

    margin = winner - loser

    # ----- Verificações de quórum -----
    # Se não há votos direcionais suficientes, é NEUTRAL
    if directional < MIN_DIRECTIONAL_VOTES:
        direction = "NEUTRAL"

    # Se a margem entre vencedor e perdedor é pequena, é NEUTRAL
    if margin < MIN_MARGIN:
        direction = "NEUTRAL"

    # Se a cobertura é baixa, é NEUTRAL
    if coverage < MIN_COVERAGE:
        direction = "NEUTRAL"

    # Confiança calculada APENAS sobre os votos direcionais (CALL/PUT) —
    # os votos neutros não reduzem nem diluem o percentual exibido.
    if direction == "NEUTRAL":
        confidence = round((winner / directional * 100) if directional else 0.0, 1)
        strength = "Sem direção"
    else:
        concordancia = winner / directional if directional > 0 else 0
        confidence = round(concordancia * 100, 1)
        if confidence >= 75:
            strength = "Forte"
        elif confidence >= 55:
            strength = "Moderada"
        else:
            strength = "Fraca"

    return {
        "direction": direction,
        "confidence": confidence,
        "confluence_score": confidence,
        "strength": strength,
        "bulls": bulls,
        "bears": bears,
        "neutrals": neutrals,
        "total": total,
        "coverage": round(coverage, 1),
        "margin": margin,
    }

def analyze_timeframe(df: pd.DataFrame, tf_key: str) -> dict | None:
    if df is None or len(df) < 30:
        return None
    df = compute_indicators(df.copy())
    i = len(df) - 1

    votes = _votar(df, i)

    agg = aggregate_votes(votes)
    last = df.iloc[-1]
    return {
        "timeframe": tf_key,
        "price": float(last["Close"]),
        "signal": agg["direction"],
        "confidence": agg["confidence"],
        "strength": agg["strength"],
        "bulls": agg["bulls"],
        "bears": agg["bears"],
        "neutrals": agg["neutrals"],
        "total": agg["total"],
        "coverage": agg["coverage"],
        "margin": agg["margin"],
        "indicators": votes,
    }


def build_strategy(expiry: str, context: dict, trigger: dict | None, context_keys: tuple[str, ...]) -> dict:
    """Emite sinal apenas quando contexto e gatilho concordam.

    O score mede confluência dos dados atuais; não é uma probabilidade de acerto.
    """
    context_frames = [context.get(key) for key in context_keys]
    signals = [item.get("signal") for item in context_frames if item]
    base = {
        "expiry": expiry,
        "context_timeframes": list(context_keys),
        "recommendation": "AGUARDAR",
        "confidence": 0,
        "confluence_score": 0,
        "strength": "Sem dados",
    }

    if len(signals) != len(context_keys):
        return {
            **base,
            "reason": "Dados insuficientes para confirmar o contexto.",
            "warning": "Sem sinal: não há candles fechados suficientes.",
        }

    if signals[0] not in ("CALL", "PUT") or any(signal != signals[0] for signal in signals):
        return {
            **base,
            "confidence": round(sum(item.get("confidence", 0) for item in context_frames) / len(context_frames), 1),
            "confluence_score": round(sum(item.get("confidence", 0) for item in context_frames) / len(context_frames), 1),
            "strength": "Sem dados",
            "reason": "Os timeframes de contexto não estão alinhados.",
            "warning": "Conflito entre timeframes: aguarde uma estrutura mais limpa.",
        }

    if not trigger or trigger.get("signal") != signals[0]:
        return {
            **base,
            "confidence": round(sum(item.get("confidence", 0) for item in (*context_frames, trigger or {})) / (len(context_frames) + 1), 1),
            "confluence_score": round(sum(item.get("confidence", 0) for item in (*context_frames, trigger or {})) / (len(context_frames) + 1), 1),
            "strength": "Sem confirmação",
            "reason": f"O gatilho de {TIMEFRAMES[expiry]['label'].split()[0]} ainda não confirma o contexto.",
            "warning": "Não operar enquanto o gatilho não confirmar a direção.",
        }

    confidence = round(
        sum(item.get("confidence", 0) for item in (*context_frames, trigger)) / (len(context_frames) + 1),
        1,
    )
    if confidence >= 75:
        strength = "Forte"
    elif confidence >= 55:
        strength = "Moderada"
    else:
        strength = "Fraca"

    return {
        **base,
        "recommendation": signals[0],
        "confidence": confidence,
        "confluence_score": confidence,
        "strength": strength,
        "reason": f"Contexto de {', '.join(context_keys)} alinhado com o gatilho para {signals[0]}.",
        "warning": "Score de confluência não representa probabilidade de acerto.",
    }
    
def analyze_asset(asset: str, strategy: str = "trend_pullback") -> dict:
    """Analisa uma unica estrategia e retorna somente os tres prazos de entrada."""
    asset = asset.upper().replace("=X", "")
    if strategy not in STRATEGIES:
        raise ValueError(f"Estratégia desconhecida: {strategy}")

    now = int(time.time())
    news = news_service.get_news_risk(asset)
    signals = {}
    for expiry in ("1min", "5min", "15min"):
        cache_key = (asset, strategy, expiry)
        cached = _SIGNAL_CACHE.get(cache_key)
        market_safe = news["available"] and not news["blocked"]
        if market_safe and cached and cached["expires_at"] > now and "proximity" in cached and "historical_accuracy" in cached:
            signals[expiry] = {**cached, "locked": True, "seconds_remaining": cached["expires_at"] - now}
            continue

        interval = TIMEFRAMES[expiry]["interval"]
        candles = iq_service.get_candles_smart(asset, interval, 240)
        # A análise ao vivo pode usar a vela em formação. Isso permite encontrar
        # um sinal no meio da vela; o contador continua apontando para o próximo
        # fechamento do timeframe.
        df = compute_indicators(candles_to_df(candles))
        closed_candles = _drop_incomplete_candle(candles, interval)
        historical_df = compute_indicators(candles_to_df(closed_candles))
        decision = _strategy_signal(df, strategy) if not df.empty else _signal("AGUARDAR", 0, "Sem dados de mercado.", [])
        if not news["available"]:
            decision = _signal("AGUARDAR", 0, "Entrada suspensa: calendário econômico indisponível.", [])
        elif news["blocked"]:
            decision = _signal("AGUARDAR", 0, "Entrada bloqueada por notícia de alto impacto.", [event["title"] for event in news["events"]])

        # Votos dos indicadores ativos (fonte única: detalhamento, IA e,
        # opcionalmente, filtro do sinal quando "usar_votos" está habilitado)
        votos = _votar(df, len(df) - 1) if not df.empty else []
        if votos:
            agregado = aggregate_votes(votos)
            if (
                _usar_votos()
                and decision["signal"] in ("CALL", "PUT")
                and agregado["direction"] in ("CALL", "PUT")
                and agregado["direction"] != decision["signal"]
            ):
                decision = _signal(
                    "AGUARDAR",
                    decision["score"],
                    f"Conflito: a estratégia sugere {decision['signal']}, mas os indicadores votam "
                    f"{agregado['direction']} ({agregado['bulls']}x{agregado['bears']}). Aguardando alinhamento.",
                    [v["nome"] for v in votos],
                )
            decision["votos"] = [
                {"nome": v["name"], "voto": v["vote"], "motivo": v["reason"]}
                for v in votos
            ]
            decision["resumo_votos"] = {
                "bulls": agregado["bulls"],
                "bears": agregado["bears"],
                "neutros": agregado["neutrals"],
                "total": agregado["total"],
                "confianca": agregado["confidence"],
            }

        # Alinha o vencimento ao proximo fechamento de vela da IQ Option.
        # Os timeframes sao contados a partir do epoch Unix: 1m, 5m e 15m.
        expires_at = ((now // interval) + 1) * interval
        horizon = 1
        decision["proximity"] = _proximity(decision["score"], STRATEGIES[strategy]["max_score"])
        decision["historical_accuracy"] = estimate_historical_accuracy(historical_df, strategy, horizon)

        # Só existe CALL/PUT quando a amostra histórica tem pelo menos 20
        # sinais e a taxa de acerto é estritamente maior que 70%.
        accuracy = decision["historical_accuracy"]
        if decision["signal"] in ("CALL", "PUT") and not accuracy.get("qualified", False):
            decision = _signal(
                "AGUARDAR",
                decision["score"],
                "Sinal técnico encontrado, mas ainda não atende ao filtro histórico mínimo "
                "(20 sinais e taxa de acerto acima de 70%).",
                decision.get("indicators", []),
            )
            decision["proximity"] = _proximity(decision["score"], STRATEGIES[strategy]["max_score"])
            decision["historical_accuracy"] = accuracy

        # O sinal pode ser encontrado no meio da vela atual. O vencimento,
        # porém, permanece fixado no próximo fechamento do timeframe.
        decision["candle_in_progress"] = bool(candles and int(candles[-1].get("time", 0)) >= int(time.time()) // interval * interval)
        signals[expiry] = {
            **decision,
            "expiry": expiry,
            "expires_at": expires_at,
            "locked": False,
            "seconds_remaining": expires_at - now,
        }
        if market_safe:
            _SIGNAL_CACHE[cache_key] = signals[expiry]
        else:
            _SIGNAL_CACHE.pop(cache_key, None)

    warning = news["warning"] if not news["available"] else None
    return {
        "asset": asset,
        "strategy": strategy,
        "strategy_name": STRATEGIES[strategy]["name"],
        "strategy_description": STRATEGIES[strategy]["description"],
        "signals": signals,
        "news": news,
        "warning": warning,
    }


def walkforward_asset(asset: str, expiry: str, count: int = 240) -> dict:
    """Mede o sinal do timeframe contra o candle seguinte sem olhar o futuro."""
    if expiry not in TIMEFRAMES or expiry == "30min":
        raise ValueError("expiry deve ser 1min, 5min ou 15min")

    cfg = TIMEFRAMES[expiry]
    candles = _drop_incomplete_candle(
        iq_service.get_candles_smart(asset.upper().replace("=X", ""), cfg["interval"], count),
        cfg["interval"],
    )
    df = candles_to_df(candles)
    evaluated = wins = losses = 0
    for end in range(40, len(df)):
        signal = analyze_timeframe(df.iloc[:end], expiry)
        if not signal or signal["signal"] not in ("CALL", "PUT"):
            continue
        close_before = float(df["Close"].iloc[end - 1])
        close_after = float(df["Close"].iloc[end])
        won = (signal["signal"] == "CALL" and close_after > close_before) or (
            signal["signal"] == "PUT" and close_after < close_before
        )
        evaluated += 1
        wins += int(won)
        losses += int(not won)

    return {
        "asset": asset.upper().replace("=X", ""),
        "expiry": expiry,
        "sample_size": evaluated,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / evaluated * 100, 1) if evaluated else None,
        "note": "Proxy walk-forward de um timeframe; não substitui backtest da estratégia completa nem garante resultado futuro.",
    }


def get_chart_data(asset: str, interval: int = 300, count: int = 200) -> dict:
    """Retorna candles + EMAs + Bollinger para o gráfico."""
    candles = iq_service.get_candles_smart(asset, interval, count)
    df = candles_to_df(candles)
    if df.empty:
        return {"candles": [], "ema10": [], "ema20": [], "bb_upper": [], "bb_lower": []}
    df = compute_indicators(df)
    return {
        "candles": [
            {"time": int(idx.timestamp()), "open": float(r["Open"]),
             "high": float(r["High"]), "low": float(r["Low"]), "close": float(r["Close"])}
            for idx, r in df.iterrows()
        ],
        "ema10": [{"time": int(idx.timestamp()), "value": float(r["EMA10"])}
                  for idx, r in df.iterrows() if not pd.isna(r["EMA10"])],
        "ema20": [{"time": int(idx.timestamp()), "value": float(r["EMA20"])}
                  for idx, r in df.iterrows() if not pd.isna(r["EMA20"])],
        "bb_upper": [{"time": int(idx.timestamp()), "value": float(r["BB_upper"])}
                     for idx, r in df.iterrows() if not pd.isna(r["BB_upper"])],
        "bb_lower": [{"time": int(idx.timestamp()), "value": float(r["BB_lower"])}
                     for idx, r in df.iterrows() if not pd.isna(r["BB_lower"])],
    }