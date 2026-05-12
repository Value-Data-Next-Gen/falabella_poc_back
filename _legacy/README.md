# `_legacy/` — código backend deprecado

Archivos movidos acá en la limpieza de R7. Ninguno está importado por
`main.py` y Python no descubre módulos automáticamente, así que estos
archivos no se cargan al arrancar uvicorn.

| Archivo                          | Motivo                                                  |
| -------------------------------- | ------------------------------------------------------- |
| `qa_audit.py`                    | Endpoints de QA manual (auditoría). No registrado en `main.include_router`. |
| `qa_persistencia.py`             | Endpoints de QA de persistencia DB.                     |
| `qa_whatsapp.py`                 | Endpoints de QA del flujo WA.                           |
| `scripts/test_e2e.py`            | Script de prueba end-to-end legacy.                     |
| `scripts/test_integral_flow.py`  | Otro script de prueba integral.                         |

Si necesitás recuperar alguno, moveló de vuelta a `backend/` y registrá su
router en `main.py` (`app.include_router(...)`).
