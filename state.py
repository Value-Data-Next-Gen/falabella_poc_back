"""Estado en memoria de la torre de control.

Singleton-ish: una sola instancia de AppState por proceso. Encapsula:
- modelo entrenado (cargado al startup)
- maestros (drivers, vehicles ext, clients) cargados al startup
- plan del dia + reloj simulado
- incidentes manuales y automaticos
- snapshot mas reciente de visitas + SHAP
- detecciones de transiciones tick-a-tick -> stream de eventos

Mutaciones protegidas con Lock para coexistir con el scheduler de APScheduler.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from events import EVENTS
from masters import build_client_master, gen_drivers, gen_vehicles_extended
from pipeline import (
    DAY_END,
    DAY_START,
    apply_status_and_predict,
    gen_today_plan,
    train_model,
    PRICE_PER_RESCUE_CLP,
    RESCUE_RATE,
)


@dataclass
class AppState:
    boot: dict[str, Any] | None = None

    # Maestros (estilo SimpliRoute: drivers, vehicles, clients)
    drivers: list[dict] = field(default_factory=list)
    vehicles_ext: list[dict] = field(default_factory=list)
    clients_master: list[dict] = field(default_factory=list)
    historical_df: pd.DataFrame | None = None

    # Estado del dia
    today: date | None = None
    day_seed: int = 0
    sim_clock: datetime | None = None
    manual_incidents: dict[int, float] = field(default_factory=dict)
    auto_incidents: dict[int, float] = field(default_factory=dict)

    # Plan del dia
    today_plan: pd.DataFrame | None = None
    snapshot_df: pd.DataFrame | None = None
    shap_vals: np.ndarray | None = None
    last_tick_at: datetime | None = None

    # Auto-advance
    auto_advance: bool = True
    sim_minutes_per_tick: int = 3

    # Estado anterior para diff de transiciones
    _prev_status: dict[str, str] = field(default_factory=dict)
    _prev_alert_vd: dict[str, bool] = field(default_factory=dict)
    _prev_alert_slack: dict[str, str] = field(default_factory=dict)

    _rng: np.random.Generator = field(default_factory=lambda: np.random.default_rng(7))
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _ticks: int = 0

    # ----- Lifecycle -----
    def init(self) -> None:
        self.boot = train_model()

        # Reconstruct historical_df briefly to derive client metrics.
        # Para ahorrar memoria: solo lo usamos al cargar masters.
        self.historical_df = self._regen_historical_for_masters()

        self.drivers = gen_drivers()
        self.vehicles_ext = gen_vehicles_extended(self.drivers)
        self.clients_master = build_client_master(self.boot["customers"], self.historical_df)

        self.today = self.boot["today"]
        self.sim_clock = datetime.combine(self.today, DAY_START)
        self._regen_plan()
        self._refresh_snapshot(emit_events=False)

    def _regen_historical_for_masters(self) -> pd.DataFrame:
        """Regenera 60 dias para metricas de clientes. Costoso pero solo una vez."""
        from pipeline import N_HISTORICAL_DAYS, gen_day_visits
        customers = self.boot["customers"]
        today = self.boot["today"]
        dfs = [
            gen_day_visits(d, today - timedelta(days=N_HISTORICAL_DAYS - d), customers)
            for d in range(N_HISTORICAL_DAYS)
        ]
        return pd.concat(dfs, ignore_index=True)

    def _regen_plan(self) -> None:
        assert self.boot is not None and self.today is not None
        self.today_plan = gen_today_plan(self.today, self.day_seed, self.boot["customers"])
        # Reset transition memory
        self._prev_status = {}
        self._prev_alert_vd = {}
        self._prev_alert_slack = {}

    def _all_incidents(self) -> dict[int, float]:
        merged: dict[int, float] = {}
        for d in (self.manual_incidents, self.auto_incidents):
            for k, v in d.items():
                merged[int(k)] = merged.get(int(k), 0.0) + float(v)
        return merged

    def _refresh_snapshot(self, emit_events: bool = True) -> None:
        assert self.boot is not None and self.today_plan is not None and self.sim_clock is not None
        df, shap_vals = apply_status_and_predict(
            self.today_plan,
            self.sim_clock,
            self._all_incidents(),
            self.boot["calibrated_model"],
            self.boot["shap_explainer"],
            self.boot["feature_names"],
            self.boot["comuna_rate"],
        )
        if emit_events and self.snapshot_df is not None:
            self._emit_transitions(df)
        self.snapshot_df = df
        self.shap_vals = shap_vals
        self.last_tick_at = datetime.utcnow()

        # Update transition memory
        self._prev_status = dict(zip(df["tracking_id"].astype(str), df["status"].astype(str)))
        self._prev_alert_vd = dict(zip(df["tracking_id"].astype(str), df["alert_valuedata"].astype(bool)))
        self._prev_alert_slack = dict(zip(df["tracking_id"].astype(str), df["alert_slack"].astype(str)))

    def _emit_transitions(self, new_df: pd.DataFrame) -> None:
        """Compara new_df vs estado previo y emite eventos."""
        assert self.sim_clock is not None
        for _, row in new_df.iterrows():
            tid = str(row["tracking_id"])

            # Status transition: pending -> completed
            prev_status = self._prev_status.get(tid)
            if prev_status == "pending" and row["status"] == "completed":
                if int(row["failed"]) == 1:
                    EVENTS.emit("failed_delivery", self.sim_clock, {
                        "tracking_id": tid,
                        "vehicle_id": int(row["vehicle_id"]),
                        "vehicle_name": str(row["vehicle_name"]),
                        "title": str(row["title"]),
                        "window_end": str(row["window_end"]),
                        "eta": str(row["estimated_time_arrival"]),
                        "delay_min": float(round(-row["slack_min"], 1)),
                    })
                else:
                    EVENTS.emit("delivery", self.sim_clock, {
                        "tracking_id": tid,
                        "vehicle_id": int(row["vehicle_id"]),
                        "vehicle_name": str(row["vehicle_name"]),
                        "title": str(row["title"]),
                        "window_end": str(row["window_end"]),
                        "eta": str(row["estimated_time_arrival"]),
                        "slack_min": float(round(row["slack_min"], 1)),
                    })

            # Alert VD: false -> true
            prev_vd = self._prev_alert_vd.get(tid, False)
            if (not prev_vd) and bool(row["alert_valuedata"]):
                EVENTS.emit("alert_triggered", self.sim_clock, {
                    "tracking_id": tid,
                    "vehicle_id": int(row["vehicle_id"]),
                    "vehicle_name": str(row["vehicle_name"]),
                    "title": str(row["title"]),
                    "window_end": str(row["window_end"]),
                    "p_fallo": float(round(row["p_fallo"], 3)),
                    "horas_hasta_we": float(round(row["horas_hasta_we"], 1)),
                })
            elif prev_vd and not bool(row["alert_valuedata"]):
                EVENTS.emit("alert_cleared", self.sim_clock, {
                    "tracking_id": tid,
                    "vehicle_id": int(row["vehicle_id"]),
                    "vehicle_name": str(row["vehicle_name"]),
                    "title": str(row["title"]),
                    "p_fallo": float(round(row["p_fallo"], 3)),
                })

            # Slack alert: not RED -> RED (visita pendiente cruza slack <= 0)
            prev_slack = self._prev_alert_slack.get(tid)
            if (
                row["status"] == "pending"
                and prev_slack is not None
                and prev_slack != "RED"
                and str(row["alert_slack"]) == "RED"
            ):
                EVENTS.emit("red_simpli", self.sim_clock, {
                    "tracking_id": tid,
                    "vehicle_id": int(row["vehicle_id"]),
                    "vehicle_name": str(row["vehicle_name"]),
                    "title": str(row["title"]),
                    "window_end": str(row["window_end"]),
                    "slack_min": float(round(row["slack_min"], 1)),
                })

    # ----- Mutations -----
    def tick(self) -> None:
        with self._lock:
            self._ticks += 1
            if self.auto_advance and self.sim_clock is not None and self.today is not None:
                day_end_dt = datetime.combine(self.today, DAY_END)
                next_clock = self.sim_clock + timedelta(minutes=self.sim_minutes_per_tick)
                if next_clock > day_end_dt + timedelta(minutes=30):
                    next_clock = datetime.combine(self.today, DAY_START)
                self.sim_clock = next_clock

            # Auto-incidente random ~5% prob por tick durante horario operativo
            if (
                self.auto_advance
                and self.sim_clock is not None
                and DAY_START <= self.sim_clock.time() <= DAY_END
                and self._rng.random() < 0.05
            ):
                vid = int(self._rng.integers(1, 13))
                extra = float(round(self._rng.uniform(15.0, 35.0), 1))
                self.auto_incidents[vid] = self.auto_incidents.get(vid, 0.0) + extra
                EVENTS.emit("incident_auto", self.sim_clock, {
                    "vehicle_id": vid,
                    "vehicle_name": f"FAL-{1000 + vid - 1}",
                    "extra_min": extra,
                    "reason": "Trafico imprevisto / cierre de calle",
                })

            self._refresh_snapshot()

    def add_incident(self, vehicle_id: int, extra_min: float) -> None:
        with self._lock:
            self.manual_incidents[int(vehicle_id)] = (
                self.manual_incidents.get(int(vehicle_id), 0.0) + float(extra_min)
            )
            EVENTS.emit("incident_manual", self.sim_clock or datetime.utcnow(), {
                "vehicle_id": int(vehicle_id),
                "vehicle_name": f"FAL-{1000 + int(vehicle_id) - 1}",
                "extra_min": float(extra_min),
                "reason": "Operador agrego incidente",
            })
            self._refresh_snapshot()

    def reset_day(self) -> None:
        with self._lock:
            self.day_seed += 1
            self.manual_incidents = {}
            self.auto_incidents = {}
            self.sim_clock = datetime.combine(self.today, DAY_START)  # type: ignore[arg-type]
            self._regen_plan()
            EVENTS.emit("day_reset", self.sim_clock, {"new_day_seed": self.day_seed})
            self._refresh_snapshot(emit_events=False)

    def set_clock(self, sim_clock: datetime | None = None,
                  offset_minutes: int | None = None) -> None:
        with self._lock:
            assert self.today is not None and self.sim_clock is not None
            if sim_clock is not None:
                self.sim_clock = sim_clock
            elif offset_minutes is not None:
                day_end_dt = datetime.combine(self.today, DAY_END)
                day_start_dt = datetime.combine(self.today, DAY_START)
                new_clock = self.sim_clock + timedelta(minutes=offset_minutes)
                if new_clock < day_start_dt:
                    new_clock = day_start_dt
                if new_clock > day_end_dt + timedelta(minutes=30):
                    new_clock = day_end_dt + timedelta(minutes=30)
                self.sim_clock = new_clock
            self._refresh_snapshot()

    def set_auto_advance(self, value: bool) -> None:
        with self._lock:
            self.auto_advance = bool(value)


STATE = AppState()
