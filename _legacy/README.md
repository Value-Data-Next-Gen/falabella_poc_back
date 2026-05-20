# `_legacy/` — código backend deprecado

Archivos movidos acá en la limpieza de R7. Ninguno está importado por
`main.py` y Python no descubre módulos automáticamente, así que estos
archivos no se cargan al arrancar uvicorn.

Los scripts `qa_*.py` y `scripts/test_*.py` fueron eliminados en el CR
fixes-qa porque contenían números de teléfono reales (PII) en los
fixtures de prueba. Si querés reescribirlos, asegurate de usar phones
sintéticos (`+5690000000X`) y emails `*@example.cl`.
