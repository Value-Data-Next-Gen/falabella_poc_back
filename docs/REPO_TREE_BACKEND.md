# Backend POC Falabella — Repo Tree

Mapa narrado de `backend/` con estado de cada archivo. Pensado para que un
nuevo dev (o el dueño del proyecto) pueda saltar de carpeta a carpeta y
entender qué hace cada cosa + qué se puede borrar sin miedo.

Convenciones:
- `activo`     → en producción, referenciado desde `main.py` o tests vivos.
- `cron`       → registrado en `main.lifespan` como APScheduler job.
- `migración`  → corrido por `fpoc_loader/migrations.py` al lifespan.
- `histórico`  → script one-shot ya aplicado; conservar para nuevos deploys.
- `script`     → CLI manual (no se invoca desde el app); ejecutado a mano.
- `deprecated` → no se usa, candidato a borrar.
- ⚠️ marca archivos dudosos cuya remoción requiere validar con el owner.

> **Nota:** `STATE.md` (autogenerado por `scripts/cartograph.py`) está
> levemente desactualizado — no incluye `admin_invitations`, `admin_day_notifications`,
> ni los endpoints `activation-link` de `mantenedores_users.py` /
> `mantenedores_drivers.py` / `empresa_contactos.py`. Re-correr cartographer
> después de este cleanup.

---

## Top-level (`backend/`)

| Archivo | Qué hace | Estado |
|---------|----------|--------|
| `main.py` | FastAPI app, lifespan, scheduler (sim tick + VIP deadline + live-gen + comment-sim + driver-sim), `include_router` de 33 routers. | activo |
| `startup.sh` | Bash que Azure App Service corre al boot (instala deps, uvicorn). | activo |
| `requirements.txt` | Deps pip pineadas a versión mínima. 21 paquetes. | activo |
| `openapi.json` | Schema autogenerado por `scripts/dump_openapi.py`. Versionado en disco — contrato con `frontend/src/types/api.ts`. | activo |
| `README.md` | Quick start local + Azure App Service + ETL Azure SQL. | activo |
| `valuedata.db` | SQLite local del POC (fallback cuando no hay Azure SQL). | activo (runtime) |
| `core/valuedata.db` | DB alterna que aparece cuando se corre desde `core/`. Idéntica a la de arriba pero generada en otro cwd. ⚠️ Probablemente accidental — verificar y borrar si sobra. | ⚠️ |
| `uvicorn.log` | Log de uvicorn en disco. Runtime artifact, no commitear. | activo (runtime) |

---

## `core/` — fundamentos compartidos

| Archivo | Qué hace | Estado |
|---------|----------|--------|
| `core/__init__.py` | Marker de paquete. | activo |
| `core/auth.py` | JWT login (`POST /api/auth/login`), `current_user` dependency, `require_admin`, `is_falabella`, `access_log`. Fail-fast si `JWT_SECRET` vacío. Define `empresas_router` (`GET /api/empresas`). | activo |
| `core/db.py` | `get_conn()` context manager. Soporta pyodbc (Azure SQL) y sqlite3 con rewriter `fpoc.X` → `fpoc_X` para SQLite. Función `backend()` discrimina ambos. | activo |
| `core/state.py` | Singleton `STATE`: snapshot del simulador, masters, drivers, vehicles, today_plan. Entrena modelo XGBoost al `STATE.init()`. Tick avanza sim_clock. | activo |
| `core/schemas.py` | Pydantic schemas compartidos entre routers legacy (`StateResponse`, `KPIs`, `Visit`, `ShapFactor`, `VisitExplanation`, `Driver`, `VehicleExtended`, `ClientMaster`, `StreamEvent`, `EmpresaSummary`, etc). | activo |
| `core/cache.py` | Decorator `@ttl_cached(ttl_seconds=N)` thread-safe minimalista. Usado para `/api/plan-diario` (~17s vs Azure SQL). | activo |
| `core/events.py` | Ring buffer in-memory de eventos del stream (`delivery`, `alert_triggered`, `red_simpli`, `comment_alert`, etc.). | activo |
| `core/app_config.py` | `fpoc_app_config` runtime config (`eta_window_hours`, `alert_threshold`). | activo |
| `core/storage.py` | Abstracción de storage para binarios (driver/empresa/vehicle docs). Azure Blob si `AZURE_STORAGE_CONNECTION_STRING` set; sino filesystem local. | activo |
| `core/twilio_templates.py` | Content SIDs centralizados (`vd_alerta_motivo`, `vd_invitacion`, etc.) con fallback hardcoded. | activo |
| `core/activation.py` | `gen_activation_token` + `build_activation_link` (wa.me) para flujo CR-014. | activo |

---

## `routers/` — endpoints FastAPI (33 módulos)

### Auth + sistema legacy

| Archivo | Endpoints / qué hace | Estado |
|---------|---------------------|--------|
| `routers/__init__.py` | Marker de paquete. | activo |
| `legacy_routes.py` | 4 sub-routers (`system`/`control`/`model`/`fleet`) con 23 endpoints históricos: `/api/health`, `/api/state`, `/api/control/clock`, `/api/model/metrics`, `/api/fleet/vehicles`, `/api/events/stream`, `/api/control/incident`, etc. Algunos no se usan del frontend (`get_kpis`, `get_visits`, `get_anticipated_alerts`, `get_explanation`, `get_app_config/update_app_config`, `get_vehicles`, `get_drivers/get_driver`, `get_clients/get_client`, `post_reset/post_freeze/post_clock/post_start_day`). ⚠️ Conservar hasta auditoría manual — algunos pueden estar siendo usados por curl/cron/sim externos. | activo (parcialmente usado) ⚠️ |

### Mantenedores (CRUD admin)

| Archivo | Endpoints / qué hace | Estado |
|---------|---------------------|--------|
| `mantenedores.py` | `/api/admin/{drivers,vehicles,dotacion-diaria}/template|upload` (bulk XLSX). `/api/admin/whatsapp/invite`. | activo |
| `mantenedores_shared.py` | Helpers compartidos por mantenedores: `require_fleet_access`, `enforce_fleet_empresa`, `can_access_empresa`, `refresh_state_maestros`. | activo |
| `mantenedores_empresas.py` | CRUD `/api/admin/empresas`. | activo |
| `mantenedores_users.py` | CRUD `/api/admin/users` + `reset-password` + `activation-link`. | activo |
| `mantenedores_drivers.py` | CRUD `/api/admin/drivers` + `activation-link`. | activo |
| `mantenedores_vehicles.py` | CRUD `/api/admin/vehicles`. | activo |
| `mantenedores_clients.py` | CRUD `/api/admin/clients` paginado. | activo |
| `mantenedores_doctypes.py` | CRUD `/api/admin/document-types` (catálogo). | activo |
| `mantenedores_documents_driver.py` | `/api/admin/drivers/{id}/documents` (list/upload/download/delete). | activo |
| `mantenedores_documents_entity.py` | Idem para `empresas/{id}/documents` y `vehicles/{id}/documents`. | activo |
| `mantenedores_dotacion.py` | `/api/admin/dotacion-diaria` (list/upsert por fecha+empresa). | activo |
| `mantenedores_capacitaciones.py` | `/api/admin/capacitacion-modulos` (catálogo) + `/api/admin/drivers/{id}/capacitaciones` (registros con validate/unvalidate). | activo |

### Operación día a día

| Archivo | Endpoints / qué hace | Estado |
|---------|---------------------|--------|
| `day_planning.py` | `/api/planificacion/clientes-del-dia`, `/api/planificacion/client-day-notes`, `/api/planificacion/day-config` (GET/PUT). | activo |
| `day_state.py` | FSM día operativo: `BORRADOR → VALIDADO → EN_CURSO → CERRADO`. Endpoints `/api/planificacion/day-state` (GET/transition/reset/regenerate/clean-and-regenerate/extend). Toca scheduler de `driver_sim` y `comment_simulator`. | activo |
| `drivers_whatsapp.py` | Gran router monolítico (1400+ líneas) que cubre: opt-in driver (`PUT /api/mantenedores/drivers/{id}`), scorecard, `import-mock/import-xlsx`, `start-day`, `day-status/day-clients/day-prep/calendar`, `dotacion-check`. ⚠️ Considerar split en CR futuro. | activo |
| `plan_diario.py` | `/api/plan-diario` — endpoint pesado (~17s cache miss). Devuelve unión de `PlanDiarioResponseNew` (real) y `PlanDiarioResponseLegacy` (sintético). Usa `core.cache`. | activo |
| `rutas.py` | `/api/planificacion/ruta?ruta_id=`, `/api/planificacion/integridad-rutas`, `/api/operacion/folios`. ⚠️ `integridad-rutas` no aparece consumido desde frontend — verificar si es solo para QA manual. | activo (`integridad-rutas` ⚠️) |
| `watchlist.py` | `/api/watchlist` — visitas en riesgo con notificaciones inline. | activo |
| `seguimiento.py` | Dashboards analítica fpoc: `/api/seguimiento/{available-dates, kpis, sla-distribution, motivos, by-empresa, by-localidad, rutas-anomalas, visits}`. | activo |
| `centros_distribucion.py` | `GET /api/centros-distribucion?region=`. Para el mapa. | activo |

### Mensajería / alertas / WhatsApp

| Archivo | Endpoints / qué hace | Estado |
|---------|---------------------|--------|
| `notifications.py` | `/api/notifications/{whatsapp, test, log, by-trackings, config, toggle}`. ⚠️ `/api/notifications/toggle` no consumido por frontend — probablemente herramienta interna. | activo (`toggle` ⚠️) |
| `twilio_inbound.py` | Webhook Twilio WhatsApp: `POST /api/v1/webhooks/twilio/{status,inbound}` + legacy alias `/whatsapp`. Contiene `_dispatch` (FSM + comandos sueltos) que delega al LLM si no matchea. | activo |
| `whatsapp_onboarding.py` | `POST /api/whatsapp/onboard` (alta manual), `GET /onboard/sandbox-info` (consumido), `GET /onboard/list`. ⚠️ `POST /onboard` y `GET /onboard/list` solo documentados en `docs/ONBOARDING.md` (curl). | activo (`onboard` POST/list ⚠️) |
| `whatsapp_escalation.py` | `POST /api/whatsapp/escalate-supervisor` (CR-013) con cooldown + supervisor lookup. | activo |
| `admin_invitations.py` | `GET /api/admin/invitations` (CR-014) — vista agregada users+drivers+contactos con estado `pending/activated/no_link`. | activo |
| `admin_day_notifications.py` | `POST /api/admin/notify-day-start` (broadcast inicio jornada) y `POST /api/admin/notify-eta-breach` (alerta atraso 1 visita). ⚠️ No consumidos desde frontend — manual triggers para QA / demo. Conservar. | activo (uso manual) ⚠️ |
| `empresa_contactos.py` | CRUD contactos de empresa transportista + bulk CSV + test-broadcast + audience-broadcast. | activo |
| `agent_web.py` | `/api/agent/web/{message, reset, state}` — chat web del agente IA (usa misma sesión que WhatsApp). | activo |
| `motivo_classifier.py` | `POST /api/motivos/classify`, alias `/api/llm/clasificar-motivo`, `GET /system-prompt`. Wrapper del LLM clasificador. | activo |
| `motivo_corrections.py` | `/api/motivo-corrections` (list + accept/reject/no-action/renotify-driver). | activo |
| `comments.py` | `/api/motivos`, `/api/motivos/alert-config` (catálogo + config alertable), `/api/visits/{id}/comment` (add/list), `/api/comments/recent`. | activo |
| `copiloto.py` | `POST /api/copiloto/decisions` (CR-013) — persiste feedback del operador sobre sugerencias del copiloto. | activo |

### Otros

| Archivo | Endpoints / qué hace | Estado |
|---------|---------------------|--------|
| `me.py` | Driver self-service: `/api/me/{profile, orders, documents, capacitaciones}`. | activo |
| `preferences.py` | `GET/PUT /api/me/preferences` (UI prefs). | activo |
| `priorities.py` | `/api/priorities` (list/set/clear overrides). | activo |
| `vip.py` | CRUD `/api/vip-clients` + `parse-notes` (LLM). | activo |
| `search.py` | `GET /api/search?q=` (buscador global topbar). | activo |
| `seed_admin.py` | `POST /api/admin/seed/region-day` y `GET /regions-supported`. ⚠️ No consumido por frontend — herramienta admin para seedear fechas con data sintética. | activo (uso manual) ⚠️ |

---

## `sims/` — simuladores en proceso

| Archivo | Qué hace / cuándo se invoca | Estado |
|---------|-----------------------------|--------|
| `sims/__init__.py` | Marker de paquete. | activo |
| `sims/_visits_db.py` | Helpers SQL sobre `fpoc.simpli_visits` para el bot/LLM (`kpis_today`, `visits_for_vehicle_today`, `drivers_summary_today_by_empresa`). | activo |
| `sims/live_generator.py` | Inserta visitas random en `fpoc.simpli_visits` cada N segundos. Endpoints `/api/live-gen/{stats,toggle,reset,batch,simulate-days}`. Registrado como cron en `main.lifespan`. | cron + activo |
| `sims/driver_sim.py` | Simulador de movimiento de drivers + entregas. Endpoint `/api/operacion/driver-positions`. Gateado por `start_sim()` desde `day_state.transition`. | cron + activo |
| `sims/comment_simulator.py` | Genera comentarios alertables periódicos. Endpoints `/api/comment-sim/{stats,toggle,config,emit-now}`. | cron + activo |
| `sims/vip_deadline_cron.py` | Cron 60s: alerta VIPs cuando se acerca el deadline. Registrado vía `register_cron(scheduler)` en `main.lifespan`. | cron + activo |
| `sims/whatsapp_agent.py` | FSM legacy del agente WhatsApp (menú interactivo). Fallback cuando el LLM falla o no aplica. Contiene `Session` (persistencia DB), `handle()`, `_render_route`, `_legacy_fsm_dispatch`. | activo |
| `sims/llm_agent.py` | Agente conversacional con Azure OpenAI gpt-4o-mini + 7 tools. Invocado por `whatsapp_agent.handle()` cuando el FSM no matchea. | activo |

---

## `ml/` — pipeline predicción

| Archivo | Qué hace | Estado |
|---------|----------|--------|
| `ml/__init__.py` | Marker de paquete. | activo |
| `ml/pipeline.py` | Generador sintético `gen_today_plan`, `gen_day_visits`, `gen_customer_pool`, entrenamiento XGB, SHAP explainer, `compute_alert_mask`. | activo |
| `ml/masters.py` | `build_client_master`, `gen_drivers`, `gen_vehicles_extended`. Construye maestros desde snapshot. | activo |
| `ml/synthetic_calibration.py` | Pesos por región + bboxes + depots + `daily_volume_factor` + `sample_subordenes`. Calibrado contra `client/data/Visitas 2025.xlsx`. | activo |

---

## `fpoc_loader/` — DDL + ETL + migraciones

### Runtime (registradas en `migrations.py`)

| Archivo | Qué hace | Estado |
|---------|----------|--------|
| `fpoc_loader/migrations.py` | Registry idempotente con tracking en `fpoc.schema_migrations`. Lista 25 migraciones ordenadas. Invocado desde `main.lifespan`. | activo |
| `fpoc_loader/bootstrap.py` | Migración 001 — bootstrap SQLite (DDL + seed users + datos). | activo |
| `fpoc_loader/migrate_dotacion_diaria.py` | 002 | migración |
| `fpoc_loader/migrate_driver_documents.py` | 003 | migración |
| `fpoc_loader/migrate_capacitaciones.py` | 004 | migración |
| `fpoc_loader/migrate_driver_role.py` | 005 | migración |
| `fpoc_loader/migrate_empresa_central.py` | 006 | migración |
| `fpoc_loader/migrate_document_types.py` | 007 | migración |
| `fpoc_loader/migrate_entity_documents.py` | 008 | migración |
| `fpoc_loader/migrate_cap_validation.py` | 009 | migración |
| `fpoc_loader/migrate_foreign_keys.py` | 010 | migración |
| `fpoc_loader/migrate_simpli_pascal_rename.py` | 011 | migración |
| `fpoc_loader/migrate_empresa_contactos_rol_check.py` | 012 | migración |
| `fpoc_loader/migrate_client_day_notes.py` | 013 | migración |
| `fpoc_loader/migrate_day_config.py` | 014 | migración |
| `fpoc_loader/migrate_day_state.py` | 015 | migración |
| `fpoc_loader/migrate_day_state_r3.py` | 016 | migración |
| `fpoc_loader/migrate_split_multi_region_routes.py` | 017 | migración |
| `fpoc_loader/migrate_driver_positions.py` | 018 | migración |
| `fpoc_loader/migrate_drivers_whatsapp.py` | 019 — sqlite-only (no-op en SQL Server). | migración |
| `fpoc_loader/migrate_empresa_contactos.py` | 020 — sqlite-only. | migración |
| `fpoc_loader/migrate_motivo_corrections.py` | 021 — sqlite-only. | migración |
| `fpoc_loader/migrate_vip_deadline.py` | 022 — sqlite-only. | migración |
| `fpoc_loader/migrate_alert_dispatch.py` | 023 — bifurcada (sqlite + sqlserver). | migración |
| `fpoc_loader/migrate_copiloto_decisions.py` | 024 — bifurcada. | migración |
| `fpoc_loader/migrate_activation_tokens.py` | 025 — wa.me activation tokens (CR-014). | migración |

### Loaders / seeds activos (importados o ejecutados a mano)

| Archivo | Qué hace | Estado |
|---------|----------|--------|
| `fpoc_loader/load_to_azure.py` | Carga `datos_eta_YYYY-MM-DD.xlsx` a Azure SQL. Idempotente por fecha. **También importado** desde `drivers_whatsapp.py` para constantes `SIMPLI_COLS`/`GEO_COLS`. | activo (import + script) |
| `fpoc_loader/seed_regiones_estacionalidad.py` | Backfill regiones + Black Friday + Cyber Week + ruta_id. **También importado** desde `routers/seed_admin.py`. | activo (import + script) |
| `fpoc_loader/seed_sqlite.py` | Seed SQLite desde Excel. **Importado** por `bootstrap.py::_load_excel_via_seed_sqlite` (migración 001). | activo (import) |

### SQL DDL

| Archivo | Qué hace | Estado |
|---------|----------|--------|
| `fpoc_loader/ddl.sql` | DDL base de Azure SQL (fpoc.simpli_visits, fpoc.geo_suborders). Cargado por `load_to_azure.py`. | activo |
| `fpoc_loader/sqlite_schema.sql` | DDL equivalente para SQLite (con prefijos `fpoc_` en vez de schema). Cargado por `bootstrap.py` y `seed_sqlite.py`. | activo |

---

## `scripts/` — utilidades CLI

| Archivo | Qué hace / cuándo se invoca | Estado |
|---------|------------------------------|--------|
| `scripts/dump_openapi.py` | Regenera `backend/openapi.json` desde `app.openapi()`. Correr al cerrar un CR con cambios al contrato. | script |
| `scripts/seed_centros_distribucion.py` | Crea `fpoc.centros_distribucion` + seed por región. Idempotente. Usa `core.db`. | script (one-shot) |
| `scripts/load_motivos_descripciones.py` | Carga descripciones de motivos del XLSX `Motivo no entrega HD.xlsx` a `fpoc.motivo_alert_config`. Usa `core.db`. | script (one-shot) |
| `scripts/smoke_wa_agent.py` | Smoke test del FSM/LLM del agente WhatsApp via `_dispatch`. | script |

---

## `tests/` — pytest (smoke contra backend levantado)

> **Nota:** Estos tests asumen un backend corriendo en `http://127.0.0.1:8001`
> (override con `TEST_BASE_URL`). No mockean; no levantan app. Si el backend
> no responde, todos fallan con error claro.

| Archivo | Qué cubre | Estado |
|---------|-----------|--------|
| `tests/__init__.py` | Marker de paquete. | activo |
| `tests/conftest.py` | Fixture `get` con JWT + helper `_wait_backend_ready`. | activo |
| `tests/test_smoke_endpoints.py` | `/api/health`, `/api/state`, `/api/auth/me`, `/api/centros-distribucion`. | activo |
| `tests/test_auth_admin_required.py` | Verifica que endpoints admin rechacen 401/403 sin token. | activo |
| `tests/test_state_machine.py` | FSM día operativo (`BORRADOR→VALIDADO→EN_CURSO→CERRADO`). | activo |
| `tests/test_driver_sim_dedup.py` | Deduplicación de visits del driver_sim. | activo |
| `tests/test_live_generator_order.py` | Orden de inserts del live-gen. | activo |
| `tests/test_copiloto_decisions.py` | `POST /api/copiloto/decisions` (CR-013). | activo |
| `tests/test_whatsapp_escalation.py` | `POST /api/whatsapp/escalate-supervisor` con cooldown (CR-013). | activo |

---

## `docs/` (sub-folder backend)

| Archivo | Qué hace | Estado |
|---------|----------|--------|
| `docs/ONBOARDING.md` | Guía de onboarding del usuario, 1:1 con el tour del frontend. | activo |

---

## Sin borrar (flagged ⚠️)

| Archivo / endpoint | Por qué se queda | Acción sugerida |
|---|---|---|
| `legacy_routes.py::get_kpis/get_visits/get_anticipated_alerts/get_explanation/get_vehicles/get_drivers/get_driver/get_clients/get_client/get_app_config/update_app_config/post_reset/post_freeze/post_clock/post_start_day/get_fleet_vehicle` | Endpoints históricos sin consumer en `frontend/src/`. Probable uso por curl/cron/QA. | Auditar manualmente; si nadie los toca en 1 mes, planear un CR de remoción con migration path. |
| `routers/rutas.py::integridad-rutas` | Sin consumer en frontend. Probable QA tool. | Idem. |
| `routers/notifications.py::toggle` | Sin consumer en frontend. | Probable admin manual; mantener. |
| `routers/admin_day_notifications.py::notify-day-start/notify-eta-breach` | Sin consumer frontend — manual triggers QA/demo. | Mantener; documentar en HANDOVER. |
| `routers/seed_admin.py::region-day/regions-supported` | Sin consumer frontend — admin manual de seeding. | Mantener. |
| `routers/whatsapp_onboarding.py::POST onboard / GET onboard/list` | Solo documentados en `backend/docs/ONBOARDING.md` (curl). | Mantener; usados manualmente. |
| `backend/core/valuedata.db` | Duplicado de `backend/valuedata.db` — aparece cuando se corre el script desde `backend/core/`. | Verificar y borrar si sobra. |
| `routers/drivers_whatsapp.py` (1400+ líneas) | Bien que funcione, pero monolítico. | Planear split en CR futuro (no en este). |

---

## Legacy (no importado por código vivo)

Archivos movidos a `backend/_legacy/` en este pase de cleanup. Conservados con
`git mv`-style (sin git acá, plain `mv`) bajo `__init__.py` vacíos para que
sigan siendo importables si hubiera que rescatarlos. **No re-importar desde
código vivo sin moverlos primero de vuelta.**

### `backend/_legacy/fpoc_loader/`

| Archivo | Razón de mover |
|---------|----------------|
| `apply_migration.py` | CLI standalone que aplica un `.sql` Azure-style con separador `GO`. Cero imports desde código vivo. Reemplazado por `fpoc_loader/migrations.py` con tracking idempotente. |
| `bootstrap_azure_schema.py` | One-shot que creó las tablas faltantes en Azure SQL en setup inicial. Cero imports. Ya aplicado en el ambiente productivo. |
| `fix_vip_columns_azure.py` | One-shot que agregó columnas `deadline_*` a `fpoc.vip_clients` en Azure SQL. Cero imports. Reemplazado por la migración 022 (`migrate_vip_deadline`). |
| `migrate_sqlite_to_azure.py` | CLI one-shot para migrar datos del SQLite local a Azure SQL durante onboarding del ambiente. Cero imports. Ya aplicado. |
| `seed_history.py` | Backfill demo de N días históricos en `fpoc.simpli_visits`. Self-contained (Azure-only `get_conn` interno), cero imports desde código vivo. |
| `seed_users.py` | Seed inicial admin + ops + transport_managers en Azure SQL. Cero imports desde código vivo; `bootstrap.py` tiene su propio `_seed_users_minimal` inline. Fallback inseguro a `admin123`. |
| `users_ddl.sql` | DDL `fpoc.users`/`fpoc.empresas_transporte`. Solo referenciado desde `seed_users.py` (también legacy) y `bootstrap_azure_schema.py` (legacy). Schema vivo en SQLite por `sqlite_schema.sql`. |
| `notifications_ddl.sql` | DDL `fpoc.notifications_log`/`fpoc.whatsapp_sessions`. Solo referenciado desde docstrings de scripts legacy. |
| `access_log_ddl.sql` | DDL `fpoc.access_log`. Solo referenciado desde docstrings de scripts legacy. |
| `content_templates_ddl.sql` | DDL `fpoc.content_templates`. Solo referenciado desde docstrings de scripts legacy. |

### `backend/_legacy/scripts/`

| Archivo | Razón de mover |
|---------|----------------|
| `cleanup_data_round7.py` | One-shot R7 (drivers con typo `DVR-*`). **Import roto** (`from db import get_conn`) — no corre as-is. Ya aplicado en R7. |
| `cleanup_stale_en_curso.py` | One-shot para cerrar días `EN_CURSO` huérfanos. **Import roto** (`from db import get_conn`). |
| `cleanup_synthetic_data.py` | Limpieza data sintética post-load. **Import roto** (`from db import get_conn`). Si se vuelve a necesitar, reescribir con `from core.db`. |
| `enrich_simpli_visits.py` | Backfill `ruta_id`/`region`/`comuna`. **Import roto** (`from db import get_conn`). Superado por las migraciones 017/018 que ya enriquecen al cargar. |
| `truncate_visit_data.py` | Truncate destructivo de visitas. **Import roto** (`from db import get_conn`). Útil pero no funcional as-is — reescribir si se necesita. |

## Conteo por categoría (post-cleanup actual)

| Categoría | Archivos | Detalle |
|-----------|----------|---------|
| Top-level `backend/` | 6 + 2 runtime | main.py, startup.sh, requirements.txt, openapi.json, README.md + valuedata.db, uvicorn.log |
| `core/` | 10 .py + 1 db | (`__init__`, activation, app_config, auth, cache, db, events, schemas, state, storage, twilio_templates) |
| `routers/` | 35 .py | Todos referenciados desde `main.py` o entre sí |
| `sims/` | 8 .py | Todos referenciados |
| `ml/` | 4 .py | (`__init__`, pipeline, masters, synthetic_calibration) |
| `fpoc_loader/` | 28 .py + 2 .sql | 25 migraciones + `migrations.py` + `bootstrap.py` + `seed_sqlite.py` + `load_to_azure.py` + `seed_regiones_estacionalidad.py` + `ddl.sql` + `sqlite_schema.sql` |
| `scripts/` | 4 .py | `dump_openapi.py`, `seed_centros_distribucion.py`, `load_motivos_descripciones.py`, `smoke_wa_agent.py` |
| `tests/` | 9 .py | incl. `__init__` y `conftest` |
| `docs/` (backend) | 1 .md | `ONBOARDING.md` |
| `_legacy/fpoc_loader/` | 6 .py + 4 .sql | One-shot scripts y DDL fragmentado, todos sin imports desde código vivo |
| `_legacy/scripts/` | 5 .py | Scripts CLI con `from db import` rotos (apuntan a un módulo inexistente) |

Smoke test post-cleanup: `from main import app; len(app.routes) == 197`.
