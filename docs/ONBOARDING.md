# ValueData × Falabella — Guía de Onboarding

Guía paso a paso para conocer la plataforma. Coincide 1:1 con el tour interactivo
del frontend (botón **Tour** en el topbar) y se puede leer offline.

---

## Paso 1 · Bienvenida

ValueData × Falabella es una **torre de control logística** que combina:

- **Plan operacional real** importado desde SimpliRoute (4-5k visitas/día).
- **ML predictivo** (XGBoost + SHAP) que anticipa entregas en riesgo con
  ~2 horas de antelación.
- **Agente WhatsApp conversacional** para drivers, jefes y clientes con
  clasificación IA de motivos de no-entrega (Azure OpenAI).
- **Eventos en vivo** (incidentes, alertas, motivos reportados) con auto-notify
  configurable.

Tres roles principales:

| Rol | Login | Ve |
|---|---|---|
| `falabella_admin` | `admin@falabella.cl` | todo: KPIs globales, config, override prioridades |
| `falabella_ops` | `ops@falabella.cl` | operación + alertas |
| `transport_manager` | `transporteN@demo.cl` | solo SU empresa de transporte |

---

## Paso 2 · Operación (módulo principal)

**Mapa con ~5,000 puntos** distribuidos en 8 regiones (RM 81%, regiones 19%).
Cada punto es una visita real del Excel del cliente con predicción ML.

Filtros:

- **Por región**: RM / regiones / RM+regiones (combinado).
- **Por patente / vehículo** (multi-select).
- **Solo VIP** (clientes marcados).
- **Solo alertas** (visitas con `alert_valuedata=true`).

Codificación visual:

- 🔴 alta probabilidad (≥50%) — alerta crítica
- 🟡 amarillo (20-50%)
- 🟢 verde (<20%)

El reloj simulado avanza solo: 3 segundos reales = 3 minutos simulados. Pausalo
desde **Plan diario → Día actual → Congelar 09:00**.

---

## Paso 3 · Planificación (preparar el día)

Tres componentes:

### 3.1 Carga de entregas

Botón **Importar desde SimpliRoute** para una fecha específica. Genera ~250
visitas mock con drivers que matchean los del CRUD (Jessica, Manuel, etc) y
distribución regional realista.

- **Idempotente**: re-clic muestra "Ya cargaste el día X (N visitas)".
- **Histórico persistido**: el panel muestra todas las cargas anteriores con
  fecha + count + timestamp + usuario.
- **Re-importar (force)**: confirm destructivo si necesitás regenerar.

### 3.2 Configuración del día

Tres botones:

- **Nuevo plan** — regenera el plan del día actual con un nuevo seed (varía
  visitas + patrones, mantiene la fecha).
- **Congelar 09:00** — pausa el reloj y resetea sim_clock a 09:00. Útil para
  configurar prioridades sin que el día avance.
- **Iniciar día** — destrabe el reloj. Auto-advance ON.

### 3.3 Marcar VIP

Cliente VIP puede ser por:

- **title** (razón social): "Wilnoscar Zurita Carrero"
- **customer_id**: ID exacto
- **reference**: número de referencia

Las visitas que matchean disparan auto-notify aunque no crucen el umbral de
p_fallo, y se muestran con ★ en el dashboard.

---

## Paso 4 · Seguimiento IA

### 4.1 Catálogo de motivos

14 motivos en total:

- 11 del Excel del cliente (PROD NO ENTREGADO POR TIEMPO, SIN MORADORES,
  PROBLEMA DE DIRECCIÓN/SIN INFORMACIÓN, FUERA DE COBERTURA/FRECUENCIA,
  NO CONOCEN A CLIENTE, PRODUCTO CON PROBLEMAS, SINIESTRO EN CALLE,
  PRODUCTO ROBADO, CLIENTE RECHAZA, NO CUMPLE CONDICIONES RETIRO,
  PRODUCTO NO CARGADO).
- 3 internos (NO DESPACHA A LOCALIDAD, RIESGO FRAUDE, DETENCION URGENTE).

Cada motivo tiene `alertable` (bool) + `severity` (low/medium/high/critical) +
descripción para el LLM. Override por empresa posible.

### 4.2 Validador IA

`POST /api/motivos/classify` con `{ "comentario": "..." }` retorna
`{ motivo, confianza, razonamiento, fallback }`.

- LLM real (Azure OpenAI gpt-4o-mini) si hay creds; fallback a keywords si no.
- Catalog cerrado: el LLM solo elige entre los 14 motivos válidos.

### 4.3 Correcciones IA

Cuando un driver corrige una clasificación, se persiste en
`fpoc_motivo_corrections` con `status='accepted'|'rejected'|'corrected'`.
Sirve como dataset para mejorar el prompt.

---

## Paso 5 · Maestros

CRUD para:

- **Drivers** (`fpoc_drivers`): id, nombre, vehicle_id, phone, opt-in WhatsApp.
- **Vehículos** (`fpoc_vehicles`): patente, capacidad.
- **Clientes** (`fpoc_clients`).
- **Empresas de transporte** (`fpoc_empresas_transporte`).
- **Contactos por empresa** (`fpoc_empresa_contactos`): jefe, coordinador,
  driver, otro. Con filtros `severities_in`, `motivos_in`, `region_filter`.

Los onboardeados via WhatsApp (cuando un número nuevo manda "hola") aparecen
como contactos con `opted_in_at=NOW`.

---

## Paso 6 · WhatsApp Agent

### 6.1 Cómo se une alguien al sandbox

1. Manda `join <código>` al `+1 415 523 8886` desde su WhatsApp.
2. Twilio responde "You are all set!".
3. Manda `hola` al mismo número → el agente lo detecta.

El código del sandbox está en `console.twilio.com → Messaging → Try it out`.

### 6.2 Detección por phone (cascada)

El agente busca el número en orden:

1. **`fpoc_drivers`** → menú driver (ver mi ruta, próxima visita, reportar).
2. **`fpoc_users`** → menú manager (KPIs, alertas, drivers, scopeado a empresa).
3. **`fpoc_empresa_contactos`**:
   - rol `jefe` o `coordinador` → menú manager.
   - rol `driver` → menú driver con vehículo auto-asignado.
   - rol `otro` → menú genérico (rol elegible).
4. **Sin match** → auto-onboard como contacto en empresa default + welcome.

### 6.3 Comandos

| Comando | Efecto |
|---|---|
| `hola` / `menu` | Reset al menú principal (auto-detect rol) |
| `salir` | Cierra la sesión |
| `help` | Lista de comandos |
| `kpis` | Resumen del día (visitas, alertas) |
| `status TRK...` | Detalle de una visita |
| `motivo TRK <MOTIVO>: <comentario>` | Reporte rápido (power user) |
| `humano` | Escala a un coordinador |
| `gracias` / 👍 | Ack simple |
| `stop` | Opt-out compliance (apaga todas las notifs) |

### 6.4 Flujo IA del driver (recomendado)

```
DRIVER: hola
BOT:    Hola Manuel 👋 (FAL-1010)
        1️⃣ Ver mi ruta · 2️⃣ Próxima visita · 3️⃣ Reportar · ...

DRIVER: 3
BOT:    Decime el tracking_id (ej: TRK0600009) o '0' para cancelar:

DRIVER: TRK0610116
BOT:    Visita: Cárcamo Ltda - La Cisterna - 17:00 - 🔴 69%
        🤖 Contame qué pasó (con tus palabras)...

DRIVER: nadie atendió toqué timbre 3 veces
BOT:    🤖 Detecté: SIN MORADORES (confianza alta)
        Razón: "Cliente no atiende, sin moradores presentes."
        1️⃣ Sí, registrar · 2️⃣ Cambiar motivo · 0️⃣ Cancelar

DRIVER: 1
BOT:    ✅ Registrado. TRK0610116 → SIN MORADORES
        Tu coordinador fue notificado.
```

### 6.5 Onboarding rápido para demos

Para sumar gente nueva (vos como admin):

```bash
curl -X POST /api/whatsapp/onboard \
  -H "Authorization: Bearer $TOK" \
  -d '{"phone":"+5691234...","name":"Pedro Demo","kind":"driver","vehicle_id":5}'
```

`kind` puede ser `driver`, `manager` (con `empresa_id` y `role`), o `contact`
(con `empresa_id` y `rol`).

---

## Paso 7 · Analítica

Lectura directa de la BD real (`fpoc_simpli_visits`, ~160k visitas histórico):

- **KPIs por período**: visitas totales, completadas, fallidas, ratio
  sub/visita.
- **Distribución SLA**: histograma por bin de 0.5h.
- **Performance por driver**: scorecard con `comments_total`,
  `corrections_acceptance_rate`, `alerts_critical_30d`.
- **Splits regionales**: RM vs regiones por todo lo anterior.

Filtros en cada panel: rango de fechas, empresa, región.

---

## Paso 8 · Configuración admin

### 8.1 Runtime config

`GET/PUT /api/admin/config`:

- `eta_window_hours`: ventana de anticipación (default 2.0). Una visita es
  alerta si `horas_hasta_window_end >= eta_window_hours`.
- `alert_threshold`: umbral de p_fallo para activar alerta (default 0.5).

Cambios surten efecto **inmediatamente** sin reiniciar (snapshot se refresca).

### 8.2 Notificaciones

Tres flags en `.env`:

- `NOTIFICATIONS_ENABLED=true|false` — master switch.
- `NOTIFICATIONS_DRY_RUN=true|false` — log sin enviar (audit).
- `ENABLE_AUTO_NOTIFY=true|false` — auto-trigger cuando se dispara una alerta.

### 8.3 Twilio webhook

URL para registrar en Twilio Sandbox settings:

```
https://<tu-host>/api/twilio/inbound
```

(o la legacy `/api/v1/webhooks/twilio/whatsapp` que ya está aliased).

Validación de firma `X-Twilio-Signature` activa por default si
`TWILIO_AUTH_TOKEN` está seteado.

---

## Apéndice · Datos del modelo ML

- **Train**: intenta primero la BD real (24k visitas, label `status='failed'`,
  ~5% positivos). Si AUC val < 0.6, fallback a sintético (60 días generados,
  ~36k visitas con patrones espaciales discriminantes).
- **AUC actual**: 0.773.
- **Brier**: 0.094.
- **Predicción**: aplicada al snapshot de visitas reales del día.

Distribución típica de `p_fallo` para el snapshot real:

- max ≈ 0.78
- p99 ≈ 0.59
- mediana ≈ 0.12
- 196 visitas con `alert_valuedata=true` (threshold default 0.5)

---

## Apéndice · Tablas clave de la DB

| Tabla | Filas (típico) | Persistido |
|---|---|---|
| `fpoc_simpli_visits` | 160k+ | ETL Excel + import-mock |
| `fpoc_notifications_log` | ~300+ | inbound + outbound + status callbacks (filtrados) |
| `fpoc_visit_comments` | crece c/ uso | comentarios reportados (driver / IA) |
| `fpoc_motivo_corrections` | crece c/ IA | dataset de correcciones |
| `fpoc_empresa_contactos` | crece c/ onboard | contactos WA |
| `fpoc_whatsapp_sessions` | activas | FSM del agente, TTL 30min |
| `fpoc_planificacion_imports` | 1 por día | log idempotente de cargas |
| `fpoc_app_config` | 1-2 keys | runtime config (eta, threshold) |
| `fpoc_access_log` | crece | auditoría logins |

---

## Re-iniciar el tour

Si querés volver a ver el tour interactivo después de cerrarlo:

- En el frontend: botón **✨ Tour** del topbar.
- Para forzar reset de la flag: limpiar `localStorage.removeItem('fpoc.tour.completed.v1')` desde DevTools.

---

_Última actualización: 2026-05-08._
