"""Carga las descripciones de motivos del XLSX 'Motivo no entrega HD.xlsx'
a fpoc.motivo_alert_config (empresa_id=NULL, global).

Estas descripciones son la fuente de verdad operacional — el LLM clasificador
las usa para reconocer cada motivo en los comentarios de drivers.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from db import get_conn  # noqa: E402

XLSX_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "client", "data", "Motivo no entrega HD.xlsx"
)


def main() -> int:
    import pandas as pd
    if not os.path.exists(XLSX_PATH):
        print(f"[error] no encuentro {XLSX_PATH}")
        return 1

    df = pd.read_excel(XLSX_PATH, sheet_name="Hoja1")
    print(f"Leyó {len(df)} motivos del XLSX")

    # Defaults de severidad basados en el motivo (heurística)
    severity_map = {
        "SINIESTRO EN CALLE": "critical",
        "PRODUCTO ROBADO": "critical",
        "RIESGO FRAUDE": "high",
        "DETENCION URGENTE": "high",
        "CLIENTE RECHAZA": "medium",
        "PRODUCTO CON PROBLEMAS": "medium",
        "NO CUMPLE CONDICIONES RETIRO": "medium",
        "PROBLEMA DE DIRECCIÓN/ SIN INFORMACIÓN": "medium",
        "FUERA DE COBERTURA/ FRECUENCIA": "medium",
        "NO DESPACHA A LOCALIDAD": "medium",
        "PROD NO ENTREGADO POR TIEMPO": "medium",
        "SIN MORADORES": "low",
        "NO CONOCEN A CLIENTE": "low",
        "PRODUCTO NO CARGADO": "high",
    }
    alertable_map = {
        "SINIESTRO EN CALLE": True,
        "PRODUCTO ROBADO": True,
        "RIESGO FRAUDE": True,
        "DETENCION URGENTE": True,
        "CLIENTE RECHAZA": True,
        "PRODUCTO CON PROBLEMAS": True,
        "PRODUCTO NO CARGADO": True,
    }

    with get_conn() as cn:
        cur = cn.cursor()
        loaded = 0
        for _, row in df.iterrows():
            motivo = str(row["MOTIVO DE NO ENTREGA"]).strip()
            # La columna 'DESCRIPCIÓN' puede tener encoding raro
            desc_col = "DESCRIPCIÓN" if "DESCRIPCIÓN" in df.columns else df.columns[1]
            description = str(row[desc_col]).strip()
            severity = severity_map.get(motivo, "medium")
            alertable = alertable_map.get(motivo, False)

            # Upsert global (empresa_id IS NULL)
            cur.execute(
                "SELECT 1 FROM fpoc.motivo_alert_config "
                "WHERE motivo = ? AND empresa_id IS NULL",
                motivo,
            )
            if cur.fetchone():
                cur.execute(
                    "UPDATE fpoc.motivo_alert_config "
                    "SET alertable = ?, severity = ?, description = ? "
                    "WHERE motivo = ? AND empresa_id IS NULL",
                    1 if alertable else 0, severity, description, motivo,
                )
            else:
                cur.execute(
                    "INSERT INTO fpoc.motivo_alert_config "
                    "(motivo, alertable, severity, empresa_id, description) "
                    "VALUES (?, ?, ?, NULL, ?)",
                    motivo, 1 if alertable else 0, severity, description,
                )
            loaded += 1
            print(f"  [OK] {motivo[:35]:<37} sev={severity:<8} alertable={alertable}")
        cn.commit()
        print(f"\nTotal: {loaded} motivos cargados con descripción.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
