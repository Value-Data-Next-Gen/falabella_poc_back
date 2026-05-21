-- fpoc schema + tablas espejo del Excel datos_eta_YYYY-MM-DD.xlsx
-- Ejecutar una sola vez; load_to_azure.py lo aplica idempotentemente.

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'fpoc')
    EXEC('CREATE SCHEMA fpoc AUTHORIZATION dbo');
GO

IF OBJECT_ID('fpoc.simpli_visits', 'U') IS NULL
BEGIN
    CREATE TABLE fpoc.simpli_visits (
        planned_date                          DATE            NOT NULL,
        id                                    BIGINT          NOT NULL,
        title                                 NVARCHAR(200)   NOT NULL,
        [order]                               INT             NOT NULL,
        address                               NVARCHAR(500)   NOT NULL,
        checkout_cl                           DATETIME2(0)    NOT NULL,
        current_eta_cl                        DATETIME2(0)    NOT NULL,
        status                                NVARCHAR(50)    NOT NULL,
        checkout_comment                      NVARCHAR(500)   NULL,
        checkout_observation                  NVARCHAR(500)   NULL,
        reference                             BIGINT          NOT NULL,
        country                               NVARCHAR(10)    NOT NULL,
        sla_hour_checkout_eta                 DECIMAL(10, 4)  NOT NULL,
        bin_start                             DECIMAL(10, 4)  NOT NULL,
        bin_end                               DECIMAL(10, 4)  NOT NULL,
        bin_label                             NVARCHAR(50)    NOT NULL,
        bin_index                             INT             NOT NULL,
        ct                                    NVARCHAR(100)   NOT NULL,
        patente_falsa                         INT             NOT NULL,
        empresa_falsa                         INT             NOT NULL,
        driver_name                            NVARCHAR(200)   NOT NULL,
        fecha_inicio_ruta                       NVARCHAR(50)    NOT NULL,
        fecha_inicio_ruta_hora_cl               TIME(0)         NOT NULL,
        fechas_futuras_bq                     BIT             NOT NULL,
        finicio_currenteta_bq                 BIT             NOT NULL,
        current_eta_cl_fechainicioruta        INT             NOT NULL,
        current_eta_cl_fechainicioruta_dates  BIT             NOT NULL,
        ruta_eta_futuro                       BIT             NOT NULL,
        ruta_fecha_inicio_mayor_eta           BIT             NOT NULL,
        ruta_primer_punto_lejano              BIT             NOT NULL,
        ruta_fecha_inicio_distinta_fecha_eta  BIT             NOT NULL,
        am_pm                                 NVARCHAR(2)     NOT NULL,
        ruta_anomala                          BIT             NOT NULL,
        CONSTRAINT PK_simpli_visits PRIMARY KEY (id)
    );
    CREATE INDEX IX_simpli_visits_planned_date ON fpoc.simpli_visits (planned_date);
    CREATE INDEX IX_simpli_visits_ct ON fpoc.simpli_visits (ct);
END;
GO

IF OBJECT_ID('fpoc.geo_suborders', 'U') IS NULL
BEGIN
    CREATE TABLE fpoc.geo_suborders (
        Suborden             BIGINT         NOT NULL,
        fechainicioruta      NVARCHAR(50)   NOT NULL,
        patente_falsa        INT            NOT NULL,
        empresa_falsa        INT            NOT NULL,
        idruta               BIGINT         NOT NULL,
        [do]                 BIGINT         NOT NULL,
        lpn                  BIGINT         NULL,
        parentorder          BIGINT         NULL,
        direccion            NVARCHAR(500)  NOT NULL,
        localidad            NVARCHAR(100)  NOT NULL,
        region               NVARCHAR(100)  NOT NULL,
        fechapactada         DATE           NOT NULL,
        tipodocumento        NVARCHAR(50)   NOT NULL,
        estado               NVARCHAR(50)   NOT NULL,
        motivonoentrega      NVARCHAR(200)  NULL,
        comentarionoentrega  NVARCHAR(500)  NULL,
        CONSTRAINT PK_geo_suborders PRIMARY KEY (Suborden)
    );
    CREATE INDEX IX_geo_suborders_idruta ON fpoc.geo_suborders (idruta);
    CREATE INDEX IX_geo_suborders_estado ON fpoc.geo_suborders (estado);
END;
GO
