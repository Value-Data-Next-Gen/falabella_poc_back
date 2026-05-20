"""Pipeline ML puro: generacion de datos sinteticos, featurize, entrenamiento, inferencia.

Extraido de demo_valuedata.py. Sin dependencias de Streamlit. Funciones puras.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd
import shap
import xgboost as xgb
from faker import Faker
from sklearn.calibration import CalibratedClassifierCV, calibration_curve
from sklearn.metrics import brier_score_loss, confusion_matrix, roc_auc_score


# =============================================================================
# CONSTANTES
# =============================================================================
SEED = 42
N_HISTORICAL_DAYS = 60
# Volumen base: ~600 visitas/día. Multiplicador DOW × mes ajusta por estacionalidad
# real (ver synthetic_calibration). Domingos terminan en ~66 visitas (factor 0.11),
# días peak (jun/oct/dic) llegan a ~1000.
N_VISITS_PER_DAY = 600
N_VEHICLES = 12
N_UNIQUE_CUSTOMERS = 2000
DEPOT = (-33.45, -70.66)
COMUNA_GRID = 0.05
# PLACEHOLDER POC — sin fuente oficial.
# Reemplazar por el costo real de reagendamiento que entregue el área de
# operaciones de Falabella. Mientras tanto el KPI "Delta de rescate" queda
# oculto en el frontend para no mostrar un número especulativo al cliente.
PRICE_PER_RESCUE_CLP = 8000        # <-- placeholder, no usar en comunicación
RESCUE_RATE = 0.60                 # <-- placeholder, supuesto de eficacia VD
ALERT_THRESHOLD = 0.50
ANTICIPATION_HOURS = 2.0
DAY_START = dtime(9, 0)
DAY_END = dtime(20, 30)


# =============================================================================
# PATRONES OCULTOS
# =============================================================================
def setup_hidden_patterns(seed: int) -> dict:
    rng = np.random.default_rng(seed)
    problem_comunas: set[tuple[float, float]] = set()
    while len(problem_comunas) < 3:
        lat = round((DEPOT[0] + rng.uniform(-0.18, 0.18)) / COMUNA_GRID) * COMUNA_GRID
        lon = round((DEPOT[1] + rng.uniform(-0.18, 0.18)) / COMUNA_GRID) * COMUNA_GRID
        problem_comunas.add((round(lat, 3), round(lon, 3)))
    problem_drivers = {int(x) + 1 for x in rng.choice(N_VEHICLES, size=2, replace=False)}
    return {"problem_comunas": problem_comunas, "problem_drivers": problem_drivers}


PATTERNS = setup_hidden_patterns(SEED)


# =============================================================================
# HELPERS
# =============================================================================
def comuna_of(lat: float, lon: float) -> str:
    glat = round(round(lat / COMUNA_GRID) * COMUNA_GRID, 3)
    glon = round(round(lon / COMUNA_GRID) * COMUNA_GRID, 3)
    return f"{glat:.3f}_{glon:.3f}"


def is_problem_comuna_coords(lat: float, lon: float) -> bool:
    glat = round(round(lat / COMUNA_GRID) * COMUNA_GRID, 3)
    glon = round(round(lon / COMUNA_GRID) * COMUNA_GRID, 3)
    return (glat, glon) in PATTERNS["problem_comunas"]


def haversine_km_vec(lat1: np.ndarray, lon1: np.ndarray,
                     lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    R = 6371.0
    lat1r = np.radians(lat1)
    lat2r = np.radians(lat2)
    dlat = lat2r - lat1r
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1r) * np.cos(lat2r) * np.sin(dlon / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(a))


def franja_factor(hour: int) -> float:
    if 9 <= hour < 11:
        return 1.0
    if 11 <= hour < 14:
        return 1.3
    if 14 <= hour < 17:
        return 1.15
    return 1.45


# =============================================================================
# POOL DE CLIENTES
# =============================================================================
def gen_customer_pool(seed: int = SEED) -> list[dict]:
    """Genera pool de clientes con distribución regional realista.

    ~83% RM (con 20% en problem_comunas escondidos para que el modelo aprenda
    el patrón); ~17% repartido en 7 regiones según pesos del Excel del cliente.
    Cada cliente lleva region/comuna y depot regional para evitar distancias
    Santiago-Concepción inválidas en el modelo.
    """
    from ml.synthetic_calibration import (
        REGION_WEIGHTS, gen_latlon_for_region, pick_comuna, depot_for_region,
    )
    rng = np.random.default_rng(seed + 1)
    fake = Faker("es_CL")
    Faker.seed(seed + 1)
    customers: list[dict] = []
    pc_list = sorted(PATTERNS["problem_comunas"])

    regions = list(REGION_WEIGHTS.keys())
    weights = np.array([REGION_WEIGHTS[r] for r in regions])
    weights = weights / weights.sum()

    for i in range(N_UNIQUE_CUSTOMERS):
        region = str(rng.choice(regions, p=weights))
        if region == "RM":
            in_problem = bool(rng.random() < 0.20)
            if in_problem:
                pc = pc_list[int(rng.integers(0, len(pc_list)))]
                lat = pc[0] + rng.uniform(-COMUNA_GRID * 0.45, COMUNA_GRID * 0.45)
                lon = pc[1] + rng.uniform(-COMUNA_GRID * 0.45, COMUNA_GRID * 0.45)
            else:
                for _ in range(20):
                    lat = DEPOT[0] + rng.uniform(-0.22, 0.22)
                    lon = DEPOT[1] + rng.uniform(-0.22, 0.22)
                    if not is_problem_comuna_coords(lat, lon):
                        break
            comuna = pick_comuna("RM", rng)
        else:
            lat, lon = gen_latlon_for_region(region, rng)
            comuna = pick_comuna(region, rng)

        depot_lat, depot_lon = depot_for_region(region)
        is_recurrent = bool(rng.random() < 0.15)
        customers.append({
            "customer_id": f"C{i:04d}",
            "title": fake.company(),
            "address": fake.address().replace("\n", ", "),
            "latitude": float(lat),
            "longitude": float(lon),
            "region": region,
            "comuna": comuna,
            "_depot_lat": float(depot_lat),
            "_depot_lon": float(depot_lon),
            "_is_recurrent": is_recurrent,
            "_in_problem_comuna": is_problem_comuna_coords(lat, lon) if region == "RM" else False,
        })
    return customers


# =============================================================================
# GENERACION DE UN DIA
# =============================================================================
def gen_day_visits(day_idx: int, planned_date: date, customers: list[dict]) -> pd.DataFrame:
    from ml.synthetic_calibration import daily_volume_factor, sample_subordenes
    seed = SEED + 1000 * day_idx
    rng = np.random.default_rng(seed)

    # Volumen del día = base × factor DOW × factor mes (ver synthetic_calibration).
    factor = daily_volume_factor(planned_date)
    n_visits = max(1, int(round(N_VISITS_PER_DAY * factor)))

    cust_idx = rng.integers(0, len(customers), size=n_visits)
    visits = []
    for i, ci in enumerate(cust_idx):
        c = customers[int(ci)]
        load = float(round(rng.uniform(0.5, 25.0), 2))
        we_h = int(rng.choice([14, 17, 18, 19, 20], p=[0.20, 0.25, 0.20, 0.20, 0.15]))
        buffer_min = int(rng.uniform(45, 100))
        pa_dt = datetime.combine(planned_date, dtime(we_h, 0)) - timedelta(minutes=buffer_min)
        visits.append({
            "id": f"V{day_idx:03d}-{i:04d}",
            "tracking_id": f"TRK{day_idx:03d}{i:04d}",
            "customer_id": c["customer_id"],
            "title": c["title"],
            "address": c["address"],
            "latitude": c["latitude"],
            "longitude": c["longitude"],
            "region": c.get("region", "RM"),
            "comuna": c.get("comuna", "Santiago"),
            "_depot_lat": c.get("_depot_lat", DEPOT[0]),
            "_depot_lon": c.get("_depot_lon", DEPOT[1]),
            "load": load,
            "n_subordenes": sample_subordenes(rng),
            "window_start": "09:00:00",
            "window_end": f"{we_h:02d}:00:00",
            "planned_arrival_time": pa_dt.strftime("%H:%M:%S"),
            "planned_date": planned_date.isoformat(),
            "reference": f"FAL-{int(rng.integers(100000, 999999))}",
            "_is_recurrent": c["_is_recurrent"],
            "_in_problem_comuna": c["_in_problem_comuna"],
        })

    rng.shuffle(visits)
    per_vehicle = n_visits // N_VEHICLES
    extra = n_visits - per_vehicle * N_VEHICLES
    cursor = 0
    for vidx in range(N_VEHICLES):
        cnt = per_vehicle + (1 if vidx < extra else 0)
        v_visits = visits[cursor:cursor + cnt]
        cursor += cnt
        v_visits.sort(key=lambda v: v["planned_arrival_time"])
        for order, v in enumerate(v_visits, start=1):
            v["vehicle_id"] = vidx + 1
            v["vehicle_name"] = f"FAL-{1000 + vidx}"
            v["order"] = order

    df = pd.DataFrame(visits)
    return _compute_eta_and_failure(df, rng)


def _compute_eta_and_failure(df: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    veh_factor = {int(v): float(rng.normal(1.0, 0.1)) for v in df["vehicle_id"].unique()}
    incidents: dict[int, tuple[int, float]] = {}
    for v in df["vehicle_id"].unique():
        if rng.random() < 0.03:
            n = int((df["vehicle_id"] == v).sum())
            start_at = int(rng.integers(1, max(2, n // 2)))
            extra_min = float(rng.uniform(20.0, 45.0))
            incidents[int(v)] = (start_at, extra_min)

    we_h = df["window_end"].str[:2].astype(int)
    pen = np.ones(len(df))
    pen[df["_in_problem_comuna"].values] *= 3.0
    pen[((we_h >= 17) & (we_h <= 19)).values] *= 2.0
    pen[df["vehicle_id"].isin(PATTERNS["problem_drivers"]).values] *= 2.5
    pen[(df["load"] > 15).values] *= 1.25
    pen[df["_is_recurrent"].values] *= 4.0
    df = df.copy()
    df["_penalty_mult"] = pen

    out_blocks = []
    for v_id, vdf in df.groupby("vehicle_id"):
        vdf = vdf.sort_values("order").reset_index(drop=True)
        prev_delay = 0.0
        vfac = veh_factor[int(v_id)]
        inc = incidents.get(int(v_id))
        delays = []
        for _, row in vdf.iterrows():
            h = int(row["planned_arrival_time"][:2])
            franja = franja_factor(h)
            local = float(rng.normal(0.0, 6.0)) * vfac * franja
            pen_extra = float(rng.exponential(scale=4.0)) * (row["_penalty_mult"] - 1.0)
            delay = local + pen_extra + 0.6 * prev_delay
            if inc is not None and int(row["order"]) >= inc[0]:
                delay += inc[1]
            delays.append(delay)
            prev_delay = delay
        vdf["delay_min"] = delays
        out_blocks.append(vdf)

    df = pd.concat(out_blocks, ignore_index=True)
    pa = pd.to_datetime(df["planned_date"] + "T" + df["planned_arrival_time"])
    we = pd.to_datetime(df["planned_date"] + "T" + df["window_end"])
    df["planned_arrival_dt"] = pa
    df["window_end_dt"] = we
    df["eta_real"] = pa + pd.to_timedelta(df["delay_min"], unit="min")
    df["slack_min"] = (we - df["eta_real"]).dt.total_seconds() / 60.0
    df["failed"] = (df["eta_real"] > we).astype(int)
    return df


# =============================================================================
# FEATURIZACION
# =============================================================================
NUMERIC_FEATURES = [
    "hora_window_end",
    "carga",
    "dist_depot_km",
    "orden_en_ruta",
    "retraso_acumulado_vehiculo",
    "tasa_fallo_historica_cliente",
    "horas_hasta_window_end",
]


def featurize(df: pd.DataFrame, comuna_failure_rate: dict[str, float] | None = None,
              now_clock: datetime | None = None,
              randomize_observation: bool = False,
              rng: np.random.Generator | None = None) -> pd.DataFrame:
    df = df.copy()
    df["carga"] = df["load"]
    df["orden_en_ruta"] = df["order"]
    df["hora_window_end"] = df["window_end"].str[:2].astype(int)
    df["dia_semana"] = pd.to_datetime(df["planned_date"]).dt.dayofweek

    glat = (np.round(df["latitude"].values / COMUNA_GRID) * COMUNA_GRID).round(3)
    glon = (np.round(df["longitude"].values / COMUNA_GRID) * COMUNA_GRID).round(3)
    df["comuna_id"] = [f"{la:.3f}_{lo:.3f}" for la, lo in zip(glat, glon)]
    df["conductor_id"] = "v" + df["vehicle_id"].astype(int).astype(str)
    # Distancia al CD regional (no Santiago) si hay info; fallback a DEPOT global.
    if "_depot_lat" in df.columns and "_depot_lon" in df.columns:
        depot_lat = df["_depot_lat"].fillna(DEPOT[0]).values
        depot_lon = df["_depot_lon"].fillna(DEPOT[1]).values
    else:
        depot_lat = np.full(len(df), DEPOT[0])
        depot_lon = np.full(len(df), DEPOT[1])
    df["dist_depot_km"] = haversine_km_vec(
        depot_lat, depot_lon,
        df["latitude"].values, df["longitude"].values,
    )

    if comuna_failure_rate is not None:
        df["tasa_fallo_historica_cliente"] = (
            df["comuna_id"].map(comuna_failure_rate).fillna(0.15)
        )
    else:
        df["tasa_fallo_historica_cliente"] = 0.15

    we = pd.to_datetime(df["planned_date"] + "T" + df["window_end"])
    day_start = pd.to_datetime(df["planned_date"] + "T09:00:00")
    if now_clock is not None:
        ref = pd.Series([pd.Timestamp(now_clock)] * len(df), index=df.index)
    elif randomize_observation:
        if rng is None:
            rng = np.random.default_rng(SEED + 99)
        span_sec = (we - day_start).dt.total_seconds().clip(lower=0).values
        offsets_sec = span_sec * rng.uniform(0.0, 1.0, size=len(df))
        ref = day_start + pd.to_timedelta(offsets_sec, unit="s")
    else:
        ref = pd.to_datetime(df["planned_date"] + "T" + df["planned_arrival_time"])

    df["_ref"] = ref.values
    df["horas_hasta_window_end"] = (
        (we - df["_ref"]).dt.total_seconds() / 3600.0
    ).clip(lower=0.0)

    if "delay_min" not in df.columns:
        df["delay_min"] = 0.0
    if "eta_real" in df.columns:
        df = df.copy()
        unobs = df["eta_real"] > df["_ref"]
        df.loc[unobs, "delay_min"] = 0.0

    df = df.sort_values(["planned_date", "vehicle_id", "order"]).reset_index(drop=True)
    df["retraso_acumulado_vehiculo"] = df.groupby(["planned_date", "vehicle_id"])["delay_min"].transform(
        lambda x: x.shift(1).fillna(0.0).cumsum()
    )

    base = df[NUMERIC_FEATURES + ["comuna_id", "conductor_id", "dia_semana"]]
    X = pd.get_dummies(
        base,
        columns=["comuna_id", "conductor_id", "dia_semana"],
        prefix=["comuna", "drv", "dow"],
        dtype=float,
    )
    if "failed" in df.columns:
        X = X.copy()
        X["failed"] = df["failed"].values
    return X


def align_columns(X: pd.DataFrame, train_cols: list[str]) -> pd.DataFrame:
    for c in train_cols:
        if c not in X.columns:
            X[c] = 0.0
    return X[train_cols]


# =============================================================================
# ENTRENAMIENTO
# =============================================================================
def _load_historical_real(end_date: date, n_days: int) -> Optional[pd.DataFrame]:
    """Carga histórico real de fpoc_simpli_visits para entrenar el modelo.

    Mapea cada fila al mismo schema que _load_real_plan (lat/lon determinista
    por region+id, vehicle_id capeado a 1..12, etc.) y agrega:
      - failed: label binaria. 1 si status=='failed' o sla_hour_checkout_eta>1.0
      - delay_min: derivado de sla_hour_checkout_eta (positivo = atrasado)
      - eta_real / slack_min: derivados.

    Devuelve None si hay <1000 visitas (muestra muy chica → fallback sintético).
    """
    import os
    if os.environ.get("DISABLE_REAL_TRAIN", "false").lower() == "true":
        return None
    try:
        from core.db import get_conn
        from ml.synthetic_calibration import REGION_BBOXES, REGION_DEPOTS
    except Exception:  # noqa: BLE001
        return None

    start = end_date - timedelta(days=n_days)
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                """
                SELECT id, planned_date, title, "order", current_eta_cl, status,
                       sla_hour_checkout_eta, patente_falsa, driver_name,
                       region, comuna
                FROM fpoc_simpli_visits
                WHERE planned_date >= ? AND planned_date < ?
                ORDER BY planned_date, patente_falsa, "order"
                """,
                (start.isoformat(), end_date.isoformat()),
            )
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        from loguru import logger
        logger.warning(f"[pipeline] _load_historical_real DB error: {e}")
        return None

    if len(rows) < 1000:
        return None

    visits = []
    for r in rows:
        vid_db = int(r[7]) if r[7] is not None else 1
        vid = ((vid_db - 1) % N_VEHICLES) + 1
        region = str(r[9]) if r[9] else "RM"
        if region not in REGION_BBOXES:
            region = "RM"
        bbox = REGION_BBOXES[region]
        depot_lat, depot_lon = REGION_DEPOTS.get(region, REGION_DEPOTS["RM"])
        rng = np.random.default_rng(int(r[0]) & 0xFFFFFFFF)
        lat = float(rng.uniform(bbox[0], bbox[1]))
        lon = float(rng.uniform(bbox[2], bbox[3]))

        # Window end derivado del ETA reportado
        eta_str = str(r[4]) if r[4] else ""
        we_h = 18
        try:
            time_part = ""
            if "T" in eta_str:
                time_part = eta_str.split("T", 1)[1]
            elif " " in eta_str:
                time_part = eta_str.split(" ", 1)[1]
            if time_part:
                we_h = int(time_part[:2]) + 1
        except Exception:  # noqa: BLE001
            pass
        we_h = max(14, min(20, we_h))
        window_end_str = f"{we_h:02d}:00:00"

        # Label estricta: SOLO status='failed' (5% positivos en BD real).
        # Incluir sla>1h dió 30% positivos y AUC=0.52 (modelo no aprende).
        sla_h = float(r[6]) if r[6] is not None else 0.0
        status_db = (str(r[5]) or "").lower()
        failed = 1 if status_db == "failed" else 0

        # delay_min: positivo = atrasado (en min). Útil como feature.
        delay_min = max(0.0, sla_h * 60.0)

        # planned_date como string ISO
        pd_str = str(r[1])[:10] if r[1] else end_date.isoformat()
        # planned_arrival_time = window_end - random buffer
        buffer_min = int(rng.uniform(45, 100))
        try:
            pd_date = date.fromisoformat(pd_str)
        except Exception:  # noqa: BLE001
            pd_date = end_date
        pa_dt = datetime.combine(pd_date, dtime(we_h, 0)) - timedelta(minutes=buffer_min)
        pa_str = pa_dt.strftime("%H:%M:%S")

        visits.append({
            "id": f"V{r[0]}",
            "tracking_id": str(r[0]),
            "customer_id": f"C{int(r[0]) % 10000:04d}",
            "title": str(r[2] or "Cliente"),
            "address": "",
            "latitude": lat,
            "longitude": lon,
            "region": region,
            "comuna": str(r[10]) if r[10] else "Santiago",
            "_depot_lat": float(depot_lat),
            "_depot_lon": float(depot_lon),
            "load": float(round(rng.uniform(0.5, 25.0), 2)),
            "n_subordenes": 1,
            "window_start": "09:00:00",
            "window_end": window_end_str,
            "planned_arrival_time": pa_str,
            "planned_date": pd_str,
            "reference": str(r[0]),
            "_is_recurrent": False,
            "_in_problem_comuna": False,
            "vehicle_id": vid,
            "vehicle_name": f"FAL-{1000 + vid - 1}",
            "order": int(r[3]) if r[3] is not None else 1,
            "delay_min": delay_min,
            "failed": failed,
        })

    df = pd.DataFrame(visits)
    # Necesario para featurize: eta_real, slack_min
    pa = pd.to_datetime(df["planned_date"] + "T" + df["planned_arrival_time"])
    we = pd.to_datetime(df["planned_date"] + "T" + df["window_end"])
    df["planned_arrival_dt"] = pa
    df["window_end_dt"] = we
    df["eta_real"] = pa + pd.to_timedelta(df["delay_min"], unit="min")
    df["slack_min"] = (we - df["eta_real"]).dt.total_seconds() / 60.0
    return df


def train_model() -> dict:
    """Entrena XGB + CalibratedClassifierCV + SHAP.

    Prioridad: BD real (fpoc_simpli_visits últimos N días) si hay >=1k visitas.
    Fallback: sintético (N_HISTORICAL_DAYS días generados).
    """
    from loguru import logger
    customers = gen_customer_pool(SEED)
    today = date.today()

    def _train_synthetic():
        dfs = []
        for d in range(N_HISTORICAL_DAYS):
            dfs.append(gen_day_visits(d, today - timedelta(days=N_HISTORICAL_DAYS - d), customers))
        return pd.concat(dfs, ignore_index=True)

    real_hist = _load_historical_real(today, N_HISTORICAL_DAYS)
    if real_hist is not None and len(real_hist) >= 1000:
        hist = real_hist
        source = "real"
        logger.info(
            f"[train_model] intento BD real: {len(hist)} visitas, "
            f"{hist['failed'].sum()} failed ({100 * hist['failed'].mean():.1f}%)"
        )
    else:
        hist = _train_synthetic()
        source = "synthetic"
        logger.info(f"[train_model] sintético directo: {len(hist)} visitas")

    glat = (np.round(hist["latitude"].values / COMUNA_GRID) * COMUNA_GRID).round(3)
    glon = (np.round(hist["longitude"].values / COMUNA_GRID) * COMUNA_GRID).round(3)
    hist["_comuna_id_tmp"] = [f"{la:.3f}_{lo:.3f}" for la, lo in zip(glat, glon)]
    comuna_rate = hist.groupby("_comuna_id_tmp")["failed"].mean().to_dict()

    X_full = featurize(
        hist, comuna_failure_rate=comuna_rate,
        randomize_observation=True, rng=np.random.default_rng(SEED + 99),
    )
    y_full = X_full.pop("failed").astype(int).values
    train_cols = X_full.columns.tolist()

    hist_sorted = hist.sort_values(["planned_date", "vehicle_id", "order"]).reset_index(drop=True)
    dates_sorted = sorted(hist["planned_date"].unique())
    # Split temporal: ~80% del rango de fechas para train, resto para val.
    # Robusto cuando hay pocos días distintos (BD real puede tener <50).
    n_train_dates = max(1, int(len(dates_sorted) * 0.8))
    train_dates = set(dates_sorted[:n_train_dates])
    is_train = hist_sorted["planned_date"].isin(train_dates).values

    X_train = X_full.iloc[is_train]
    X_val = X_full.iloc[~is_train]
    y_train = y_full[is_train]
    y_val = y_full[~is_train]
    # Si el split temporal dejó val vacío (todas las fechas en train), tomamos
    # un 20% holdout aleatorio del train.
    if len(X_val) == 0:
        from loguru import logger
        logger.warning("[train_model] split temporal dió val=0; usando holdout aleatorio 20%")
        rng_split = np.random.default_rng(SEED)
        idx = np.arange(len(X_train))
        rng_split.shuffle(idx)
        cut = int(len(idx) * 0.8)
        X_val = X_train.iloc[idx[cut:]].reset_index(drop=True)
        y_val = y_train[idx[cut:]]
        X_train = X_train.iloc[idx[:cut]].reset_index(drop=True)
        y_train = y_train[idx[:cut]]

    # Si tenemos UNA sola clase en y_train (ej: todas pending recién cargadas),
    # CalibratedClassifierCV degenera y predict_proba devuelve [N x 1].
    # Saltamos directo al fallback sintético que sí tiene 2 clases.
    if len(np.unique(y_train)) < 2:
        from loguru import logger
        logger.warning(
            f"[train_model] y_train tiene 1 sola clase (n_pos={int((y_train==1).sum())}); "
            "saltamos al fallback sintético"
        )
        # Forzamos auc_real bajo para gatillar el fallback más abajo
        auc_real = 0.0
        cal = None  # placeholder; se reemplaza en el fallback
    else:
        spw = max(1.0, float((y_train == 0).sum() / max(1, (y_train == 1).sum())))
        base_xgb = xgb.XGBClassifier(
            n_estimators=300, max_depth=5, learning_rate=0.05,
            scale_pos_weight=spw, eval_metric="logloss",
            n_jobs=-1, random_state=SEED, tree_method="hist",
        )
        cal = CalibratedClassifierCV(base_xgb, method="isotonic", cv=3)
        cal.fit(X_train.values, y_train)

        p_val = cal.predict_proba(X_val.values)[:, 1]
        auc_real = float(roc_auc_score(y_val, p_val)) if len(np.unique(y_val)) > 1 else 0.5

    # Si entrenamos con BD real y el AUC quedó débil (<0.6), reentrenar con
    # sintético (que tiene patrones espaciales claros para que el ML aprenda).
    # La predicción sigue aplicándose al snapshot de datos reales.
    AUC_FALLBACK_THRESHOLD = 0.6
    if source == "real" and auc_real < AUC_FALLBACK_THRESHOLD:
        logger.warning(
            f"[train_model] AUC real {auc_real:.3f} < {AUC_FALLBACK_THRESHOLD}; "
            "fallback a entrenamiento sintético con espacial discriminante"
        )
        hist = _train_synthetic()
        source = "synthetic_fallback"
        # Re-featurize y re-split
        glat = (np.round(hist["latitude"].values / COMUNA_GRID) * COMUNA_GRID).round(3)
        glon = (np.round(hist["longitude"].values / COMUNA_GRID) * COMUNA_GRID).round(3)
        hist["_comuna_id_tmp"] = [f"{la:.3f}_{lo:.3f}" for la, lo in zip(glat, glon)]
        comuna_rate = hist.groupby("_comuna_id_tmp")["failed"].mean().to_dict()
        X_full = featurize(
            hist, comuna_failure_rate=comuna_rate,
            randomize_observation=True, rng=np.random.default_rng(SEED + 99),
        )
        y_full = X_full.pop("failed").astype(int).values
        train_cols = X_full.columns.tolist()
        hist_sorted = hist.sort_values(["planned_date", "vehicle_id", "order"]).reset_index(drop=True)
        dates_sorted = sorted(hist["planned_date"].unique())
        n_train_dates = max(1, int(len(dates_sorted) * 0.8))
        train_dates = set(dates_sorted[:n_train_dates])
        is_train = hist_sorted["planned_date"].isin(train_dates).values
        X_train = X_full.iloc[is_train]
        X_val = X_full.iloc[~is_train]
        y_train = y_full[is_train]
        y_val = y_full[~is_train]
        spw = max(1.0, float((y_train == 0).sum() / max(1, (y_train == 1).sum())))
        base_xgb = xgb.XGBClassifier(
            n_estimators=300, max_depth=5, learning_rate=0.05,
            scale_pos_weight=spw, eval_metric="logloss",
            n_jobs=-1, random_state=SEED, tree_method="hist",
        )
        cal = CalibratedClassifierCV(base_xgb, method="isotonic", cv=3)
        cal.fit(X_train.values, y_train)
        p_val = cal.predict_proba(X_val.values)[:, 1]

    frac_pos, mean_pred = calibration_curve(y_val, p_val, n_bins=10, strategy="quantile")
    metrics = {
        "auc": float(roc_auc_score(y_val, p_val)),
        "brier": float(brier_score_loss(y_val, p_val)),
        "confusion_matrix": confusion_matrix(y_val, (p_val >= 0.5).astype(int)).tolist(),
        "calibration_curve": [
            {"predicted": float(mp), "actual": float(fp)}
            for mp, fp in zip(mean_pred, frac_pos)
        ],
        "n_train": int(len(y_train)),
        "n_val": int(len(y_val)),
        "base_rate_train": float(y_train.mean()),
        "base_rate_val": float(y_val.mean()),
        "training_source": source,  # 'real' | 'synthetic' | 'synthetic_fallback'
    }

    shap_model = xgb.XGBClassifier(
        n_estimators=300, max_depth=5, learning_rate=0.05,
        scale_pos_weight=spw, eval_metric="logloss",
        n_jobs=-1, random_state=SEED, tree_method="hist",
    )
    shap_model.fit(X_train, y_train)
    explainer = shap.TreeExplainer(shap_model)

    return {
        "calibrated_model": cal,
        "shap_explainer": explainer,
        "shap_model": shap_model,
        "feature_names": train_cols,
        "comuna_rate": comuna_rate,
        "customers": customers,
        "metrics": metrics,
        "today": today,
        "hist_size": int(len(hist)),
    }


# =============================================================================
# DIA DE HOY + INFERENCIA
# =============================================================================
def _load_real_plan(today_date: date) -> Optional[pd.DataFrame]:
    """Carga visitas REALES de fpoc_simpli_visits para today_date.

    Mapea el schema de BD al schema que espera apply_status_and_predict. Si
    no hay filas para esa fecha, devuelve None (caller cae al sintético).

    Diseño:
      - lat/lon se generan deterministically desde region+id (no hay geocoding
        del Excel real). Suficiente para mapa visual + ML features.
      - vehicle_id capeado a 1..12 via (patente_falsa-1)%12+1 (data vieja del
        seed tenia patentes 1..40; nuevos imports ya usan 1..12).
      - Los campos sintéticos (load, n_subordenes, _is_recurrent) se rellenan
        con valores randomizados pero deterministas por id.
    """
    import os
    if os.environ.get("DISABLE_REAL_PLAN", "false").lower() == "true":
        return None
    try:
        from core.db import get_conn
        from ml.synthetic_calibration import REGION_BBOXES, REGION_DEPOTS
    except Exception:  # noqa: BLE001
        return None

    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                """
                SELECT id, title, "order", address, current_eta_cl, status,
                       sla_hour_checkout_eta, patente_falsa, empresa_falsa,
                       driver_name, region, comuna
                FROM fpoc_simpli_visits
                WHERE planned_date = ?
                ORDER BY patente_falsa, "order"
                """,
                (today_date.isoformat(),),
            )
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        from loguru import logger
        logger.warning(f"[pipeline] _load_real_plan fallo lectura DB: {e}")
        return None

    if not rows:
        return None

    visits = []
    for r in rows:
        vid_db = int(r[7]) if r[7] is not None else 1
        vid = ((vid_db - 1) % N_VEHICLES) + 1  # cap a 1..12 para que el modelo lo conozca
        region = str(r[10]) if r[10] else "RM"
        if region not in REGION_BBOXES:
            region = "RM"
        bbox = REGION_BBOXES[region]
        depot_lat, depot_lon = REGION_DEPOTS.get(region, REGION_DEPOTS["RM"])
        # Determinista: misma visita siempre en misma lat/lon
        rng = np.random.default_rng(int(r[0]) & 0xFFFFFFFF)
        lat = float(rng.uniform(bbox[0], bbox[1]))
        lon = float(rng.uniform(bbox[2], bbox[3]))

        # Window end derivado del ETA reportado en BD (le sumamos ~1h de buffer)
        eta_str = str(r[4]) if r[4] else ""
        we_h = 18
        try:
            time_part = ""
            if "T" in eta_str:
                time_part = eta_str.split("T", 1)[1]
            elif " " in eta_str:
                time_part = eta_str.split(" ", 1)[1]
            if time_part:
                we_h = int(time_part[:2]) + 1
        except Exception:  # noqa: BLE001
            pass
        we_h = max(14, min(20, we_h))
        window_end_str = f"{we_h:02d}:00:00"
        # planned_arrival_time = window_end - buffer
        buffer_min = int(rng.uniform(45, 100))
        pa_dt = datetime.combine(today_date, dtime(we_h, 0)) - timedelta(minutes=buffer_min)

        # Status real de BD; si está completed se respeta, si pending lo dejamos
        status_db = (str(r[5]) or "pending").lower()
        if status_db not in ("pending", "completed", "failed"):
            status_db = "pending"

        visits.append({
            "id": f"V{r[0]}",
            "tracking_id": str(r[0]),
            "customer_id": f"C{int(r[0]) % 10000:04d}",
            "title": str(r[1] or "Cliente"),
            "address": str(r[3] or ""),
            "latitude": lat,
            "longitude": lon,
            "region": region,
            "comuna": str(r[11]) if r[11] else "Santiago",
            "_depot_lat": float(depot_lat),
            "_depot_lon": float(depot_lon),
            "load": float(round(rng.uniform(0.5, 25.0), 2)),
            "n_subordenes": 1,
            "window_start": "09:00:00",
            "window_end": window_end_str,
            "planned_arrival_time": pa_dt.strftime("%H:%M:%S"),
            "planned_date": today_date.isoformat(),
            "reference": str(r[0]),
            "_is_recurrent": False,
            "_in_problem_comuna": False,
            "vehicle_id": vid,
            "vehicle_name": f"FAL-{1000 + vid - 1}",
            "order": int(r[2]) if r[2] is not None else 1,
        })

    df = pd.DataFrame(visits)
    rng = np.random.default_rng(SEED)
    return _compute_eta_and_failure(df, rng)


def gen_today_plan(today_date: date, day_seed: int, customers: list[dict]) -> pd.DataFrame:
    """Genera el plan de hoy. Prioridad: BD real → sintético fallback.

    Si fpoc_simpli_visits tiene visitas para today_date las usamos (con todos
    los campos derivados para que el ML pueda predecir). Si no hay data en BD,
    cae al generador sintético histórico.
    """
    real = _load_real_plan(today_date)
    if real is not None and len(real) > 0:
        from loguru import logger
        logger.info(f"[pipeline] today_plan desde BD real: {len(real)} visitas para {today_date}")
        return real
    return gen_day_visits(N_HISTORICAL_DAYS + day_seed, today_date, customers)


def apply_status_and_predict(
    today_df: pd.DataFrame, sim_clock: datetime,
    extra_incidents: dict[int, float], cal_model,
    explainer, feature_names: list[str],
    comuna_rate: dict[str, float],
) -> tuple[pd.DataFrame, np.ndarray]:
    df = today_df.copy()

    if extra_incidents:
        for v_id, extra_min in extra_incidents.items():
            mask = (df["vehicle_id"] == int(v_id)) & (df["planned_arrival_dt"] >= pd.Timestamp(sim_clock))
            df.loc[mask, "delay_min"] = df.loc[mask, "delay_min"] + float(extra_min)
        df["eta_real"] = df["planned_arrival_dt"] + pd.to_timedelta(df["delay_min"], unit="min")

    sim_ts = pd.Timestamp(sim_clock)
    is_completed = df["eta_real"] <= sim_ts

    obs_delay = {}
    for v_id, vdf in df.groupby("vehicle_id"):
        vdf_sorted = vdf.sort_values("order")
        comp = vdf_sorted[vdf_sorted["eta_real"] <= sim_ts]
        obs_delay[int(v_id)] = float(comp.iloc[-1]["delay_min"]) if not comp.empty else 0.0
    df["_obs_delay"] = df["vehicle_id"].astype(int).map(obs_delay)

    pending_proj = df["planned_arrival_dt"] + pd.to_timedelta(df["_obs_delay"], unit="min")
    df["current_eta"] = df["eta_real"].where(is_completed, pending_proj)
    df["slack_min"] = (df["window_end_dt"] - df["current_eta"]).dt.total_seconds() / 60.0
    df["status"] = np.where(is_completed, "completed", "pending")
    df["estimated_time_arrival"] = df["current_eta"].dt.strftime("%H:%M:%S")
    df["alert_slack"] = np.where(df["slack_min"] > 20, "GREEN",
                                  np.where(df["slack_min"] > 0, "YELLOW", "RED"))

    X = featurize(df, comuna_failure_rate=comuna_rate, now_clock=sim_clock)
    if "failed" in X.columns:
        X = X.drop(columns=["failed"])
    X = align_columns(X, feature_names)

    df = df.sort_values(["planned_date", "vehicle_id", "order"]).reset_index(drop=True)
    df["p_fallo"] = cal_model.predict_proba(X.values)[:, 1]

    shap_vals = explainer.shap_values(X)
    if isinstance(shap_vals, list):
        shap_vals = shap_vals[1]
    shap_vals = np.asarray(shap_vals)

    df["_shap_idx"] = np.arange(len(df))
    df["horas_hasta_we"] = X["horas_hasta_window_end"].values
    df["alert_valuedata"] = compute_alert_mask(df)
    return df, shap_vals


def compute_alert_mask(
    df: pd.DataFrame,
    *,
    threshold: float | None = None,
    eta_window_hours: float | None = None,
) -> pd.Series:
    """Reglas para `alert_valuedata`. Lee `app_config` en runtime si no se
    pasan parámetros explícitos. Permite overrides per-request (ej. preview en UI).

    Requiere que `df` tenga: p_fallo, horas_hasta_we, status.
    """
    if threshold is None or eta_window_hours is None:
        try:
            from core.app_config import get_alert_threshold, get_eta_window_hours
            if threshold is None:
                threshold = get_alert_threshold()
            if eta_window_hours is None:
                eta_window_hours = get_eta_window_hours()
        except Exception:  # noqa: BLE001
            # Fallback a constantes si app_config falla (ej. DB caída en tests).
            if threshold is None:
                threshold = ALERT_THRESHOLD
            if eta_window_hours is None:
                eta_window_hours = ANTICIPATION_HOURS
    return (
        (df["p_fallo"] >= threshold)
        & (df["horas_hasta_we"] >= eta_window_hours)
        & (df["status"] == "pending")
    )


def top_shap_factors(shap_vals: np.ndarray, feature_names: list[str], idx: int,
                     k: int = 3, only_positive: bool = True) -> list[tuple[str, float]]:
    contribs = shap_vals[idx]
    pairs = list(zip(feature_names, contribs))
    pairs.sort(key=lambda x: x[1], reverse=True)
    if only_positive:
        pairs = [p for p in pairs if p[1] > 0]
    return [(n, float(v)) for n, v in pairs[:k]]


def humanize_feature(name: str) -> str:
    if name.startswith("comuna_"):
        return f"Zona {name.replace('comuna_', '')}"
    if name.startswith("drv_v"):
        try:
            vid = int(name.replace("drv_v", ""))
            return f"Conductor FAL-{1000 + vid - 1}"
        except ValueError:
            return name
    if name.startswith("dow_"):
        dias = {"0": "Lun", "1": "Mar", "2": "Mié", "3": "Jue",
                "4": "Vie", "5": "Sáb", "6": "Dom"}
        return f"Día: {dias.get(name.replace('dow_', ''), name)}"
    return {
        "hora_window_end": "Hora límite ventana",
        "carga": "Carga (m³/kg)",
        "dist_depot_km": "Distancia al depot (km)",
        "orden_en_ruta": "Posición en ruta",
        "retraso_acumulado_vehiculo": "Retraso acumulado del vehículo",
        "tasa_fallo_historica_cliente": "Tasa histórica de fallo de la zona",
        "horas_hasta_window_end": "Horas hasta deadline",
    }.get(name, name)
