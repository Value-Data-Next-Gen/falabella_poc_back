-- Extiende notifications_log para soportar Content Templates de Twilio
IF COL_LENGTH('fpoc.notifications_log', 'content_sid') IS NULL
    ALTER TABLE fpoc.notifications_log ADD content_sid NVARCHAR(100) NULL;
GO
IF COL_LENGTH('fpoc.notifications_log', 'content_variables') IS NULL
    ALTER TABLE fpoc.notifications_log ADD content_variables NVARCHAR(MAX) NULL;
GO
