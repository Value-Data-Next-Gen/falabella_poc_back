"""Crea las tablas que faltan en Azure SQL (schema fpoc) que no están en
ddl.sql. Idempotente — solo crea lo que no existe.

Uso:
    cd backend
    python fpoc_loader/bootstrap_azure_schema.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
WS_ROOT = BACKEND.parent
for _p in (BACKEND / ".env", WS_ROOT / ".env"):
    if _p.exists():
        load_dotenv(_p)
        break


def open_azure():
    import pyodbc
    server = os.environ["DB_SERVER"].replace("tcp:", "")
    cs = (
        f"DRIVER={{{os.environ.get('DB_DRIVER', 'ODBC Driver 17 for SQL Server')}}};"
        f"SERVER={server};DATABASE={os.environ['DB_NAME']};"
        f"UID={os.environ['DB_USER']};PWD={os.environ['DB_PASSWORD']};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )
    return pyodbc.connect(cs, autocommit=True)


# ------- DDL T-SQL para Azure SQL (idempotente) -------
# Cada bloque chequea OBJECT_ID antes de CREATE.

DDL_BLOCKS = [
    # drivers
    """
    IF OBJECT_ID('fpoc.drivers', 'U') IS NULL
    BEGIN
        CREATE TABLE fpoc.drivers (
            driver_id            NVARCHAR(20)    NOT NULL PRIMARY KEY,
            name                 NVARCHAR(200)   NOT NULL,
            phone                NVARCHAR(50)    NULL,
            license              NVARCHAR(50)    NULL,
            vehicle_id           INT             NULL,
            vehicle_name         NVARCHAR(50)    NULL,
            rating               DECIMAL(3, 2)   NULL,
            deliveries_30d       INT             NOT NULL DEFAULT 0,
            fail_rate_30d        DECIMAL(5, 3)   NOT NULL DEFAULT 0,
            joined_at            DATETIME2(0)    NULL,
            active               BIT             NOT NULL DEFAULT 1,
            is_problem_hidden    BIT             NOT NULL DEFAULT 0,
            phone_e164           NVARCHAR(20)    NULL,
            notify_whatsapp      BIT             NOT NULL DEFAULT 0,
            opted_in_at          DATETIME2(0)    NULL
        );
    END
    """,
    # vehicles
    """
    IF OBJECT_ID('fpoc.vehicles', 'U') IS NULL
    BEGIN
        CREATE TABLE fpoc.vehicles (
            vehicle_id           INT             NOT NULL PRIMARY KEY,
            name                 NVARCHAR(100)   NOT NULL,
            type                 NVARCHAR(50)    NULL,
            plate                NVARCHAR(20)    NULL,
            capacity_m3          INT             NULL,
            driver_id            NVARCHAR(20)    NULL,
            driver_name          NVARCHAR(200)   NULL,
            depot_lat            DECIMAL(9, 6)   NULL,
            depot_lon            DECIMAL(9, 6)   NULL,
            year                 INT             NULL,
            active               BIT             NOT NULL DEFAULT 1,
            is_problem_hidden    BIT             NOT NULL DEFAULT 0
        );
    END
    """,
    # clients
    """
    IF OBJECT_ID('fpoc.clients', 'U') IS NULL
    BEGIN
        CREATE TABLE fpoc.clients (
            customer_id          NVARCHAR(50)    NOT NULL PRIMARY KEY,
            title                NVARCHAR(200)   NOT NULL,
            address              NVARCHAR(500)   NOT NULL,
            latitude             DECIMAL(9, 6)   NOT NULL,
            longitude            DECIMAL(9, 6)   NOT NULL,
            is_recurrent         BIT             NOT NULL DEFAULT 0,
            in_problem_comuna    BIT             NOT NULL DEFAULT 0,
            notes                NVARCHAR(500)   NULL,
            updated_at           DATETIME2(0)    NOT NULL DEFAULT SYSDATETIME(),
            region               NVARCHAR(50)    NULL,
            comuna               NVARCHAR(100)   NULL
        );
    END
    """,
    # visit_comments
    """
    IF OBJECT_ID('fpoc.visit_comments', 'U') IS NULL
    BEGIN
        CREATE TABLE fpoc.visit_comments (
            comment_id           INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
            tracking_id          NVARCHAR(50)    NOT NULL,
            vehicle_id           INT             NULL,
            empresa_id           INT             NULL,
            motivo               NVARCHAR(80)    NOT NULL,
            comentario           NVARCHAR(MAX)   NOT NULL,
            created_by           INT             NULL,
            created_at           DATETIME2(0)    NOT NULL DEFAULT SYSDATETIME(),
            region               NVARCHAR(50)    NULL
        );
        CREATE INDEX IX_visit_comments_tracking ON fpoc.visit_comments (tracking_id);
    END
    """,
    # motivo_alert_config
    """
    IF OBJECT_ID('fpoc.motivo_alert_config', 'U') IS NULL
    BEGIN
        CREATE TABLE fpoc.motivo_alert_config (
            motivo               NVARCHAR(80)    NOT NULL,
            empresa_id           INT             NULL,
            alertable            BIT             NOT NULL,
            severity             NVARCHAR(20)    NOT NULL,
            description          NVARCHAR(MAX)   NULL,
            updated_at           DATETIME2(0)    NOT NULL DEFAULT SYSDATETIME(),
            updated_by           INT             NULL
        );
        CREATE UNIQUE INDEX UQ_motivo_alert_config ON fpoc.motivo_alert_config (motivo, empresa_id)
            WHERE empresa_id IS NOT NULL;
    END
    """,
    # motivo_corrections
    """
    IF OBJECT_ID('fpoc.motivo_corrections', 'U') IS NULL
    BEGIN
        CREATE TABLE fpoc.motivo_corrections (
            correction_id        INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
            comment_id           INT             NULL,
            tracking_id          NVARCHAR(50)    NOT NULL,
            motivo_reportado     NVARCHAR(80)    NOT NULL,
            motivo_sugerido      NVARCHAR(80)    NOT NULL,
            confianza            NVARCHAR(20)    NOT NULL,
            razonamiento         NVARCHAR(MAX)   NOT NULL,
            driver_id            NVARCHAR(20)    NULL,
            status               NVARCHAR(20)    NOT NULL DEFAULT 'pending',
            decided_by_user_id   INT             NULL,
            decided_at           DATETIME2(0)    NULL,
            notified_driver_at   DATETIME2(0)    NULL,
            created_at           DATETIME2(0)    NOT NULL DEFAULT SYSDATETIME(),
            region               NVARCHAR(50)    NULL
        );
    END
    """,
    # empresa_contactos
    """
    IF OBJECT_ID('fpoc.empresa_contactos', 'U') IS NULL
    BEGIN
        CREATE TABLE fpoc.empresa_contactos (
            contact_id           INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
            empresa_id           INT             NOT NULL,
            nombre               NVARCHAR(200)   NOT NULL,
            rol                  NVARCHAR(50)    NULL,
            phone_e164           NVARCHAR(20)    NULL,
            email                NVARCHAR(200)   NULL,
            severities_in        NVARCHAR(MAX)   NULL,
            motivos_in           NVARCHAR(MAX)   NULL,
            region_filter        NVARCHAR(20)    NULL,
            opted_in_at          DATETIME2(0)    NULL,
            active               BIT             NOT NULL DEFAULT 1,
            notes                NVARCHAR(500)   NULL,
            created_by_user_id   INT             NULL,
            created_at           DATETIME2(0)    NOT NULL DEFAULT SYSDATETIME(),
            updated_at           DATETIME2(0)    NOT NULL DEFAULT SYSDATETIME()
        );
    END
    """,
    # planificacion_imports
    """
    IF OBJECT_ID('fpoc.planificacion_imports', 'U') IS NULL
    BEGIN
        CREATE TABLE fpoc.planificacion_imports (
            fecha                DATE            NOT NULL PRIMARY KEY,
            count                INT             NOT NULL,
            imported_at          DATETIME2(0)    NOT NULL DEFAULT SYSDATETIME(),
            imported_by_user_id  INT             NULL
        );
    END
    """,
    # whatsapp_sessions
    """
    IF OBJECT_ID('fpoc.whatsapp_sessions', 'U') IS NULL
    BEGIN
        CREATE TABLE fpoc.whatsapp_sessions (
            phone_e164           NVARCHAR(20)    NOT NULL PRIMARY KEY,
            state                NVARCHAR(50)    NOT NULL DEFAULT 'idle',
            role                 NVARCHAR(20)    NULL,
            identified_id        NVARCHAR(50)    NULL,
            context              NVARCHAR(MAX)   NULL,
            updated_at           DATETIME2(0)    NOT NULL DEFAULT SYSDATETIME(),
            created_at           DATETIME2(0)    NOT NULL DEFAULT SYSDATETIME()
        );
    END
    """,
    # app_config
    """
    IF OBJECT_ID('fpoc.app_config', 'U') IS NULL
    BEGIN
        CREATE TABLE fpoc.app_config (
            [key]                NVARCHAR(100)   NOT NULL PRIMARY KEY,
            value                NVARCHAR(500)   NOT NULL,
            updated_at           DATETIME2(0)    NOT NULL DEFAULT SYSDATETIME(),
            updated_by_user_id   INT             NULL
        );
    END
    """,
]

# Cols que faltan en tablas que YA existen (drivers WA, motivo deadlines, etc).
ALTERS = [
    # users: phone_e164 + notify_whatsapp + thresholds (de notifications_ddl.sql)
    "IF COL_LENGTH('fpoc.users', 'phone_e164') IS NULL ALTER TABLE fpoc.users ADD phone_e164 NVARCHAR(20) NULL",
    "IF COL_LENGTH('fpoc.users', 'notify_whatsapp') IS NULL ALTER TABLE fpoc.users ADD notify_whatsapp BIT NOT NULL DEFAULT 0",
    "IF COL_LENGTH('fpoc.users', 'notify_pfallo_threshold') IS NULL ALTER TABLE fpoc.users ADD notify_pfallo_threshold DECIMAL(4,3) NOT NULL DEFAULT 0.5",
    "IF COL_LENGTH('fpoc.users', 'notify_slack_min_threshold') IS NULL ALTER TABLE fpoc.users ADD notify_slack_min_threshold INT NOT NULL DEFAULT 0",
    "IF COL_LENGTH('fpoc.users', 'notify_only_vip') IS NULL ALTER TABLE fpoc.users ADD notify_only_vip BIT NOT NULL DEFAULT 0",
    # notifications_log: direction + profile_name + media_urls + region + contact_id + driver_id + content_*
    "IF COL_LENGTH('fpoc.notifications_log', 'contact_id') IS NULL ALTER TABLE fpoc.notifications_log ADD contact_id INT NULL",
    "IF COL_LENGTH('fpoc.notifications_log', 'driver_id') IS NULL ALTER TABLE fpoc.notifications_log ADD driver_id NVARCHAR(20) NULL",
    "IF COL_LENGTH('fpoc.notifications_log', 'content_sid') IS NULL ALTER TABLE fpoc.notifications_log ADD content_sid NVARCHAR(100) NULL",
    "IF COL_LENGTH('fpoc.notifications_log', 'content_variables') IS NULL ALTER TABLE fpoc.notifications_log ADD content_variables NVARCHAR(MAX) NULL",
    "IF COL_LENGTH('fpoc.notifications_log', 'region') IS NULL ALTER TABLE fpoc.notifications_log ADD region NVARCHAR(50) NULL",
    "IF COL_LENGTH('fpoc.notifications_log', 'direction') IS NULL ALTER TABLE fpoc.notifications_log ADD direction NVARCHAR(20) NULL DEFAULT 'outbound'",
    "IF COL_LENGTH('fpoc.notifications_log', 'profile_name') IS NULL ALTER TABLE fpoc.notifications_log ADD profile_name NVARCHAR(200) NULL",
    "IF COL_LENGTH('fpoc.notifications_log', 'media_urls') IS NULL ALTER TABLE fpoc.notifications_log ADD media_urls NVARCHAR(MAX) NULL",
    # simpli_visits: region + comuna + ruta_id (sprint 6)
    "IF COL_LENGTH('fpoc.simpli_visits', 'region') IS NULL ALTER TABLE fpoc.simpli_visits ADD region NVARCHAR(50) NULL",
    "IF COL_LENGTH('fpoc.simpli_visits', 'comuna') IS NULL ALTER TABLE fpoc.simpli_visits ADD comuna NVARCHAR(100) NULL",
    "IF COL_LENGTH('fpoc.simpli_visits', 'ruta_id') IS NULL ALTER TABLE fpoc.simpli_visits ADD ruta_id NVARCHAR(50) NULL",
    # vip_clients: deadline_time (Sprint 4 deadline cron)
    "IF COL_LENGTH('fpoc.vip_clients', 'deadline_time') IS NULL ALTER TABLE fpoc.vip_clients ADD deadline_time TIME(0) NULL",
    "IF COL_LENGTH('fpoc.vip_clients', 'deadline_warning_min') IS NULL ALTER TABLE fpoc.vip_clients ADD deadline_warning_min INT NULL",
]


def main():
    print("=== Bootstrap Azure SQL schema (tablas faltantes) ===")
    print(f"  Server: {os.environ['DB_SERVER']}")
    print(f"  DB:     {os.environ['DB_NAME']}")
    print()
    cn = open_azure()
    cur = cn.cursor()

    print("==> Creando tablas faltantes...")
    for i, sql in enumerate(DDL_BLOCKS, 1):
        try:
            cur.execute(sql)
            print(f"  [{i}/{len(DDL_BLOCKS)}] OK")
        except Exception as e:
            print(f"  [{i}/{len(DDL_BLOCKS)}] FAIL: {e}")

    print()
    print("==> Aplicando ALTERs idempotentes...")
    for i, sql in enumerate(ALTERS, 1):
        try:
            cur.execute(sql)
            print(f"  [{i}/{len(ALTERS)}] OK")
        except Exception as e:
            print(f"  [{i}/{len(ALTERS)}] FAIL: {e}")

    print()
    print("==> Tablas finales en fpoc:")
    cur.execute(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA='fpoc' ORDER BY TABLE_NAME"
    )
    for r in cur.fetchall():
        cur2 = cn.cursor()
        cur2.execute(f"SELECT COUNT(*) FROM fpoc.[{r[0]}]")
        n = int(cur2.fetchone()[0])
        print(f"  fpoc.{r[0]:35s} n={n}")

    cn.close()
    print()
    print("OK")


if __name__ == "__main__":
    main()
