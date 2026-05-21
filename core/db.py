"""DB layer — Azure SQL como ÚNICO backend.

Configuración via env vars (todas requeridas; fail-fast al import):
  - DB_SERVER    (sin prefijo `tcp:` o con él; se normaliza)
  - DB_NAME
  - DB_USER
  - DB_PASSWORD
  - DB_DRIVER    (opcional; default 'ODBC Driver 17 for SQL Server')

API exportada para los callers:
  - get_conn()         → context manager con cursor pyodbc envuelto
  - conn_str()         → connection string (para herramientas / dumps)
  - IntegrityError     → re-export de pyodbc.IntegrityError
  - backend()          → "sqlserver" (constante; compat con código viejo)

El wrapper de cursor reescribe SQL portable a sintaxis MSSQL:
  - fpoc_<tabla>                  → fpoc.<tabla>      (schema dot notation)
  - datetime('now', '-N days')    → DATEADD(day, -N, SYSUTCDATETIME())
  - ... LIMIT N                   → SELECT TOP N ...           (literal)
  - ... LIMIT ?                   → SELECT TOP (?) ...         (placeholder; reordena params)
  - ... LIMIT ?/N OFFSET ?        → ... OFFSET ? ROWS FETCH NEXT ?/N ROWS ONLY
  - INSERT OR IGNORE              → INSERT (caller maneja duplicado)
  - last_insert_rowid()           → SCOPE_IDENTITY()

Otras divergencias T-SQL (MERGE, OUTPUT INSERTED, DATEADD, PERCENTILE_CONT) se
escriben directamente en MSSQL en cada módulo — son demasiado contextuales para
un rewriter genérico.

Histórico: este módulo soportaba además SQLite como fallback dev local. Ese
path fue removido (POC ya usa Azure SQL siempre); el rewriter MSSQL se mantiene
porque varios módulos siguen escribiendo SQL en notación `fpoc_<tabla>` y con
`LIMIT/OFFSET` portable.
"""
from __future__ import annotations

import os
import re
from typing import Any

# Fail-fast al import: si no hay DB_SERVER, abortamos antes de levantar uvicorn.
# El backend NO tiene fallback a SQLite — Azure SQL es obligatorio.
if not os.environ.get("DB_SERVER"):
    raise RuntimeError(
        "DB_SERVER no está seteada. Azure SQL es el único backend soportado. "
        "Configurar DB_SERVER, DB_NAME, DB_USER, DB_PASSWORD en .env."
    )


# =============================================================================
# SQL rewriter — convierte sintaxis SQLite-style a MSSQL.
# Algunos módulos escriben queries en notación portable (`fpoc_<tabla>`,
# `LIMIT N`, `datetime('now', ...)`) por inercia histórica del modo dual.
# El rewriter las traduce on-the-fly al dialecto MSSQL.
# =============================================================================
_FPOC_TABLE_RE = re.compile(r"\bfpoc_([a-zA-Z_][a-zA-Z0-9_]*)\b")
_DATETIME_NOW_RE = re.compile(
    r"datetime\s*\(\s*'now'\s*,\s*'-?(\d+)\s+(day|days|hour|hours|minute|minutes)'\s*\)",
    re.IGNORECASE,
)
# LIMIT N al final del query (literal). Se traduce a SELECT TOP N en el primer SELECT.
_LIMIT_TAIL_RE = re.compile(r"\s+LIMIT\s+(\d+)\s*;?\s*$", re.IGNORECASE)
# LIMIT ? al final (placeholder). El último param se mueve al principio como TOP (?).
_LIMIT_PLACEHOLDER_TAIL_RE = re.compile(r"\s+LIMIT\s+\?\s*;?\s*$", re.IGNORECASE)
# LIMIT ? OFFSET ?  ó  LIMIT N OFFSET ? — se traduce a OFFSET ? ROWS FETCH NEXT ? ROWS ONLY.
# Requiere que el query ya tenga ORDER BY (sino MSSQL rechaza OFFSET).
_LIMIT_OFFSET_PLACEHOLDER_RE = re.compile(
    r"\s+LIMIT\s+(\?|\d+)\s+OFFSET\s+\?\s*;?\s*$", re.IGNORECASE
)
_SELECT_TOKEN_RE = re.compile(r"\bSELECT\b", re.IGNORECASE)
# INSERT OR IGNORE no tiene equivalente directo en T-SQL; lo neutralizamos a INSERT
# y dejamos al caller manejar duplicados (idealmente con MERGE o IF NOT EXISTS).
_INSERT_OR_IGNORE_RE = re.compile(r"\bINSERT\s+OR\s+IGNORE\b", re.IGNORECASE)
# last_insert_rowid() (SQLite) -> SCOPE_IDENTITY() (T-SQL) para devolver el id
# autoincrementado del INSERT recien hecho en el mismo cursor.
_LAST_INSERT_ROWID_RE = re.compile(r"\blast_insert_rowid\s*\(\s*\)", re.IGNORECASE)


def _rewrite_sql_for_mssql(sql: str, params: tuple = ()) -> tuple[str, tuple]:
    """Convierte SQL escrito en estilo SQLite a sintaxis SQL Server.

    Devuelve (sql_reescrito, params_reordenados). La función puede mover params
    cuando los placeholders cambian de posición (ej: LIMIT ? -> SELECT TOP (?)).
    """
    sql = _FPOC_TABLE_RE.sub(r"fpoc.\1", sql)

    def _datetime_repl(m):
        n = m.group(1)
        unit = m.group(2).lower()
        unit_map = {
            "day": "day", "days": "day",
            "hour": "hour", "hours": "hour",
            "minute": "minute", "minutes": "minute",
        }
        u = unit_map.get(unit, "day")
        return f"DATEADD({u}, -{n}, SYSUTCDATETIME())"

    sql = _DATETIME_NOW_RE.sub(_datetime_repl, sql)

    # Caso compuesto: LIMIT ? OFFSET ? o LIMIT N OFFSET ?
    m_off = _LIMIT_OFFSET_PLACEHOLDER_RE.search(sql)
    if m_off:
        limit_token = m_off.group(1)  # '?' o número literal
        sql = _LIMIT_OFFSET_PLACEHOLDER_RE.sub("", sql)
        if limit_token == "?":
            # params: [..., limit, offset]. MSSQL espera OFFSET ? FETCH NEXT ?
            # con ese mismo orden (offset va primero en el SQL).
            params = list(params)
            limit_p, offset_p = params[-2], params[-1]
            params = tuple(params[:-2] + [offset_p, limit_p])
            sql = sql + " OFFSET ? ROWS FETCH NEXT ? ROWS ONLY"
        else:
            # LIMIT N (literal) OFFSET ?  →  OFFSET ? ROWS FETCH NEXT N ROWS ONLY
            sql = sql + f" OFFSET ? ROWS FETCH NEXT {limit_token} ROWS ONLY"
    else:
        # LIMIT ? (placeholder, sin OFFSET)
        m_p = _LIMIT_PLACEHOLDER_TAIL_RE.search(sql)
        if m_p:
            sql = _LIMIT_PLACEHOLDER_TAIL_RE.sub("", sql)
            # Mover el último param (limit) al principio, en SELECT TOP (?).
            params = list(params)
            limit_p = params.pop()
            params = tuple([limit_p] + params)
            sql = _SELECT_TOKEN_RE.sub("SELECT TOP (?)", sql, count=1)
        else:
            # LIMIT N literal (mantengo comportamiento original).
            m = _LIMIT_TAIL_RE.search(sql)
            if m:
                n = m.group(1)
                sql = _LIMIT_TAIL_RE.sub("", sql)
                sql = _SELECT_TOKEN_RE.sub(f"SELECT TOP {n}", sql, count=1)

    sql = _INSERT_OR_IGNORE_RE.sub("INSERT", sql)
    sql = _LAST_INSERT_ROWID_RE.sub("SCOPE_IDENTITY()", sql)
    return sql, tuple(params) if not isinstance(params, tuple) else params


class MssqlCursor:
    """Wrap pyodbc cursor para reescribir SQL estilo SQLite (fpoc_tabla,
    datetime('now', '-N days')) a sintaxis MSSQL en cada execute/executemany."""

    def __init__(self, cur) -> None:
        self._cur = cur

    def execute(self, sql, *params):
        # Normalizar params a tupla plana (algunos callers pasan lista/tupla single).
        flat = tuple(params[0]) if (len(params) == 1 and isinstance(params[0], (tuple, list))) else tuple(params)
        sql, flat = _rewrite_sql_for_mssql(sql, flat)
        if flat:
            self._cur.execute(sql, flat)
        else:
            self._cur.execute(sql)
        return self

    def executemany(self, sql, seq):
        # executemany no entra en reordering de LIMIT — sería ambiguo. Solo
        # aplica las reescrituras de strings (sin tocar params).
        sql, _ = _rewrite_sql_for_mssql(sql, ())
        self._cur.executemany(sql, seq)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    @property
    def fast_executemany(self):
        return self._cur.fast_executemany

    @fast_executemany.setter
    def fast_executemany(self, value):
        self._cur.fast_executemany = value

    def close(self):
        self._cur.close()


class MssqlConn:
    """Wrapper de pyodbc.Connection que aplica el rewrite SQLite->MSSQL en
    cada cursor.execute. Soporta `with get_conn() as cn:` (commit/rollback
    explícito por el caller; el __exit__ solo hace rollback si hay excepción)."""

    def __init__(self, raw) -> None:
        self._raw = raw

    def cursor(self) -> MssqlCursor:
        return MssqlCursor(self._raw.cursor())

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()

    def execute(self, sql, *params):
        cur = self.cursor()
        cur.execute(sql, *params)
        return cur

    def __enter__(self) -> "MssqlConn":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # NO commit automático: los callers ya tienen `cn.commit()` explícito
        # en cada path de escritura. Si una excepción burbujea, hacemos rollback.
        try:
            if exc_type is not None:
                self._raw.rollback()
        finally:
            self._raw.close()


def _open_sqlserver() -> MssqlConn:
    import pyodbc  # import perezoso (evita romper si el driver no está al import)

    server = os.environ["DB_SERVER"].replace("tcp:", "")
    cs = (
        f"DRIVER={{{os.environ.get('DB_DRIVER', 'ODBC Driver 17 for SQL Server')}}};"
        f"SERVER={server};"
        f"DATABASE={os.environ['DB_NAME']};"
        f"UID={os.environ['DB_USER']};"
        f"PWD={os.environ['DB_PASSWORD']};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )
    return MssqlConn(pyodbc.connect(cs, autocommit=False))


def get_conn() -> MssqlConn:
    """Devuelve una conexión Azure SQL usable con `with`."""
    return _open_sqlserver()


def conn_str() -> str:
    """Connection string para herramientas externas (dumps, manual debugging)."""
    server = os.environ["DB_SERVER"].replace("tcp:", "")
    return (
        f"DRIVER={{{os.environ.get('DB_DRIVER', 'ODBC Driver 17 for SQL Server')}}};"
        f"SERVER={server};"
        f"DATABASE={os.environ['DB_NAME']};"
        f"UID={os.environ['DB_USER']};"
        f"PWD={os.environ['DB_PASSWORD']};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )


# IntegrityError re-export (para `except db.IntegrityError`).
# Import perezoso fallback por si pyodbc no está disponible en algún path
# inusual (CI sin ODBC); seteamos None y el except simplemente no matchea.
try:
    import pyodbc as _pyodbc  # type: ignore
    IntegrityError = _pyodbc.IntegrityError
except Exception:  # noqa: BLE001
    IntegrityError = None  # type: ignore[assignment]


def backend() -> str:
    """Compat con código viejo. Siempre devuelve 'sqlserver'."""
    return "sqlserver"


__all__ = [
    "get_conn",
    "conn_str",
    "IntegrityError",
    "backend",
    "MssqlConn",
    "MssqlCursor",
]
