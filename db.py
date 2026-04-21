"""Helper central para la conexión a Azure SQL.

Credenciales desde os.environ (cargadas por main.py desde .env).
"""
from __future__ import annotations

import os

import pyodbc


def conn_str() -> str:
    return (
        f"DRIVER={{{os.environ.get('DB_DRIVER', 'ODBC Driver 17 for SQL Server')}}};"
        f"SERVER={os.environ['DB_SERVER'].replace('tcp:', '')};"
        f"DATABASE={os.environ['DB_NAME']};"
        f"UID={os.environ['DB_USER']};"
        f"PWD={os.environ['DB_PASSWORD']};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )


def get_conn() -> pyodbc.Connection:
    return pyodbc.connect(conn_str(), autocommit=False)
