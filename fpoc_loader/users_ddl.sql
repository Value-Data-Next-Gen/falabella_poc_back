-- Empresas de transporte + usuarios (multi-tenant POC)

IF OBJECT_ID('fpoc.empresas_transporte', 'U') IS NULL
BEGIN
    CREATE TABLE fpoc.empresas_transporte (
        empresa_id  INT           NOT NULL PRIMARY KEY,
        nombre      NVARCHAR(100) NOT NULL,
        activo      BIT           NOT NULL CONSTRAINT DF_empresas_activo DEFAULT 1,
        created_at  DATETIME2(0)  NOT NULL CONSTRAINT DF_empresas_created DEFAULT SYSUTCDATETIME()
    );
END;
GO

IF OBJECT_ID('fpoc.users', 'U') IS NULL
BEGIN
    CREATE TABLE fpoc.users (
        user_id        INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        email          NVARCHAR(200)     NOT NULL UNIQUE,
        password_hash  NVARCHAR(200)     NOT NULL,
        display_name   NVARCHAR(200)     NOT NULL,
        role           NVARCHAR(30)      NOT NULL,
        empresa_id     INT               NULL,
        activo         BIT               NOT NULL CONSTRAINT DF_users_activo DEFAULT 1,
        created_at     DATETIME2(0)      NOT NULL CONSTRAINT DF_users_created DEFAULT SYSUTCDATETIME(),
        last_login     DATETIME2(0)      NULL,
        CONSTRAINT CK_users_role CHECK (role IN ('falabella_admin', 'falabella_ops', 'transport_manager')),
        CONSTRAINT FK_users_empresa FOREIGN KEY (empresa_id) REFERENCES fpoc.empresas_transporte(empresa_id)
    );
    CREATE INDEX IX_users_empresa ON fpoc.users(empresa_id);
    CREATE INDEX IX_users_role ON fpoc.users(role);
END;
GO
