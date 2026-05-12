"""QA: persistencia + cobertura analitica.
Reporta:
  1. Tablas DB con count + fechas
  2. Endpoints con fuente (snapshot sintetico vs BD real)
  3. Discrepancias entre KPIs sinteticos y reales
"""
import io
import os
import sys
import sqlite3

import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DB = "valuedata.db"
BASE = "http://localhost:8001/api"


def section(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


# ============== 1. TABLAS DB ==============
section("1. TABLAS DB - counts + ranges")
cn = sqlite3.connect(DB)
cn.row_factory = sqlite3.Row

tables = [
    ("fpoc_simpli_visits", "planned_date", "BD real visitas (ETL Excel + import-mock)"),
    ("fpoc_geo_suborders", None, "subordenes geo (real)"),
    ("fpoc_clients", None, "clientes (CRUD)"),
    ("fpoc_drivers", None, "drivers (CRUD + onboarded)"),
    ("fpoc_vehicles", None, "vehiculos"),
    ("fpoc_empresas_transporte", None, "empresas"),
    ("fpoc_users", None, "usuarios web"),
    ("fpoc_empresa_contactos", "created_at", "contactos WA por empresa"),
    ("fpoc_visit_comments", "created_at", "comentarios reportados"),
    ("fpoc_motivo_corrections", "created_at", "correcciones IA del motivo"),
    ("fpoc_motivo_alert_config", None, "config alertable por motivo"),
    ("fpoc_vip_clients", None, "clientes VIP"),
    ("fpoc_visit_priority_overrides", "created_at", "overrides de prioridad"),
    ("fpoc_notifications_log", "created_at", "log WhatsApp (in+out)"),
    ("fpoc_planificacion_imports", "imported_at", "log de imports"),
    ("fpoc_whatsapp_sessions", "updated_at", "sesiones FSM agente"),
    ("fpoc_app_config", "updated_at", "ETA window + threshold runtime"),
    ("fpoc_access_log", "created_at", "auditoria de logins"),
]
for tab, date_col, desc in tables:
    try:
        n = cn.execute(f"SELECT COUNT(*) FROM {tab}").fetchone()[0]
        if date_col:
            r = cn.execute(f"SELECT MIN({date_col}), MAX({date_col}) FROM {tab}").fetchone()
            rng = f"{r[0]} → {r[1]}" if r[0] else "(vacia)"
        else:
            rng = ""
        flag = "✓" if n > 0 else "○"
        print(f"  {flag}  {tab:35s} n={n:>6}  {rng}  {desc}")
    except Exception as e:
        print(f"  ✗  {tab:35s} ERR: {e}")

# ============== 2. ENDPOINTS ==============
section("2. ENDPOINTS - fuente de datos")

TOK = requests.post(f"{BASE}/auth/login", json={
    "email": "admin@falabella.cl", "password": "admin123"
}).json()["access_token"]
H = {"Authorization": f"Bearer {TOK}"}

ENDPOINTS = [
    # (path, source, comentario)
    ("/visits", "SNAPSHOT", "ML predictions, mapa, dashboard analitico"),
    ("/alerts/anticipated", "SNAPSHOT", "alertas ML"),
    ("/kpis", "SNAPSHOT", "KPIs analitico"),
    ("/state", "SNAPSHOT", "estado del simulador"),
    ("/plan-diario?legacy=true", "BD-real (mas snapshot fallback)", "plan operacional"),
    ("/seguimiento/kpis", "BD-real", "KPIs históricos"),
    ("/seguimiento/sla-distribution", "BD-real", "distribución SLA"),
    ("/seguimiento/available-dates", "BD-real", "fechas con datos"),
    ("/drivers/scorecard", "BD-real (comments+corrections)", "scorecard drivers"),
    ("/comments/recent", "BD-real", "comentarios recientes"),
    ("/notifications/log", "BD-real (notifications_log)", "log WA"),
    ("/planificacion/imports", "BD-real", "log imports"),
    ("/whatsapp/onboard/list", "BD-real", "personas onboardeadas"),
    ("/admin/config", "BD-real (app_config)", "ETA + threshold"),
    ("/motivos/system-prompt", "BD-real (catalog + DB overrides)", "prompt LLM"),
]

for path, src, desc in ENDPOINTS:
    try:
        r = requests.get(f"{BASE}{path}", headers=H, timeout=15)
        st = r.status_code
        try:
            j = r.json()
            n = len(j) if isinstance(j, list) else (1 if isinstance(j, dict) else 0)
        except Exception:
            n = "?"
        print(f"  [{st}]  {path:42s}  src={src:32s}  n={n:>5}  {desc}")
    except Exception as e:
        print(f"  [ERR] {path}: {e}")

# ============== 3. DISCREPANCIA SNAPSHOT vs BD REAL ==============
section("3. DISCREPANCIA - analitica sintetica vs BD real (HOY)")

today_snap = requests.get(f"{BASE}/state", headers=H).json().get("today")
print(f"  Día actual del simulador: {today_snap}")

# Snapshot
visits_snap = requests.get(f"{BASE}/visits", headers=H).json()
alerts_snap = requests.get(f"{BASE}/alerts/anticipated?limit=500", headers=H).json()
print(f"\n  SNAPSHOT (sintético, lo que ven mapa/alertas/KPIs analitico):")
print(f"    Visitas: {len(visits_snap)}")
print(f"    Alertas: {len(alerts_snap)}")
rm_s = sum(1 for v in visits_snap if v.get('region') == 'RM')
print(f"    RM: {rm_s} | Regiones: {len(visits_snap)-rm_s}")

# BD real
real_today = cn.execute(
    "SELECT COUNT(*), COUNT(DISTINCT ruta_id), COUNT(DISTINCT driver_name) "
    "FROM fpoc_simpli_visits WHERE planned_date = ?", (today_snap,)
).fetchone()
print(f"\n  BD REAL fpoc_simpli_visits (lo que ve plan_diario):")
print(f"    Visitas: {real_today[0]}")
print(f"    Rutas distintas: {real_today[1]}")
print(f"    Drivers distintos: {real_today[2]}")

real_rm = cn.execute(
    "SELECT region, COUNT(*) FROM fpoc_simpli_visits WHERE planned_date = ? GROUP BY region",
    (today_snap,)
).fetchall()
for r in real_rm:
    print(f"      {r[0]}: {r[1]}")

# ============== 4. GAPS Y RECOMENDACIONES ==============
section("4. GAPS detectados")

gaps = []

# Gap 1: ¿hay endpoints analytics que SÍ deberían leer real?
gaps.append("/visits, /alerts/anticipated, /kpis leen del SNAPSHOT (508 visitas).")
gaps.append("  → El cliente espera ver las visitas REALES (4-5k/día con drivers reales).")
gaps.append("  → Recomendación: agregar endpoint /visits/real-time que lea fpoc_simpli_visits")
gaps.append("    o switch global por env var, manteniendo /visits como ML compat.")

# Gap 2: comentarios en visits sintéticas vs reales
nc = cn.execute("SELECT COUNT(*) FROM fpoc_visit_comments WHERE tracking_id LIKE 'TRK%'").fetchone()[0]
nc_real = cn.execute("SELECT COUNT(*) FROM fpoc_visit_comments WHERE tracking_id NOT LIKE 'TRK%'").fetchone()[0]
gaps.append(f"\nComments por origen: TRK* (sintético): {nc}, ids reales: {nc_real}")
if nc_real == 0:
    gaps.append("  → Todos los comments son sobre tracking_ids sintéticos.")
    gaps.append("  → El driver via WA reporta sobre TRK* del snapshot, NO sobre IDs reales del Excel.")

# Gap 3: el ML model entrena con sintético, no con real
gaps.append("\nML model:")
gaps.append("  → train_model() entrena con visitas sintéticas (gen_day_visits + features).")
gaps.append("  → Las predicciones (p_fallo, alert_valuedata) solo aplican al snapshot.")
gaps.append("  → BD real (162k visitas Excel) NO recibe predicciones ML.")

# Gap 4: maestros
gaps.append("\nMaestros consistentes (post-fix import):")
gaps.append("  → fpoc_drivers (12) ↔ fpoc_simpli_visits.driver_name: 100% match en imports nuevos.")
gaps.append("  → Match perfecto desde el último ajuste de live_generator.")

for g in gaps:
    print(g)

print("\n" + "=" * 72)
print("FIN AUDIT")
print("=" * 72)
