"""Classifier de motivos de no-entrega.

Replica el system prompt y la lógica del notebook auditoria_llm_directo.ipynb.
- Si hay creds Azure OpenAI -> usa el LLM (gpt-4o-mini, JSON estricto).
- Si no -> fallback determinístico por keywords (siempre devuelve algo).

Endpoint: POST /api/motivos/classify
"""
from __future__ import annotations

import json as _json
import os
import re
import unicodedata
from typing import Optional

from fastapi import APIRouter, Depends
from loguru import logger
from pydantic import BaseModel, Field

from auth import CurrentUser, current_user
from comments import (
    MOTIVOS_CATALOGO,
    _default_description,
    _resolve_alert_config,
    _resolve_description,
)


router = APIRouter(tags=["motivo-classifier"])


def _build_manual(empresa_id: Optional[int] = None) -> str:
    """Construye el bloque de reglas operacionales leyendo descripciones desde DB
    (override admin) con fallback al default del catálogo."""
    bloques: list[str] = []
    for motivo in MOTIVOS_CATALOGO:
        desc, _is_custom = _resolve_description(motivo, empresa_id)
        if not desc:
            desc = _default_description(motivo)
        bloques.append(f"## {motivo}\n{desc.strip()}")
    return "\n\n".join(bloques)


def _build_system_prompt(empresa_id: Optional[int] = None) -> str:
    manual = _build_manual(empresa_id)
    schema_hint = '{"motivo_correcto": "NOMBRE_EXACTO", "confianza": "alta|media|baja", "razonamiento": "1 frase corta"}'
    return (
        "Sos un auditor experto en operaciones de transporte y entrega en Chile. "
        "Clasificas motivos de no-entrega leyendo el comentario del operador y "
        "aplicando un manual operacional.\n\n"
        "REGLAS OPERACIONALES:\n\n"
        f"{manual}\n\n"
        "INSTRUCCIONES:\n"
        "1. Lee el COMENTARIO con atencion. Identifica la CAUSA RAIZ, no el evento final. "
        "Ejemplo: 'cliente anulo porque la direccion estaba mal' -> la causa raiz es la direccion, no el rechazo.\n"
        "2. Elegi UN motivo de la lista usando el manual.\n"
        "3. Responde SOLO en JSON valido sin texto adicional, con esta estructura exacta:\n"
        f"{schema_hint}\n\n"
        "4. Si el comentario es ambiguo o muy breve, usa confianza='baja'.\n"
        "5. NO inventes motivos. Solo elegi entre los listados.\n"
    )


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_KEYWORDS: dict[str, list[str]] = {
    "SINIESTRO EN CALLE": [
        "choque", "accidente", "encerrona", "asalto", "carabineros",
        "panne", "robo en ruta", "siniestro", "atropell", "colision",
    ],
    "PRODUCTO ROBADO": [
        "producto robado", "carga robada", "sustraido", "sustra",
        "robaron", "robo de carga", "robo total",
    ],
    "PRODUCTO NO CARGADO": [
        "no cargado", "no fue cargado", "quedo en bodega", "no subio al camion",
        "olvidado en origen", "no salio en la ruta", "no se cargo",
    ],
    "PRODUCTO CON PROBLEMAS": [
        "roto", "danado", "embalaje", "faltante", "incompleto",
        "caja abierta", "deteriorado", "rotura", "averiado",
    ],
    "PROBLEMA DE DIRECCION/ SIN INFORMACION": [
        "direccion erronea", "direccion mal", "direccion incorrecta",
        "sin numeracion", "no existe", "no ubicable", "mal geolocaliz",
        "calle no existe", "no encontre la direccion", "vecinos no conocen",
    ],
    "NO DESPACHA A LOCALIDAD": [
        "no despacha", "fuera de mi zona", "no atendemos", "comuna no atendida",
        "fuera de zona de despacho",
    ],
    "FUERA DE COBERTURA/ FRECUENCIA": [
        "fuera de cobertura", "fuera de ruta", "frecuencia", "no alcanzo a llegar",
    ],
    "PROD N ENTREGADO X TIEMPO": [
        "fin de jornada", "fin de turno", "se acabo el tiempo", "no alcance",
        "atraso", "tarde", "horario cumplido",
    ],
    "CLIENTE RECHAZA ENVIO": [
        "cliente rechaza", "no quiere recibir", "anulo compra", "anulo el pedido",
        "devuelve", "cancela", "rechazo el envio",
    ],
    "SIN MORADORES": [
        "no atiende", "no responde", "nadie en", "sin moradores", "no hay nadie",
        "timbre", "buzon", "luces apagadas", "no contesta",
    ],
    "NO CUMPLE CONDICIONES RETIRO": [
        "sin rut", "no acredita", "sin autorizacion", "falta documentacion",
        "sin documento", "no se identifica",
    ],
}


def _classify_keywords(comentario: str) -> dict:
    text = _normalize(comentario)
    scores: dict[str, int] = {}
    matched: dict[str, list[str]] = {}
    for motivo, patterns in _KEYWORDS.items():
        hits = [p for p in patterns if p in text]
        if hits:
            scores[motivo] = len(hits)
            matched[motivo] = hits
    if not scores:
        return {
            "motivo": "SIN MORADORES",
            "confianza": "baja",
            "razonamiento": "Sin keywords detectadas; default conservador. Revisar manual.",
            "fallback": True,
        }
    best = max(MOTIVOS_CATALOGO, key=lambda m: (scores.get(m, 0), -MOTIVOS_CATALOGO.index(m)))
    n = scores[best]
    confianza = "alta" if n >= 2 else "media"
    razon = f"Match por keywords: {', '.join(matched[best][:3])}"
    return {
        "motivo": best,
        "confianza": confianza,
        "razonamiento": razon,
        "fallback": True,
    }


def _classify_llm(comentario: str, empresa_id: Optional[int] = None) -> Optional[dict]:
    # Soportar tanto los nombres oficiales (AZURE_OPENAI_*) como los cortos
    # (AZURE_*) que aparecen en el notebook auditoria_llm_directo.
    endpoint = (
        os.environ.get("AZURE_OPENAI_ENDPOINT")
        or os.environ.get("AZURE_ENDPOINT")
        or ""
    ).strip().strip('"').strip("'")
    api_key = (
        os.environ.get("AZURE_OPENAI_API_KEY")
        or os.environ.get("AZURE_API_KEY")
        or ""
    ).strip().strip('"').strip("'")
    deployment = (
        os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT")
        or os.environ.get("AZURE_CHAT_DEPLOYMENT")
        or "gpt-4o-mini"
    ).strip().strip('"').strip("'")
    api_version = (
        os.environ.get("AZURE_OPENAI_API_VERSION")
        or os.environ.get("AZURE_API_VERSION")
        or "2024-08-01-preview"
    ).strip().strip('"').strip("'")
    if not endpoint or not api_key:
        logger.warning(f"[classify] sin creds Azure (endpoint set={bool(endpoint)}, key set={bool(api_key)})")
        return None
    try:
        from openai import AzureOpenAI
        client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
        )
        system_prompt = _build_system_prompt(empresa_id)
        resp = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"COMENTARIO: {comentario}\n\nResponde en JSON con tu clasificacion segun el manual."},
            ],
            temperature=0,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        contenido = resp.choices[0].message.content.strip()
        # Workaround: algunos endpoints devuelven UTF-8 mal interpretado
        # como latin-1 ("anuló" -> "anulÃ³"). Si detectamos ese patrón,
        # recodificamos. Si no aplica, queda igual.
        try:
            fixed = contenido.encode("latin-1").decode("utf-8")
            if "Ã" in contenido and "Ã" not in fixed:
                contenido = fixed
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
        data = _json.loads(contenido)
        motivo = (data.get("motivo_correcto") or "").strip()
        if motivo not in MOTIVOS_CATALOGO:
            norm = lambda s: re.sub(r"[^\w\s]", "", s).lower().strip()
            match = next((m for m in MOTIVOS_CATALOGO if norm(m) == norm(motivo)), None)
            if match is None:
                return None
            motivo = match
        return {
            "motivo": motivo,
            "confianza": (data.get("confianza") or "media").lower(),
            "razonamiento": str(data.get("razonamiento") or "")[:300],
            "fallback": False,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[classify] LLM fallo: {e}")
        return None


class ClassifyRequest(BaseModel):
    comentario: str = Field(min_length=1, max_length=2000)


class ClassifyResponse(BaseModel):
    motivo: str
    confianza: str
    razonamiento: str
    fallback: bool
    alertable: bool
    severity: str


@router.post("/api/motivos/classify", response_model=ClassifyResponse)
def classify(req: ClassifyRequest, user: CurrentUser = Depends(current_user)) -> ClassifyResponse:
    empresa_id = None if user.is_falabella else user.empresa_id
    result = _classify_llm(req.comentario, empresa_id) or _classify_keywords(req.comentario)
    alertable, severity = _resolve_alert_config(result["motivo"], empresa_id)
    return ClassifyResponse(
        motivo=result["motivo"],
        confianza=result["confianza"],
        razonamiento=result["razonamiento"],
        fallback=bool(result["fallback"]),
        alertable=alertable,
        severity=severity,
    )


class SystemPromptResponse(BaseModel):
    system_prompt: str
    empresa_id: Optional[int] = None
    has_llm_creds: bool


@router.get("/api/motivos/system-prompt", response_model=SystemPromptResponse)
def get_system_prompt(user: CurrentUser = Depends(current_user)) -> SystemPromptResponse:
    """Devuelve el system prompt construido con las descripciones actuales.
    Útil para que el admin previsualice exactamente qué le va al LLM."""
    empresa_id = None if user.is_falabella else user.empresa_id
    has_creds = bool(
        os.environ.get("AZURE_OPENAI_ENDPOINT") or os.environ.get("AZURE_ENDPOINT")
    ) and bool(
        os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("AZURE_API_KEY")
    )
    return SystemPromptResponse(
        system_prompt=_build_system_prompt(empresa_id),
        empresa_id=empresa_id,
        has_llm_creds=has_creds,
    )
