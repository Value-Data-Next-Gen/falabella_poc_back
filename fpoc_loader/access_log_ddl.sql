-- Auditoría de accesos: login success/failed + IP + user-agent
IF OBJECT_ID('fpoc.access_log', 'U') IS NULL
BEGIN
    CREATE TABLE fpoc.access_log (
        log_id           INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        event_type       NVARCHAR(30)      NOT NULL,
        user_id          INT               NULL,
        email_attempted  NVARCHAR(200)     NULL,
        ip_address       NVARCHAR(50)      NULL,
        user_agent       NVARCHAR(500)     NULL,
        error_detail     NVARCHAR(200)     NULL,
        created_at       DATETIME2(0)      NOT NULL CONSTRAINT DF_access_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_access_user FOREIGN KEY (user_id) REFERENCES fpoc.users(user_id),
        CONSTRAINT CK_access_event CHECK (event_type IN ('login_success', 'login_failed', 'logout'))
    );
    CREATE INDEX IX_access_created ON fpoc.access_log(created_at DESC);
    CREATE INDEX IX_access_user ON fpoc.access_log(user_id);
    CREATE INDEX IX_access_ip ON fpoc.access_log(ip_address);
END;
GO
