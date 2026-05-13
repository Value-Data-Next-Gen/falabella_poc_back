"""Calibración del generador sintético basada en datos reales del cliente.

Fuentes:
  - client/data/Visitas 2025.xlsx     → 363 días, distribución por DOW y mes
  - client/data/Subordenes 2025.xlsx  → ratio sub/visita = 1.26
  - fpoc_simpli_visits (post-seed)    → split RM 83% / regiones 17%

Expone:
  - REGION_WEIGHTS:    dict region → peso (suman 1.0)
  - REGION_BBOXES:     dict region → (lat_min, lat_max, lon_min, lon_max)
  - REGION_DEPOTS:     dict region → (lat, lon) del CD que la cubre
  - REGION_COMUNAS:    dict region → list[str] comunas representativas
  - dow_volume_factor(weekday)  → multiplicador 0..1.x según día semana
  - month_volume_factor(month)  → multiplicador 0.8..1.7 según estacionalidad
  - SUBORDER_RATIO     → 1.26 (suborders por visita)
"""
from __future__ import annotations

import random
from typing import Optional


# =============================================================================
# DISTRIBUCIÓN GEOGRÁFICA (basada en distribución real de visitas en el Excel)
# =============================================================================
# Pesos: aproximación de fpoc_simpli_visits.region counts (RM 83% / regiones 17%)
REGION_WEIGHTS: dict[str, float] = {
    "RM":          0.833,
    "Valparaíso":  0.032,
    "Coquimbo":    0.032,
    "Biobío":      0.031,
    "Araucanía":   0.016,
    "Maule":       0.025,
    "O'Higgins":   0.016,
    "Antofagasta": 0.015,
}

# Bounding boxes (lat_min, lat_max, lon_min, lon_max). Aproximación a la
# zona urbana principal de cada región. Suficiente para generar lat/lon en
# rangos realistas para mapas y filtros de region.
REGION_BBOXES: dict[str, tuple[float, float, float, float]] = {
    "RM":          (-33.65, -33.30, -70.85, -70.50),  # Santiago metro
    "Valparaíso":  (-33.10, -32.95, -71.65, -71.50),  # Valpo + Viña
    "Coquimbo":    (-30.05, -29.85, -71.40, -71.20),  # La Serena + Coquimbo
    "Biobío":      (-36.90, -36.70, -73.15, -72.95),  # Concepción + Talcahuano
    "Araucanía":   (-38.80, -38.65, -72.65, -72.50),  # Temuco
    "Maule":       (-35.50, -35.30, -71.75, -71.55),  # Talca / Curicó
    "O'Higgins":   (-34.25, -34.05, -70.85, -70.65),  # Rancagua
    "Antofagasta": (-23.75, -23.55, -70.50, -70.30),  # Antofagasta
}

# CDs por región (lat, lon). Para el cómputo de dist_depot_km usamos el CD
# regional, no el de Santiago. Esto evita distancias 400km que romperían el
# entrenamiento del modelo.
REGION_DEPOTS: dict[str, tuple[float, float]] = {
    "RM":          (-33.45, -70.66),  # Santiago (CD OMNICANAL LOF2)
    "Valparaíso":  (-33.04, -71.62),  # CD CENTRO
    "O'Higgins":   (-34.17, -70.74),
    "Maule":       (-35.43, -71.65),
    "Biobío":      (-36.83, -73.05),  # CD SUR
    "Araucanía":   (-38.74, -72.59),
    "Coquimbo":    (-29.95, -71.34),  # CD NORTE
    "Antofagasta": (-23.65, -70.40),
}

# Comunas representativas por región. Para RM usamos lista corta; para regiones
# pocas pero realistas. Se usa para etiquetar visitas (campo comuna).
REGION_COMUNAS: dict[str, list[str]] = {
    "RM": [
        "Santiago", "Las Condes", "Providencia", "Maipú", "Puente Alto",
        "La Florida", "Ñuñoa", "San Bernardo", "Quilicura", "Pudahuel",
        "Recoleta", "Independencia", "Estación Central", "La Cisterna",
        "La Pintana", "El Bosque", "San Miguel", "Macul", "Peñalolén",
        "Vitacura", "Lo Barnechea", "Renca", "Conchalí",
    ],
    "Valparaíso":  ["Valparaíso", "Viña del Mar", "Quilpué", "Villa Alemana"],
    "Coquimbo":    ["La Serena", "Coquimbo", "Ovalle"],
    "Biobío":      ["Concepción", "Talcahuano", "Chiguayante", "San Pedro de la Paz"],
    "Araucanía":   ["Temuco", "Padre Las Casas"],
    "Maule":       ["Talca", "Curicó", "Linares"],
    "O'Higgins":   ["Rancagua", "Machalí"],
    "Antofagasta": ["Antofagasta", "Calama"],
}


# =============================================================================
# DISTRIBUCIÓN TEMPORAL (basada en Visitas 2025.xlsx, 363 días, lun-sáb)
# =============================================================================
# Multiplicadores de volumen por día de semana, normalizados a Wed=1.0.
# Calculados de los promedios reales del Excel (ver client/data/Visitas 2025.xlsx).
DOW_VOLUME_FACTOR: dict[int, float] = {
    0: 0.72,  # Monday    (22122 / 30708)
    1: 0.99,  # Tuesday   (30501 / 30708)
    2: 1.00,  # Wednesday baseline
    3: 0.92,  # Thursday  (28275 / 30708)
    4: 0.86,  # Friday    (26464 / 30708)
    5: 0.74,  # Saturday  (22825 / 30708)
    6: 0.11,  # Sunday    (3358 / 30708) — operación residual
}

# Multiplicadores estacionales por mes (jun/oct/dic = peaks de Cyber/BlackFriday/Navidad).
MONTH_VOLUME_FACTOR: dict[int, float] = {
    1: 0.95, 2: 0.85, 3: 0.95, 4: 1.05, 5: 0.92, 6: 1.70,
    7: 1.00, 8: 1.05, 9: 0.90, 10: 1.55, 11: 0.95, 12: 1.75,
}


# =============================================================================
# SUBÓRDENES
# =============================================================================
# Ratio promedio suborders/visita en el Excel real (lun-sáb): 1.26
SUBORDER_RATIO: float = 1.26


# =============================================================================
# Helpers
# =============================================================================
def dow_volume_factor(weekday: int) -> float:
    """weekday: 0=Mon..6=Sun (datetime.date.weekday())."""
    return DOW_VOLUME_FACTOR.get(int(weekday), 1.0)


def month_volume_factor(month: int) -> float:
    return MONTH_VOLUME_FACTOR.get(int(month), 1.0)


def daily_volume_factor(d) -> float:
    """Multiplicador combinado DOW × mes para una fecha."""
    return dow_volume_factor(d.weekday()) * month_volume_factor(d.month)


def pick_region(rng) -> str:
    """Elige una región según REGION_WEIGHTS. Usa rng (np.random.Generator)."""
    regs = list(REGION_WEIGHTS.keys())
    weights = [REGION_WEIGHTS[r] for r in regs]
    total = sum(weights)
    weights = [w / total for w in weights]
    return str(rng.choice(regs, p=weights))


def gen_latlon_for_region(region: str, rng) -> tuple[float, float]:
    """Genera lat/lon aleatoria dentro del bbox de la región."""
    bbox = REGION_BBOXES.get(region) or REGION_BBOXES["RM"]
    lat = float(rng.uniform(bbox[0], bbox[1]))
    lon = float(rng.uniform(bbox[2], bbox[3]))
    return lat, lon


def pick_comuna(region: str, rng) -> str:
    comunas = REGION_COMUNAS.get(region) or REGION_COMUNAS["RM"]
    return str(rng.choice(comunas))


def depot_for_region(region: str) -> tuple[float, float]:
    return REGION_DEPOTS.get(region, REGION_DEPOTS["RM"])


def sample_subordenes(rng) -> int:
    """Cantidad de subórdenes por visita. Distribución pseudo-Poisson centrada
    en SUBORDER_RATIO (1.26). Mínimo 1, p95 ~3."""
    val = int(rng.poisson(lam=SUBORDER_RATIO - 1.0)) + 1
    return max(1, min(val, 6))
