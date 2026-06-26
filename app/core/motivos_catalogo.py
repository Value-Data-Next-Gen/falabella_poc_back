"""Catalogo oficial de motivos de no-entrega.

14 motivos: 11 del catalogo Falabella + 3 internos.
Cada motivo tiene descripcion + reglas de desambiguacion para el LLM.
"""
from __future__ import annotations

MOTIVOS: list[dict[str, str | bool]] = [
    {"codigo": "SIN MORADORES", "descripcion": "Al llegar, no hay nadie que reciba el paquete. Cliente ausente, inmueble deshabitado, reagenda.", "alertable": False, "severity": "low"},
    {"codigo": "NO CONOCEN A CLIENTE", "descripcion": "Las personas en la direccion no conocen al destinatario.", "alertable": False, "severity": "low"},
    {"codigo": "PROBLEMA DE DIRECCION/ SIN INFORMACION", "descripcion": "Direccion incorrecta, incompleta o imposible de localizar.", "alertable": True, "severity": "medium"},
    {"codigo": "NO DESPACHA A LOCALIDAD", "descripcion": "Direccion fuera de la zona que la empresa atiende.", "alertable": False, "severity": "low"},
    {"codigo": "FUERA DE COBERTURA/ FRECUENCIA", "descripcion": "Zona fuera del alcance de cobertura o frecuencia de visita.", "alertable": False, "severity": "low"},
    {"codigo": "PROD NO ENTREGADO POR TIEMPO", "descripcion": "No se pudo entregar dentro del tiempo limite. Trafico, demoras.", "alertable": False, "severity": "low"},
    {"codigo": "PRODUCTO NO CARGADO", "descripcion": "Paquete no fue cargado en el vehiculo desde el origen.", "alertable": True, "severity": "high"},
    {"codigo": "CLIENTE RECHAZA", "descripcion": "Cliente rechaza recibir el paquete.", "alertable": False, "severity": "low"},
    {"codigo": "SINIESTRO EN CALLE", "descripcion": "Accidente, manifestacion, cierre de calles o clima adverso.", "alertable": True, "severity": "critical"},
    {"codigo": "PRODUCTO CON PROBLEMAS", "descripcion": "Producto con defectos, danos o problemas.", "alertable": False, "severity": "medium"},
    {"codigo": "NO CUMPLE CONDICIONES RETIRO", "descripcion": "Condiciones inadecuadas para entrega/retiro. Falta espacio, acceso.", "alertable": False, "severity": "low"},
    {"codigo": "PRODUCTO ROBADO", "descripcion": "Paquete robado durante el proceso de entrega.", "alertable": True, "severity": "critical"},
    {"codigo": "RIESGO FRAUDE", "descripcion": "Pedido sospechoso de fraude. NO entregar, reportar inmediatamente.", "alertable": True, "severity": "critical"},
    {"codigo": "DETENCION URGENTE", "descripcion": "Detencion ordenada por Falabella. NO entregar, devolver al CD.", "alertable": True, "severity": "high"},
]

DESAMBIGUACION = """Reglas de desambiguacion para clasificar motivos:
- SIN MORADORES: solo si nadie atiende. Si el problema es la direccion -> PROBLEMA DE DIRECCION. Si rechazan -> CLIENTE RECHAZA. Si no conocen -> NO CONOCEN A CLIENTE.
- NO CONOCEN A CLIENTE: solo si hay personas pero no conocen al destinatario. No si nadie habia -> SIN MORADORES.
- PROBLEMA DE DIRECCION: direccion mala, no existe, incompleta. No si la zona no se atiende -> NO DESPACHA A LOCALIDAD.
- CLIENTE RECHAZA: solo si el cliente atendio y rechaza. Si anula por direccion mala -> PROBLEMA DE DIRECCION.
- SINIESTRO EN CALLE: accidente, manifestacion, clima. No si fue por tiempo -> PROD NO ENTREGADO POR TIEMPO.
- PRODUCTO NO CARGADO: nunca subio al camion. No si no llego a tiempo -> PROD NO ENTREGADO POR TIEMPO.
- RIESGO FRAUDE: datos sospechosos, RUT clonado. No si solo rechaza -> CLIENTE RECHAZA.
- DETENCION URGENTE: orden de Falabella/transporte. No si el cliente rechaza -> CLIENTE RECHAZA.
"""
