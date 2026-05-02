-- SQLite schema para el POC ValueData (DB_BACKEND=sqlite).
-- Equivalente al fpoc.* de Azure SQL, pero con tablas planas (fpoc_*) porque
-- SQLite no soporta schemas. Idempotente: usa IF NOT EXISTS en todo.

-- ============================================================================
-- Datos del Excel datos_eta_*.xlsx
-- ============================================================================

CREATE TABLE IF NOT EXISTS fpoc_simpli_visits (
    planned_date                          DATE       NOT NULL,
    id                                    INTEGER    NOT NULL PRIMARY KEY,
    title                                 TEXT       NOT NULL,
    "order"                               INTEGER    NOT NULL,
    address                               TEXT       NOT NULL,
    checkout_cl                           TIMESTAMP  NOT NULL,
    current_eta_cl                        TIMESTAMP  NOT NULL,
    status                                TEXT     NOT NULL,
    checkout_comment                      TEXT,
    checkout_observation                  TEXT,
    reference                             INTEGER  NOT NULL,
    country                               TEXT     NOT NULL,
    sla_hour_checkout_eta                 REAL     NOT NULL,
    bin_start                             REAL     NOT NULL,
    bin_end                               REAL     NOT NULL,
    bin_label                             TEXT     NOT NULL,
    bin_index                             INTEGER  NOT NULL,
    ct                                    TEXT     NOT NULL,
    Patente_falsa                         INTEGER  NOT NULL,
    Empresa_falsa                         INTEGER  NOT NULL,
    Drivername                            TEXT     NOT NULL,
    Fechainicioruta                       TEXT     NOT NULL,
    Fechainicioruta_hora_cl               TEXT     NOT NULL,
    fechas_futuras_bq                     INTEGER  NOT NULL,
    finicio_currenteta_bq                 INTEGER  NOT NULL,
    current_eta_cl_fechainicioruta        INTEGER  NOT NULL,
    current_eta_cl_fechainicioruta_dates  INTEGER  NOT NULL,
    ruta_eta_futuro                       INTEGER  NOT NULL,
    ruta_fecha_inicio_mayor_eta           INTEGER  NOT NULL,
    ruta_primer_punto_lejano              INTEGER  NOT NULL,
    ruta_fecha_inicio_distinta_fecha_eta  INTEGER  NOT NULL,
    am_pm                                 TEXT     NOT NULL,
    ruta_anomala                          INTEGER  NOT NULL
);
CREATE INDEX IF NOT EXISTS IX_simpli_visits_planned_date ON fpoc_simpli_visits (planned_date);
CREATE INDEX IF NOT EXISTS IX_simpli_visits_ct ON fpoc_simpli_visits (ct);
CREATE INDEX IF NOT EXISTS IX_simpli_visits_empresa ON fpoc_simpli_visits (Empresa_falsa);

CREATE TABLE IF NOT EXISTS fpoc_geo_suborders (
    Suborden             INTEGER  NOT NULL PRIMARY KEY,
    fechainicioruta      TEXT     NOT NULL,
    patente_falsa        INTEGER  NOT NULL,
    empresa_falsa        INTEGER  NOT NULL,
    idruta               INTEGER  NOT NULL,
    "do"                 INTEGER  NOT NULL,
    lpn                  INTEGER,
    parentorder          INTEGER,
    direccion            TEXT     NOT NULL,
    localidad            TEXT     NOT NULL,
    region               TEXT     NOT NULL,
    fechapactada         DATE     NOT NULL,
    tipodocumento        TEXT     NOT NULL,
    estado               TEXT     NOT NULL,
    motivonoentrega      TEXT,
    comentarionoentrega  TEXT
);
CREATE INDEX IF NOT EXISTS IX_geo_suborders_idruta ON fpoc_geo_suborders (idruta);
CREATE INDEX IF NOT EXISTS IX_geo_suborders_estado ON fpoc_geo_suborders (estado);

-- ============================================================================
-- Multi-tenancy: empresas_transporte + users
-- ============================================================================

CREATE TABLE IF NOT EXISTS fpoc_empresas_transporte (
    empresa_id  INTEGER  NOT NULL PRIMARY KEY,
    nombre      TEXT     NOT NULL,
    activo      INTEGER    NOT NULL DEFAULT 1,
    created_at  TIMESTAMP  NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fpoc_users (
    user_id        INTEGER    PRIMARY KEY AUTOINCREMENT,
    email          TEXT       NOT NULL UNIQUE,
    password_hash  TEXT       NOT NULL,
    display_name   TEXT       NOT NULL,
    role           TEXT       NOT NULL CHECK (role IN ('falabella_admin', 'falabella_ops', 'transport_manager')),
    empresa_id     INTEGER,
    activo         INTEGER    NOT NULL DEFAULT 1,
    created_at     TIMESTAMP  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login     TIMESTAMP,
    -- Notificaciones (vienen del notifications_ddl.sql original)
    phone_e164                  TEXT,
    notify_whatsapp             INTEGER NOT NULL DEFAULT 0,
    notify_pfallo_threshold     REAL    NOT NULL DEFAULT 0.500,
    notify_slack_min_threshold  INTEGER NOT NULL DEFAULT 15,
    notify_only_vip             INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (empresa_id) REFERENCES fpoc_empresas_transporte(empresa_id)
);
CREATE INDEX IF NOT EXISTS IX_users_empresa ON fpoc_users(empresa_id);
CREATE INDEX IF NOT EXISTS IX_users_role ON fpoc_users(role);

-- ============================================================================
-- Auditoría de accesos
-- ============================================================================

CREATE TABLE IF NOT EXISTS fpoc_access_log (
    log_id           INTEGER  PRIMARY KEY AUTOINCREMENT,
    event_type       TEXT     NOT NULL CHECK (event_type IN ('login_success', 'login_failed', 'logout')),
    user_id          INTEGER,
    email_attempted  TEXT,
    ip_address       TEXT,
    user_agent       TEXT,
    error_detail     TEXT,
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES fpoc_users(user_id)
);
CREATE INDEX IF NOT EXISTS IX_access_created ON fpoc_access_log(created_at DESC);
CREATE INDEX IF NOT EXISTS IX_access_user ON fpoc_access_log(user_id);
CREATE INDEX IF NOT EXISTS IX_access_ip ON fpoc_access_log(ip_address);

-- ============================================================================
-- Notificaciones
-- ============================================================================

CREATE TABLE IF NOT EXISTS fpoc_notifications_log (
    notification_id   INTEGER  PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER,
    contact_id        INTEGER,
    to_number         TEXT     NOT NULL,
    channel           TEXT     NOT NULL DEFAULT 'whatsapp',
    subject           TEXT,
    body              TEXT     NOT NULL,
    tracking_id       TEXT,
    twilio_sid        TEXT,
    status            TEXT     NOT NULL,
    error_msg         TEXT,
    triggered_by      TEXT     NOT NULL,
    content_sid       TEXT,
    content_variables TEXT,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES fpoc_users(user_id),
    FOREIGN KEY (contact_id) REFERENCES fpoc_empresa_contactos(contact_id)
);
CREATE INDEX IF NOT EXISTS IX_notif_user ON fpoc_notifications_log(user_id);
CREATE INDEX IF NOT EXISTS IX_notif_tracking ON fpoc_notifications_log(tracking_id);
CREATE INDEX IF NOT EXISTS IX_notif_created ON fpoc_notifications_log(created_at DESC);

-- ============================================================================
-- Contactos por empresa transportista (destinatarios de notificaciones).
-- Separa el concepto "destinatario WhatsApp" del "user que hace login".
-- Un mismo phone puede tener N contactos (diferentes empresas/roles), y un
-- mismo user puede no estar acá (admin login no recibe alertas, p.e.).
-- ============================================================================
CREATE TABLE IF NOT EXISTS fpoc_empresa_contactos (
    contact_id          INTEGER  PRIMARY KEY AUTOINCREMENT,
    empresa_id          INTEGER  NOT NULL,
    nombre              TEXT     NOT NULL,
    rol                 TEXT     NOT NULL CHECK (rol IN ('jefe','coordinador','dispatcher','driver','otro')),
    phone_e164          TEXT     NOT NULL,
    email               TEXT,
    severities_in       TEXT,    -- JSON array. NULL = todas las severidades
    motivos_in          TEXT,    -- JSON array. NULL = todos los motivos
    region_filter       TEXT     NOT NULL DEFAULT 'all' CHECK (region_filter IN ('RM','regiones','all')),
    opted_in_at         TIMESTAMP,  -- compliance ToS WhatsApp; NULL = no consintió aún
    active              INTEGER  NOT NULL DEFAULT 1,
    notes               TEXT,
    created_by_user_id  INTEGER,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (empresa_id) REFERENCES fpoc_empresas_transporte(empresa_id),
    FOREIGN KEY (created_by_user_id) REFERENCES fpoc_users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_fpoc_empresa_contactos_empresa ON fpoc_empresa_contactos(empresa_id, active);
CREATE INDEX IF NOT EXISTS idx_fpoc_empresa_contactos_phone   ON fpoc_empresa_contactos(phone_e164);

-- ============================================================================
-- VIP clients + priority overrides
-- ============================================================================

CREATE TABLE IF NOT EXISTS fpoc_vip_clients (
    vip_id               INTEGER  PRIMARY KEY AUTOINCREMENT,
    match_type           TEXT     NOT NULL CHECK (match_type IN ('customer_id', 'title', 'reference')),
    match_value          TEXT     NOT NULL,
    empresa_id           INTEGER,
    tier                 TEXT     NOT NULL DEFAULT 'VIP',
    notes                TEXT,
    deadline_time        TEXT,                         -- HH:MM, NULL si no aplica
    alert_minutes_before INTEGER  NOT NULL DEFAULT 60, -- min antes del deadline
    last_alert_sent_at   TIMESTAMP,                    -- tracking del cron
    active               INTEGER  NOT NULL DEFAULT 1,
    created_by           INTEGER,
    created_at           TIMESTAMP  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (match_type, match_value, empresa_id),
    FOREIGN KEY (created_by) REFERENCES fpoc_users(user_id),
    FOREIGN KEY (empresa_id) REFERENCES fpoc_empresas_transporte(empresa_id)
);
CREATE INDEX IF NOT EXISTS IX_vip_match ON fpoc_vip_clients(match_type, match_value);

CREATE TABLE IF NOT EXISTS fpoc_visit_priority_overrides (
    override_id    INTEGER  PRIMARY KEY AUTOINCREMENT,
    tracking_id    TEXT     NOT NULL UNIQUE,
    priority       TEXT     NOT NULL CHECK (priority IN ('low', 'normal', 'high', 'vip')),
    reason         TEXT,
    set_by         INTEGER,
    set_at         TIMESTAMP  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (set_by) REFERENCES fpoc_users(user_id)
);

-- ============================================================================
-- Maestros operativos (drivers, vehicles, clients) — persistidos en SQLite
-- ============================================================================

CREATE TABLE IF NOT EXISTS fpoc_drivers (
    driver_id        TEXT     PRIMARY KEY,
    name             TEXT     NOT NULL,
    phone            TEXT,
    license          TEXT,
    vehicle_id       INTEGER  NOT NULL,
    vehicle_name     TEXT     NOT NULL,
    rating           REAL     NOT NULL DEFAULT 4.5,
    deliveries_30d   INTEGER  NOT NULL DEFAULT 0,
    fail_rate_30d    REAL     NOT NULL DEFAULT 0.10,
    joined_at        DATE,
    active           INTEGER  NOT NULL DEFAULT 1,
    is_problem_hidden INTEGER NOT NULL DEFAULT 0,
    -- Sprint 4.A1: WhatsApp opt-in
    phone_e164       TEXT,
    notify_whatsapp  INTEGER  NOT NULL DEFAULT 0,
    opted_in_at      TIMESTAMP,
    updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Sprint 4.A2: Tabla de correcciones de motivo (LLM auto-validation)
CREATE TABLE IF NOT EXISTS fpoc_motivo_corrections (
    correction_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id         INTEGER NOT NULL,
    tracking_id        TEXT NOT NULL,
    motivo_reportado   TEXT NOT NULL,
    motivo_sugerido    TEXT NOT NULL,
    confianza          TEXT NOT NULL,
    razonamiento       TEXT NOT NULL,
    driver_id          TEXT,
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','accepted','rejected','no_action')),
    decided_by_user_id INTEGER,
    decided_at         TIMESTAMP,
    notified_driver_at TIMESTAMP,
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (comment_id) REFERENCES fpoc_visit_comments(comment_id)
);
CREATE INDEX IF NOT EXISTS idx_corrections_status ON fpoc_motivo_corrections(status, created_at);
CREATE INDEX IF NOT EXISTS idx_corrections_driver ON fpoc_motivo_corrections(driver_id);
CREATE INDEX IF NOT EXISTS idx_corrections_tracking ON fpoc_motivo_corrections(tracking_id);

CREATE TABLE IF NOT EXISTS fpoc_vehicles (
    vehicle_id     INTEGER  PRIMARY KEY,
    name           TEXT     NOT NULL,
    type           TEXT     NOT NULL,
    plate          TEXT     NOT NULL,
    capacity_m3    INTEGER  NOT NULL,
    driver_id      TEXT,
    driver_name    TEXT,
    depot_lat      REAL     NOT NULL,
    depot_lon      REAL     NOT NULL,
    year           INTEGER,
    active         INTEGER  NOT NULL DEFAULT 1,
    is_problem_hidden INTEGER NOT NULL DEFAULT 0,
    updated_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fpoc_clients (
    customer_id      TEXT     PRIMARY KEY,
    title            TEXT     NOT NULL,
    address          TEXT     NOT NULL,
    latitude         REAL     NOT NULL,
    longitude        REAL     NOT NULL,
    is_recurrent     INTEGER  NOT NULL DEFAULT 0,
    in_problem_comuna INTEGER NOT NULL DEFAULT 0,
    notes            TEXT,
    updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS IX_clients_title ON fpoc_clients(title);
CREATE INDEX IF NOT EXISTS IX_clients_recurrent ON fpoc_clients(is_recurrent);

-- ============================================================================
-- Comentarios del transportista + config de motivos alertables
-- (catálogo de motivos extraído del notebook auditoria_llm_directo.ipynb)
-- ============================================================================

CREATE TABLE IF NOT EXISTS fpoc_visit_comments (
    comment_id   INTEGER  PRIMARY KEY AUTOINCREMENT,
    tracking_id  TEXT     NOT NULL,
    vehicle_id   INTEGER,
    empresa_id   INTEGER,
    motivo       TEXT     NOT NULL,
    comentario   TEXT     NOT NULL,
    created_by   INTEGER,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (created_by) REFERENCES fpoc_users(user_id),
    FOREIGN KEY (empresa_id) REFERENCES fpoc_empresas_transporte(empresa_id)
);
CREATE INDEX IF NOT EXISTS IX_comments_tracking ON fpoc_visit_comments(tracking_id);
CREATE INDEX IF NOT EXISTS IX_comments_empresa ON fpoc_visit_comments(empresa_id);
CREATE INDEX IF NOT EXISTS IX_comments_created ON fpoc_visit_comments(created_at DESC);

CREATE TABLE IF NOT EXISTS fpoc_motivo_alert_config (
    motivo       TEXT     NOT NULL,
    empresa_id   INTEGER,                    -- NULL = configuración global
    alertable    INTEGER  NOT NULL DEFAULT 0,
    severity     TEXT     NOT NULL DEFAULT 'medium' CHECK (severity IN ('low','medium','high','critical')),
    description  TEXT,                       -- override de la descripción usada en el prompt LLM
    updated_by   INTEGER,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (motivo, empresa_id),
    FOREIGN KEY (updated_by) REFERENCES fpoc_users(user_id),
    FOREIGN KEY (empresa_id) REFERENCES fpoc_empresas_transporte(empresa_id)
);
