"""Stream de eventos en vivo (ring buffer en memoria).

Tipos:
  - delivery        : visita pasa pending -> completed (entregada antes de window_end)
  - failed_delivery : visita pasa pending -> completed pero llego despues de window_end
  - alert_triggered : visita cruza p_fallo >= 0.5 con horizonte >= 2h (NUEVA alerta VD)
  - alert_cleared   : visita que estaba en alerta vuelve a p_fallo < 0.5
  - red_simpli      : visita pendiente cruza slack <= 0 (rojo SimpliRoute nuevo)
  - incident_auto   : random ~5% prob por tick: spike de delay en un vehiculo
  - tick            : marcador heartbeat del scheduler (siempre, para mostrar vida)
"""
from __future__ import annotations

import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque

MAX_EVENTS = 200


class EventLog:
    def __init__(self, maxlen: int = MAX_EVENTS):
        self._events: Deque[dict] = deque(maxlen=maxlen)

    def emit(self, type_: str, sim_clock: datetime, payload: dict[str, Any]) -> dict:
        evt = {
            "event_id": uuid.uuid4().hex,
            "type": type_,
            "sim_ts": sim_clock.isoformat(),
            "wall_ts": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        self._events.append(evt)
        return evt

    def recent(self, limit: int = 50,
               types: list[str] | None = None) -> list[dict]:
        items = list(self._events)
        if types:
            items = [e for e in items if e["type"] in types]
        items.reverse()
        return items[:limit]

    def reset(self) -> int:
        """Limpia el buffer. Devuelve cuántos eventos se eliminaron."""
        n = len(self._events)
        self._events.clear()
        return n


EVENTS = EventLog()
