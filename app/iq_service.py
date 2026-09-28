"""
Serviço de conexão com a IQ Option via biblioteca comunitária iqoptionapi.
Mantém uma instância global conectada e expõe métodos para candles,
troca de conta (demo/oficial), compra de opções binárias e payout.
"""
from __future__ import annotations

import os
import time
import threading

from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")

# Conta usada nas operações: PRACTICE (demo) ou REAL (oficial).
# O trade_manager aplica a conta salva na configuração ao iniciar.
ACCOUNT_TYPE = os.getenv("IQ_ACCOUNT_TYPE", "PRACTICE").upper()
if ACCOUNT_TYPE not in ("PRACTICE", "REAL"):
    ACCOUNT_TYPE = "PRACTICE"

_api: IQ_Option | None = None
_pending_2fa_client: IQ_Option | None = None
_lock = threading.RLock()

# Cache de streams ativos: {(asset, interval): True}
_streams: dict[tuple[str, int], bool] = {}

# Cache do payout por ativo (evita chamadas pesadas de get_all_init)
_payout_cache: dict[str, dict] = {}
_payout_cache_ts = 0.0


def connect() -> tuple[bool, str]:
    global _api
    with _lock:
        if _api is not None:
            return True, "Já conectado"
        if not IQ_EMAIL or not IQ_PASSWORD:
            return False, "IQ_EMAIL/IQ_PASSWORD ausentes no .env"
        try:
            _api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
            try:
                _api.set_max_reconnect(5)
            except Exception:
                pass
            ok, reason = _api.connect()
            if not ok:
                _api = None
                return False, f"Falha: {reason}"
            ok_troca, msg = set_account_type(ACCOUNT_TYPE)
            if not ok_troca:
                _api = None
                return False, msg
            return True, f"Conectado à conta {ACCOUNT_TYPE}"
        except Exception as e:
            _api = None
            return False, f"Exceção: {e}"


def reconnect(email: str, password: str) -> tuple[bool, str]:
    """Conecta com as credenciais informadas e trata 2FA quando solicitado."""
    global _api, _pending_2fa_client, IQ_EMAIL, IQ_PASSWORD
    with _lock:
        try:
            if _api is not None:
                for asset, interval in list(_streams):
                    try:
                        _api.stop_candles_stream(asset, interval)
                    except Exception:
                        pass
            _api = None
            _pending_2fa_client = None
            _streams.clear()
            IQ_EMAIL, IQ_PASSWORD = email.strip(), password
            print(f"[iq] login manual: email_configurado={bool(IQ_EMAIL)} senha_configurada={bool(IQ_PASSWORD)}")
            client = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
            try:
                client.set_max_reconnect(5)
            except Exception:
                pass
            ok, reason = client.connect()
            if not ok:
                if reason == "2FA":
                    _pending_2fa_client = client
                    return False, "2FA_REQUIRED"
                _pending_2fa_client = None
                return False, f"Falha: {reason!r}"
            client.change_balance(ACCOUNT_TYPE)
            _api = client
            return True, f"Conta conectada na conta {ACCOUNT_TYPE}"
        except Exception as exc:
            _api = None
            _pending_2fa_client = None
            return False, f"Exceção: {type(exc).__name__}: {exc}"


def complete_2fa(code: str) -> tuple[bool, str]:
    """Conclui o login quando a IQ Option exige código SMS/2FA."""
    global _api, _pending_2fa_client
    code = (code or "").strip()
    if not code:
        return False, "Informe o código 2FA."
    with _lock:
        client = _pending_2fa_client
        if client is None:
            return False, "Não há uma autenticação 2FA pendente."
        try:
            ok, reason = client.connect_2fa(code)
            if not ok:
                return False, f"Falha no 2FA: {reason!r}"
            client.change_balance(ACCOUNT_TYPE)
            _api = client
            _pending_2fa_client = None
            return True, f"Conta conectada na conta {ACCOUNT_TYPE}"
        except Exception as exc:
            return False, f"Exceção no 2FA: {type(exc).__name__}: {exc}"


def credentials_configured() -> dict:
    """Indica presença das variáveis de ambiente, sem expor seus valores."""
    return {"email": bool(IQ_EMAIL), "password": bool(IQ_PASSWORD), "complete": bool(IQ_EMAIL and IQ_PASSWORD)}
def is_connected() -> bool:
    if _api is None:
        return False
    try:
        return bool(_api.check_connect())
    except Exception:
        return False


def get_api():
    """Exposição da instância do cliente (uso interno do trade_manager)."""
    return _api


def set_account_type(tipo: str) -> tuple[bool, str]:
    """Define a conta das operações: PRACTICE (demo) ou REAL (oficial).

    Se já estiver conectado, troca a conta ativa na IQ Option na hora.
    """
    global ACCOUNT_TYPE
    tipo = (tipo or "PRACTICE").upper()
    if tipo not in ("PRACTICE", "REAL"):
        return False, f"Tipo de conta inválido: {tipo} (use PRACTICE ou REAL)"
    ACCOUNT_TYPE = tipo
    with _lock:
        if _api is not None:
            try:
                _api.change_balance(tipo)
                time.sleep(0.3)
                if _api.get_balance_mode() != tipo:
                    return False, (
                        f"Não foi possível trocar a conta ativa para {tipo}. "
                        "A conta pode não existir no perfil da IQ Option."
                    )
            except Exception as e:
                return False, f"Falha ao trocar a conta ativa: {e}"
    return True, f"Conta configurada: {tipo}"


def get_balance_mode_safe():
    """Retorna o modo da conta ativa na IQ Option (None se desconectado)."""
    if _api is None:
        return None
    try:
        return _api.get_balance_mode()
    except Exception:
        return None


def get_balance():
    if _api is None:
        return None
    try:
        return _api.get_balance()
    except Exception:
        return None


def buy(ativo: str, valor: float, direcao: str, expiracao_min: int) -> tuple[bool, object]:
    """Executa uma opção binária (turbo/binary) na conta ativa.

    Retorna (True, ordem_id) em caso de sucesso.
    """
    if _api is None:
        return False, "não conectado"
    try:
        return _api.buy(round(float(valor), 2), ativo, direcao.lower(), int(expiracao_min))
    except Exception as e:
        return False, f"exceção ao comprar: {e}"


def get_payout(ativo: str, expiracao_min: int) -> float | None:
    """Multiplicador de payout esperado (ex.: 0.81 = 81%). Best-effort."""
    global _payout_cache, _payout_cache_ts
    if _api is None:
        return None
    try:
        if time.time() - _payout_cache_ts > 300:
            _payout_cache = _api.get_all_profit()
            _payout_cache_ts = time.time()
    except Exception:
        return None
    info = _payout_cache.get(ativo)
    if not info:
        return None
    opcao = "turbo" if int(expiracao_min) <= 5 else "binary"
    return info.get(opcao) or info.get("turbo") or info.get("binary")


# Lista padrão de pares (forex) disponíveis na IQ Option
DEFAULT_ASSETS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD",
    "AUDUSD", "NZDUSD", "EURGBP", "EURJPY", "GBPJPY",
    "EURCHF", "AUDJPY", "CADJPY", "CHFJPY", "EURAUD",
]

# OTC: mesmos pares no mercado OTC da IQ Option (horário próprio; ficam
# fechados quando o ambiente OTC não está no ar). Analisados como os normais.
OTC_SUFFIX = "-OTC"
DEFAULT_ASSETS_OTC = [f"{asset}{OTC_SUFFIX}" for asset in DEFAULT_ASSETS]


def list_assets() -> list[str]:
    """Retorna lista fixa de pares (normais + OTC) — evita dependência do init interno."""
    return [*DEFAULT_ASSETS, *DEFAULT_ASSETS_OTC]


# Cache do status de mercado. O iqoptionapi expõe get_all_open_time()
# em vez de get_asset_metadata(); mantemos cache para não repetir chamadas pesadas.
_open_time_cache: dict = {"ts": 0.0, "data": None}
OPEN_TIME_CACHE_SECONDS = 60

def get_market_status(asset: str) -> str | None:
    """Situação do par na IQ Option: 'aberto', 'fechado' ou None."""
    if _api is None:
        return None
    try:
        now = time.time()
        if now - _open_time_cache["ts"] > OPEN_TIME_CACHE_SECONDS or _open_time_cache["data"] is None:
            _open_time_cache["data"] = _api.get_all_open_time()
            _open_time_cache["ts"] = now

        data = _open_time_cache["data"] or {}
        asset_key = asset.upper()
        for market in ("turbo", "binary"):
            section = data.get(market, {})
            info = section.get(asset_key) if hasattr(section, "get") else None
            if isinstance(info, dict) and "open" in info:
                return "aberto" if bool(info["open"]) else "fechado"
        return None
    except Exception as exc:
        print(f"[iq] erro get_market_status({asset}): {exc}")
        return None

def _ensure_active(asset: str) -> bool:
    """Garante que o ticker exista no catálogo ACTIVES do iqoptionapi."""
    if _api is None:
        return False
    key = asset.upper()
    if key in OP_code.ACTIVES:
        return True
    try:
        _api.update_ACTIVES_OPCODE()
    except Exception as exc:
        print(f"[iq] não foi possível atualizar ACTIVES para {asset}: {exc}")
    return key in OP_code.ACTIVES
def _normalize(c: dict) -> dict:
    """Padroniza o dicionário de candle da IQ Option."""
    return {
        "time": int(c.get("from") or c.get("at") or 0),
        "open": float(c.get("open", 0) or 0),
        "high": float(c.get("max") or c.get("high") or 0),
        "low": float(c.get("min") or c.get("low") or 0),
        "close": float(c.get("close", 0) or 0),
        "volume": float(c.get("volume", 0) or 0),
    }


def get_candles(asset: str, interval: int = 300, count: int = 200) -> list[dict]:
    """Busca candles históricos via IQ Option."""
    if _api is None:
        return []
    try:
        if not _ensure_active(asset):
            raise ValueError(f"Ativo não encontrado no catálogo da IQ Option: {asset}")
        raw = _api.get_candles(asset, interval, count, time.time())
        if isinstance(raw, dict):
            raw = raw.get("candles") or raw.get("data") or []
        return [_normalize(c) for c in (raw or [])]
    except Exception as e:
        print(f"[iq] erro get_candles({asset}, {interval}): {e}")
        return []


def start_stream(asset: str, interval: int = 300, count: int = 200) -> bool:
    """Inicia streaming em tempo real de um ativo."""
    if _api is None:
        return False
    if not _ensure_active(asset):
        return False
    key = (asset, interval)
    with _lock:
        if _streams.get(key):
            return True
        try:
            _api.start_candles_stream(asset, interval, count)
            _streams[key] = True
            return True
        except Exception as e:
            print(f"[iq] erro start_stream({asset}, {interval}): {e}")
            return False


def stop_stream(asset: str, interval: int = 300):
    if _api is None:
        return
    key = (asset, interval)
    with _lock:
        if not _streams.get(key):
            return
        try:
            _api.stop_candles_stream(asset, interval)
        except Exception:
            pass
        _streams.pop(key, None)


def get_realtime_candles(asset: str, interval: int = 300) -> list[dict]:
    """Lê os candles atualizados do cache do stream."""
    if _api is None:
        return []
    try:
        raw = _api.get_realtime_candles(asset, interval)
        if not isinstance(raw, dict):
            return []
        # Formato típico: {interval: {timestamp: candle}} ou {timestamp: candle}
        data = raw.get(str(interval)) or raw.get(interval)
        if isinstance(data, dict):
            candles = list(data.values())
        else:
            # Fallback: assume que o dict é o próprio conjunto de candles
            candles = [
                v for v in raw.values()
                if isinstance(v, dict) and "close" in v
            ]
        return [_normalize(c) for c in candles]
    except Exception as e:
        print(f"[iq] erro get_realtime_candles({asset}): {e}")
        return []


def get_candles_smart(asset: str, interval: int = 300, count: int = 200) -> list[dict]:
    """
    Estratégia híbrida:
      1) Tenta pegar do stream em tempo real.
      2) Se vazio, busca históricos.
      3) Retorna os últimos `count` candles.
    """
    candles = get_realtime_candles(asset, interval)
    if len(candles) < 30:
        candles = get_candles(asset, interval, count)
    return candles[-count:] if len(candles) > count else candles