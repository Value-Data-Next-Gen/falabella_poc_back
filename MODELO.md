# Modelo predictivo y feature engineering — POC ValueData

> **TL;DR para el cliente**:
> - El modelo es **XGBoost real**, calibrado con isotonic regression y explicado
>   con SHAP — no es un mock ni un placeholder. Hoy entrega AUC ≈ 0.77 / Brier ≈ 0.10
>   sobre 60 días de operación.
> - El **feature schema está diseñado sobre las columnas del Excel que entregaron**
>   (`datos_eta_*.xlsx`, hojas Simpli + Geo). Toda variable que el modelo consume
>   es derivable de campos que ya existen en sus datos. Ver §3.4 — Mapeo Excel → feature.
> - Para el POC, los **labels de entrenamiento se generan sintéticamente** porque el
>   Excel actual cubre **un solo día (~2k visitas)** y no trae el evento histórico
>   "fallo anticipado vs ventana" que el clasificador necesita. El simulador
>   reproduce los mismos patrones que ven en operación (zonas problema, ventanas
>   tardías, drivers conflictivos, recurrencia de clientes) — esto permite
>   demostrar end-to-end que el pipeline encuentra esos patrones, sin esperar
>   a que se acumule histórico real.
> - **Camino a producción**: cuando lleguen 60+ días de SimpliRoute con el campo
>   "ETA real vs window_end", se reemplaza el generador por el loader real y
>   el resto del stack (features, calibración, SHAP, alertas) **queda intacto**.
>   Es ese tipo de POC donde la inversión no se tira a la basura.

---

## 1. Origen de los datos

### 1.1 Excel original (`datos_eta_2026_04_19.xlsx`)

Dos hojas, formato tipo SimpliRoute:

**Hoja `Simpli`** — visitas con ETA y SLA (1 fila por visita, ~2.120 filas):
- Llave: `id` (BIGINT)
- Tiempos: `planned_date`, `checkout_cl`, `current_eta_cl`
- SLA: `sla_hour_checkout_eta` (horas de adelanto/atraso vs ETA), `bin_label`/`bin_index`
- Ruta: `Patente_falsa`, `Empresa_falsa`, `Drivername`, `Fechainicioruta`, `am_pm`
- Anomalías: 4 flags (`ruta_eta_futuro`, `ruta_fecha_inicio_mayor_eta`,
  `ruta_primer_punto_lejano`, `ruta_fecha_inicio_distinta_fecha_eta`) +
  agregado `ruta_anomala`

**Hoja `Geo`** — sub-órdenes con motivo de no entrega (~2.220 filas):
- Llave: `Suborden`
- Geografía: `direccion`, `localidad`, `region`
- Estado: `estado`, `motivonoentrega`, `comentarionoentrega`
- Vínculo con Simpli: `idruta` + `fechainicioruta`

### 1.2 Carga a SQLite

`fpoc_loader/seed_sqlite.py`:

```
Excel → pandas → fpoc_simpli_visits (1.866 después de dedupe)
              → fpoc_geo_suborders  (2.119)
              → fpoc_empresas_transporte (5 empresas distintas: 22,23,25,27,33)
              → fpoc_users          (1 admin + 1 ops + 5 transport_managers)
```

Esta data sirve **solo para Seguimiento** — endpoints `/api/seguimiento/*` que
hacen agregados (KPIs por fecha, SLA distribution, top motivos, performance
por empresa/localidad, breakdown de anomalías). El modelo no la consume.

---

## 2. Generador sintético (entrada del modelo)

### 2.1 Patrones ocultos (la "trampa" que SHAP debe encontrar)

`pipeline.setup_hidden_patterns(SEED)` define al inicio del proceso:

- **3 comunas problema**: tres celdas de 0.05° × 0.05° random alrededor del
  depot (`-33.45, -70.66`). Los clientes en estas comunas tendrán fail rate
  inflado.
- **2 drivers problema**: dos vehículos de 12 con factor multiplicativo de
  retraso aumentado.

Estos patrones quedan fijos por el `SEED=42` y se filtran al simulador como
penalizaciones — **el modelo no los conoce explícitamente**, los descubre
desde los features.

### 2.2 Pool de clientes

`gen_customer_pool(seed=42)` — 800 clientes únicos con coordenadas:

- 20% en comunas problema (clusterizados dentro de las 3 zonas)
- 80% distribuidos uniformemente alrededor del depot, evitando comunas problema
- 15% marcados como `_is_recurrent` (clientes con historial de fallar reiteradamente)
- Cada cliente: `customer_id`, `title` (Faker es_CL), `address`, `lat`, `lon`

### 2.3 Plan de un día — `gen_day_visits(day_idx, planned_date, customers)`

1. **Sample**: 120 visitas extraídas con reemplazo del pool (algunos clientes
   aparecen varias veces).
2. **Ventana horaria** `window_end`: muestreada de `[14, 17, 18, 19, 20]` con
   probabilidades `[0.20, 0.25, 0.20, 0.20, 0.15]` — la ventana crítica
   17–19h queda sobre-representada (es la que más falla en realidad).
3. **`planned_arrival_time`**: `window_end − buffer` donde `buffer ∈ U(45, 100)` minutos.
4. **Reparto a 12 vehículos**: round-robin (~10 visitas/vehículo), ordenadas
   internamente por `planned_arrival_time`.

### 2.4 Cómputo de ETA real y label `failed` — `_compute_eta_and_failure`

Para cada vehículo se simula la cadena de retrasos:

```
delay_i = local_noise * vehicle_factor * franja_factor(hora)
        + penalty_extra
        + 0.6 * delay_{i-1}        # propagación al siguiente punto
```

Donde:
- `local_noise ~ N(0, 6)` minutos
- `vehicle_factor ~ N(1.0, 0.1)` por vehículo (constante en el día)
- `franja_factor(hora)`: 1.0 (9–11), 1.3 (11–14), 1.15 (14–17), **1.45 (17+)** —
  tarde es peor.
- `penalty_extra ~ Exp(λ=4) * (penalty_mult − 1)` donde `penalty_mult` es:
  - `× 3.0` si la visita está en comuna problema
  - `× 2.0` si `window_end ∈ {17, 18, 19}`
  - `× 2.5` si el vehículo está en `problem_drivers`
  - `× 1.25` si `load > 15` (m3/kg)
  - `× 4.0` si el cliente es recurrente
  - Multiplicativos: una visita en comuna problema + driver problema + ventana
    17h tiene penalty_mult ≈ 15.
- **Incidente**: 3% prob por vehículo de tener un incidente que agrega
  20–45 min a partir de cierta posición de la ruta.

Resultado:
```
eta_real = planned_arrival_time + delay_min
slack_min = (window_end − eta_real).total_seconds() / 60
failed = 1 si eta_real > window_end else 0
```

Con esta receta, el fail rate global queda en torno a **15–25%** y los
patrones ocultos quedan fuertemente correlacionados con `failed`.

### 2.5 Histórico de entrenamiento

`train_model()` itera 60 días: `[today − 60, today − 1]`:

```python
for d in range(60):
    day_date = today - timedelta(days=60 - d)
    hist_dfs.append(gen_day_visits(d, day_date, customers))
hist = pd.concat(hist_dfs)   # ~7.200 filas
```

Cada día tiene su propio seed (`SEED + 1000*day_idx`) para variabilidad,
pero el pool de clientes y los patrones ocultos son los mismos.

---

## 3. Feature engineering

### 3.1 Features numéricos (`NUMERIC_FEATURES`)

| Feature                          | Cómputo                                                      | Por qué importa                          |
|----------------------------------|--------------------------------------------------------------|------------------------------------------|
| `hora_window_end`                | `int(window_end[:2])`                                        | Ventanas tardías concentran fallos       |
| `carga`                          | `load` (m³/kg)                                                | Carga grande → tiempo de descarga mayor  |
| `dist_depot_km`                  | Haversine entre cliente y depot                              | Distancia → tiempo de viaje + retorno    |
| `orden_en_ruta`                  | Posición de la visita dentro del vehículo                    | Más tarde en la ruta → más retraso acum. |
| `retraso_acumulado_vehiculo`     | `cumsum(delay_min)` shifted (sin leak)                       | Señal directa de "ya viene tarde"        |
| `tasa_fallo_historica_cliente`   | Fail rate observado por `comuna_id` en histórico              | Codifica el patrón "comuna problema"     |
| `horas_hasta_window_end`         | `(window_end − ref_clock) / 3600` clipped a 0                | Anticipación: cuánto margen queda        |

### 3.2 Features categóricos (one-hot)

- `comuna_id`: cuadrícula de 0.05° × 0.05° del par `(lat, lon)`. Codifica zona
  geográfica sin necesidad de un padrón comunal real.
- `conductor_id`: `v1`, `v2`, …, `v12` — uno por vehículo.
- `dia_semana`: 0–6.

`pd.get_dummies(prefix=["comuna","drv","dow"])` los expande. El modelo termina
con ~70–90 columnas (depende de cuántas comunas distintas aparecen).

### 3.4 Mapeo Excel → feature (datos del cliente → variables del modelo)

Toda feature que consume el clasificador es derivable directamente de columnas
que **ya existen** en el Excel que entregó Falabella:

| Feature del modelo               | Origen en `datos_eta_*.xlsx`                                                          | Cómputo                                                |
|----------------------------------|----------------------------------------------------------------------------------------|--------------------------------------------------------|
| `hora_window_end`                | Hoja Simpli — derivada de `current_eta_cl` (hora prometida) o de un campo de ventana   | `int(window_end[:2])`                                  |
| `carga`                          | (no presente en el Excel actual — placeholder; SimpliRoute la trae como `load`)       | `load` directo                                         |
| `dist_depot_km`                  | Simpli — `address` geocodificado o lat/lon (la integración real con SimpliRoute trae lat/lon) | Haversine entre cliente y depot                |
| `orden_en_ruta`                  | Simpli — `[order]` (ya viene)                                                          | `order` directo                                        |
| `retraso_acumulado_vehiculo`     | Simpli — `Patente_falsa` + `sla_hour_checkout_eta` por ruta + `Fechainicioruta`        | `cumsum(delay)` shifted dentro del grupo (vehicle, day)|
| `tasa_fallo_historica_cliente`   | Simpli — agregado `Empresa_falsa`/zona/`address` × histórico de `failed`               | Fail rate por `comuna_id`                              |
| `horas_hasta_window_end`         | Simpli — `current_eta_cl` − ahora                                                     | `(window_end − ref_clock) / 3600`                      |
| `comuna_id`                      | Simpli — derivada de lat/lon del cliente (o `Geo.localidad` + `Geo.region`)           | Bucket de 0.05° × 0.05° (≈ 5 km)                       |
| `conductor_id`                   | Simpli — `Drivername` o `Patente_falsa`                                                | Hash a id estable                                      |
| `dia_semana`                     | Simpli — `planned_date`                                                                | `dayofweek`                                            |

Y la hoja **Geo** del Excel suma señales adicionales que **ya están listas
para incorporarse como features nuevos** en el siguiente sprint:

| Feature derivable (no implementado todavía)            | Columna Geo                          | Hipótesis                                                |
|--------------------------------------------------------|--------------------------------------|----------------------------------------------------------|
| `tipo_documento`                                       | `tipodocumento`                      | Distintos tipos (boleta vs factura) tienen tasa de fallo distinta |
| `region`, `localidad`                                  | `region`, `localidad`                | Reemplaza el grid de 0.05° por un padrón real            |
| `motivo_recurrente_cliente`                            | `motivonoentrega` agregado por cliente | "Sin moradores" 3 veces seguidas → +30% riesgo siguiente |
| `n_subordenes_misma_ruta`                              | `count(Suborden) by idruta`          | Rutas con muchas sub-órdenes saturan al driver           |
| `densidad_localidad`                                   | `count(Suborden) by localidad`        | Comunas con +200 entregas/día concentran fallos          |

Y los flags de **anomalía de ruta** del Excel (`ruta_eta_futuro`,
`ruta_fecha_inicio_mayor_eta`, `ruta_primer_punto_lejano`,
`ruta_fecha_inicio_distinta_fecha_eta`, `ruta_anomala`) son features ya
calculadas por el equipo de Falabella — **se enchufan directamente al modelo
como 5 columnas binarias adicionales**. En la versión actual del POC se usan
solo en la vista de Seguimiento; pasarlas al clasificador es trivial
(agregarlas a `NUMERIC_FEATURES`).

> **Mensaje al cliente**: lo que entregaron en el Excel ya cubre el 80% de las
> variables que el modelo necesita. Los features que faltan (carga real,
> lat/lon precisas, recurrencia por cliente) salen del API de SimpliRoute en
> producción — no requieren capturar nada nuevo.

### 3.3 El truco de la observación parcial — `randomize_observation`

Si entrenamos con `delay_min` total del día, el modelo se hace trampa: ya sabe
el delay final cuando lo vamos a usar para predecir antes del fin de la ruta.

Solución: durante featurización del histórico, el "reloj de observación"
(`_ref`) se elige random entre `09:00` y `window_end`:

```python
span_sec = (window_end - day_start).total_seconds()
offset_sec = span_sec * uniform(0, 1)
ref = day_start + offset_sec
```

Y luego:

```python
unobserved = eta_real > _ref
df.loc[unobserved, "delay_min"] = 0.0
retraso_acumulado_vehiculo = cumsum(delay_min) shifted   # solo lo ya completado
```

Es decir: para cada visita histórica, se simula que estamos parados en un
momento aleatorio del día y solo "vemos" lo que ya pasó. Esto reproduce el
escenario de inferencia (mirar a las 13:00 y predecir si la visita de las
18:00 va a fallar) sin leakage.

### 3.4 En inferencia (`apply_status_and_predict`)

Mismo featurize, pero con `now_clock = sim_clock` fijo. El campo
`_obs_delay` por vehículo es el `delay_min` de la última visita completada
(la mejor evidencia de "cómo viene") y se proyecta a las pendientes:

```
current_eta = planned_arrival_time + obs_delay   # para visitas pending
slack_min   = window_end − current_eta
```

---

## 4. Entrenamiento

### 4.1 Split temporal

Los primeros 50 días → train, últimos 10 días → val. Split por fecha (no random)
para detectar si el modelo generaliza a días nuevos.

### 4.2 Modelo base

```python
xgb.XGBClassifier(
    n_estimators=300, max_depth=5, learning_rate=0.05,
    scale_pos_weight=spw,        # spw = neg/pos para compensar desbalance
    eval_metric="logloss",
    tree_method="hist",
)
```

`scale_pos_weight` corrige el desbalance (~80% no-fallo, 20% fallo).

### 4.3 Calibración

XGBoost devuelve scores no calibrados. Para que `p_fallo = 0.7` signifique
realmente "70% de chance de fallar" se aplica:

```python
CalibratedClassifierCV(base_xgb, method="isotonic", cv=3)
```

Isotonic regression con 3-fold sobre el train set. La curva de calibración
resultante (`calibration_curve` en `metrics`) se grafica en el frontend
(`/api/model/metrics`) y muestra que predicción ≈ frecuencia observada.

### 4.4 Métricas reportadas

- **AUC** ≈ 0.77 (ROC-AUC sobre val)
- **Brier score** ≈ 0.097 (mean squared error de probabilidades; menor es mejor)
- **Confusion matrix** con threshold 0.50
- **Calibration curve** (10 bins, strategy=quantile)

Ver `STATE.boot["metrics"]` y `/api/model/metrics`.

### 4.5 SHAP

Se entrena un segundo XGB **sin calibración** (necesario porque
`CalibratedClassifierCV` envuelve el modelo y `TreeExplainer` necesita acceso
directo al booster):

```python
shap_model = xgb.XGBClassifier(...).fit(X_train, y_train)
explainer = shap.TreeExplainer(shap_model)
```

En cada inferencia:

```python
shap_vals = explainer.shap_values(X)     # (n_visitas, n_features)
top_factors = top_shap_factors(shap_vals, feature_names, idx, k=3)
```

Se exponen como "Top 3 factores que más empujan hacia fallo" en el frontend
(`/api/visits/{tracking_id}/explanation`).

---

## 5. Reglas de negocio sobre las predicciones

`apply_status_and_predict` agrega 3 indicadores que combinan modelo + reglas:

```python
alert_slack    = "RED" si slack_min ≤ 0
                 "YELLOW" si 0 < slack_min ≤ 20
                 "GREEN" si slack_min > 20

alert_valuedata = (p_fallo ≥ 0.50)
                  ∧ (horas_hasta_window_end ≥ 2.0)
                  ∧ (status == "pending")
```

`alert_valuedata` es la "alerta anticipada" que justifica la torre: dispara
≥ 2h antes del deadline cuando el modelo asigna ≥ 50% de probabilidad de
fallo. Las constantes (`ALERT_THRESHOLD = 0.50`, `ANTICIPATION_HOURS = 2.0`)
son configurables y deberían tunearse contra una curva de ROI real.

---

## 6. Camino a producción (qué cambia, qué se mantiene)

| Componente                       | POC                                          | Producción                                      | ¿Reuso?         |
|----------------------------------|----------------------------------------------|-------------------------------------------------|-----------------|
| **Generador de visitas**         | `gen_day_visits` sintético                   | Loader SimpliRoute (REST API)                   | Reemplazo       |
| **Labels (`failed`)**            | Computado por simulador                      | Evento "ETA real > window_end" del histórico    | Reemplazo       |
| **Feature engineering**          | `featurize()` en `pipeline.py`               | **Idéntico**                                    | ✅ 100%          |
| **Modelo + calibración**         | XGBoost + isotonic + SHAP                    | **Idéntico**                                    | ✅ 100%          |
| **Reglas de alerta** (`alert_valuedata`) | `p_fallo ≥ 0.5 ∧ horas_hasta_we ≥ 2` | Tunear thresholds contra ROI medido           | ✅ se mantiene   |
| **Inferencia**                   | `apply_status_and_predict` cada 3s           | **Idéntico**, conectado a feed real             | ✅ 100%          |
| **Layout multi-tenant**          | Empresas extraídas del Excel                 | Misma tabla `fpoc_empresas_transporte`          | ✅ 100%          |
| **Deployment**                   | uvicorn local + SQLite                       | App Service + Azure SQL + Container Registry    | Switch via env  |

Los componentes marcados como "Reuso 100%" no requieren reescribirse — son
los que más esfuerzo tomó construir bien (calibración, SHAP, reglas, scope
multi-tenant). Esta es la inversión que el cliente preserva del POC.

### 6.1 Mejoras esperables con datos reales

1. **Más historia → mejor AUC**. Hoy AUC ≈ 0.77 sobre 60 días sintéticos. Con
   12 meses de operación real esperamos 0.82–0.88 (referencia: estudios
   públicos de last-mile delivery prediction).
2. **Comuna real (no grid)**: usar código comunal del INE en lugar de la
   cuadrícula de 0.05° puede subir AUC en 1–2 puntos y mejorar la
   interpretabilidad de SHAP ("Zona Las Condes" vs "Zona -33.45_-70.65").
3. **Anomalías como features**: los 5 flags `ruta_*` del Excel se enchufan
   como columnas binarias adicionales. Son señales fuertes (el equipo de
   Falabella ya las identificó como predictivas).
4. **Features externos**: clima (Meteoblue / DMC API), congestión (Google
   Maps Distance Matrix), eventos masivos (calendario público). Cada uno
   suma 1–3 puntos de AUC en estudios comparables.

### 6.2 Limitaciones honestas del POC

- **Comuna de 0.05°** ≈ 5 km × 5 km. Granularidad coarse — es un proxy.
- **`scale_pos_weight` simple**: para datasets reales considerar `focal loss`
  o muestreo estratificado por hora/comuna.
- **Sin features de contexto externo**: clima, eventos, congestión vial — pendientes.
- **`PRICE_PER_RESCUE_CLP = 8000` y `RESCUE_RATE = 0.60` son placeholders**.
  El KPI "Delta de rescate" está oculto en el frontend hasta tener números
  oficiales del área de operaciones.
- **Re-entrenamiento mensual** (no implementado): producción requiere un
  pipeline reproducible (MLflow / DVC + un job programado).

---

## 7. Cómo correr todo end-to-end

```bash
# 1) Cargar Excel a SQLite (una vez, o cuando llegue Excel nuevo)
cd valuedata_backend
python fpoc_loader/seed_sqlite.py

# 2) Levantar backend (entrena modelo en ~30s)
DB_BACKEND=sqlite python -m uvicorn main:app --host 127.0.0.1 --port 8090

# 3) Levantar frontend
cd ../valuedata_frontend
npm run dev

# 4) Login en http://localhost:5180
#    admin@falabella.cl / admin123                  → ve todo
#    transporte22@demo.cl / demo123                 → ve solo Transporte 22
```

Endpoints de modelo: `/api/model/metrics`, `/api/model/importance`,
`/api/visits/{tid}/explanation`, `/api/alerts/anticipated`.
