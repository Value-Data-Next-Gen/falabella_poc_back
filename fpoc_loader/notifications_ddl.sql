-- Migración: notificaciones + VIP + prioridad
-- Todo idempotente (IF NOT EXISTS / IF COL_LENGTH)

-- 1) Extensiones a fpoc.users
IF COL_LENGTH('fpoc.users', 'phone_e164') IS NULL
    ALTER TABLE fpoc.users ADD phone_e164 NVARCHAR(20) NULL;
GO
IF COL_LENGTH('fpoc.users', 'notify_whatsapp') IS NULL
    ALTER TABLE fpoc.users ADD notify_whatsapp BIT NOT NULL
        CONSTRAINT DF_users_notify_whatsapp DEFAULT 0;
GO
IF COL_LENGTH('fpoc.users', 'notify_pfallo_threshold') IS NULL
    ALTER TABLE fpoc.users ADD notify_pfallo_threshold DECIMAL(4,3) NOT NULL
        CONSTRAINT DF_users_notify_pfallo DEFAULT 0.500;
GO
IF COL_LENGTH('fpoc.users', 'notify_slack_min_threshold') IS NULL
    ALTER TABLE fpoc.users ADD notify_slack_min_threshold INT NOT NULL
        CONSTRAINT DF_users_notify_slack DEFAULT 15;
GO
IF COL_LENGTH('fpoc.users', 'notify_only_vip') IS NULL
    ALTER TABLE fpoc.users ADD notify_only_vip BIT NOT NULL
        CONSTRAINT DF_users_notify_only_vip DEFAULT 0;
GO

-- 2) Log de notificaciones enviadas (auditoría)
IF OBJECT_ID('fpoc.notifications_log', 'U') IS NULL
BEGIN
    CREATE TABLE fpoc.notifications_log (
        notification_id   INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        user_id           INT               NULL,
        to_number         NVARCHAR(20)      NOT NULL,
        channel           NVARCHAR(20)      NOT NULL CONSTRAINT DF_notif_channel DEFAULT 'whatsapp',
        subject           NVARCHAR(200)     NULL,
        body              NVARCHAR(MAX)     NOT NULL,
        tracking_id       NVARCHAR(50)      NULL,
        twilio_sid        NVARCHAR(100)     NULL,
        status            NVARCHAR(20)      NOT NULL,   -- 'sent' | 'dry_run' | 'error' | 'queued'
        error_msg         NVARCHAR(500)     NULL,
        triggered_by      NVARCHAR(20)      NOT NULL,   -- 'manual' | 'auto_threshold' | 'vip'
        created_at        DATETIME2(0)      NOT NULL CONSTRAINT DF_notif_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_notif_user FOREIGN KEY (user_id) REFERENCES fpoc.users(user_id)
    );
    CREATE INDEX IX_notif_user ON fpoc.notifications_log(user_id);
    CREATE INDEX IX_notif_tracking ON fpoc.notifications_log(tracking_id);
    CREATE INDEX IX_notif_created ON fpoc.notifications_log(created_at DESC);
END;
GO

-- 3) Clientes VIP (marcar por customer_id del pipeline sintético
--    o por title para matchear con simpli_visits real)
IF OBJECT_ID('fpoc.vip_clients', 'U') IS NULL
BEGIN
    CREATE TABLE fpoc.vip_clients (
        vip_id         INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        match_type     NVARCHAR(20)      NOT NULL,  -- 'customer_id' | 'title' | 'reference'
        match_value    NVARCHAR(200)     NOT NULL,
        empresa_id     INT               NULL,      -- NULL = VIP global (todas las empresas)
        tier           NVARCHAR(20)      NOT NULL CONSTRAINT DF_vip_tier DEFAULT 'VIP',
        notes          NVARCHAR(500)     NULL,
        active         BIT               NOT NULL CONSTRAINT DF_vip_active DEFAULT 1,
        created_by     INT               NULL,
        created_at     DATETIME2(0)      NOT NULL CONSTRAINT DF_vip_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_vip_user FOREIGN KEY (created_by) REFERENCES fpoc.users(user_id),
        CONSTRAINT FK_vip_empresa FOREIGN KEY (empresa_id) REFERENCES fpoc.empresas_transporte(empresa_id),
        CONSTRAINT UQ_vip_match UNIQUE (match_type, match_value, empresa_id)
    );
    CREATE INDEX IX_vip_match ON fpoc.vip_clients(match_type, match_value);
END;
GO

-- 4) Overrides de prioridad por visita
IF OBJECT_ID('fpoc.visit_priority_overrides', 'U') IS NULL
BEGIN
    CREATE TABLE fpoc.visit_priority_overrides (
        override_id    INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        tracking_id    NVARCHAR(50)      NOT NULL UNIQUE,
        priority       NVARCHAR(20)      NOT NULL CONSTRAINT CK_priority
                         CHECK (priority IN ('low', 'normal', 'high', 'vip')),
        reason         NVARCHAR(500)     NULL,
        set_by         INT               NULL,
        set_at         DATETIME2(0)      NOT NULL CONSTRAINT DF_prio_set DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_prio_user FOREIGN KEY (set_by) REFERENCES fpoc.users(user_id)
    );
END;
GO
