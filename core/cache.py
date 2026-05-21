"""TTL cache thread-safe minimalista para handlers FastAPI.

Casos de uso: respuestas pesadas que cambian con baja frecuencia (segundos)
pero el frontend polea agresivamente. Ej: `/api/plan-diario?source=real`
tarda ~17s contra Azure SQL y el front lo polea cada 10s → requests apilados
bloqueando event loop. Cachear con TTL de 5s elimina el problema.

NO usar para datos sensibles por usuario sin incluir el `user_id` en la key.
NO usar para writes (la cache asume idempotencia de la f cacheada).

Uso:
    from core.cache import ttl_cached

    @ttl_cached(ttl_seconds=5)
    def compute_expensive(arg1, arg2): ...

Con argumentos no-hashables: pasalos por kwargs explícitos y stringificalos
antes (la key del cache es repr(args) + sorted(kwargs.items())).
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

T = TypeVar("T")

_lock = threading.Lock()
_store: dict[tuple, tuple[float, Any]] = {}
# Lock por key: requests concurrentes al mismo (fn, args) esperan a la
# primera en lugar de recomputar (thundering herd). Sin esto, con N requests
# concurrentes al mismo endpoint costoso (ej. 5 polls de 10s superponiéndose
# en una query de 50s), TODOS pagan la query en lugar de esperar al cache.
_keylocks: dict[tuple, threading.Lock] = {}


def _get_keylock(key: tuple) -> threading.Lock:
    with _lock:
        kl = _keylocks.get(key)
        if kl is None:
            kl = threading.Lock()
            _keylocks[key] = kl
        return kl


def ttl_cached(ttl_seconds: float) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorador. Cachea el retorno por (fn_name, args, kwargs) durante
    `ttl_seconds`. Pasado el TTL recomputa. Thread-safe + single-flight."""
    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        fn_name = f"{fn.__module__}.{fn.__qualname__}"

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            key = (fn_name, repr(args), tuple(sorted(kwargs.items())))
            now = time.time()
            # Fast path: cache hit
            with _lock:
                hit = _store.get(key)
                if hit is not None and now - hit[0] < ttl_seconds:
                    return hit[1]
            # Slow path: lock por key para single-flight
            kl = _get_keylock(key)
            with kl:
                # Re-check después de adquirir el keylock — otra request
                # pudo haber computado mientras esperábamos.
                now = time.time()
                with _lock:
                    hit = _store.get(key)
                    if hit is not None and now - hit[0] < ttl_seconds:
                        return hit[1]
                value = fn(*args, **kwargs)
                with _lock:
                    _store[key] = (time.time(), value)
                return value

        return wrapper
    return deco


def invalidate_all() -> int:
    """Borra toda la cache. Devuelve el conteo de entries removidas."""
    with _lock:
        n = len(_store)
        _store.clear()
    return n


def invalidate_prefix(prefix: str) -> int:
    """Borra entries cuya key (fn_name) empieza con `prefix`. Útil para
    invalidar todo el módulo de plan-diario tras un clean-and-regenerate."""
    with _lock:
        keys = [k for k in _store if k[0].startswith(prefix)]
        for k in keys:
            del _store[k]
    return len(keys)


def stats() -> dict:
    """Para debugging."""
    with _lock:
        return {"entries": len(_store), "keys": list(set(k[0] for k in _store))}
