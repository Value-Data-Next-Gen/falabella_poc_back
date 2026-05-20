# Bulk onboarding por Excel

Tooling para sumar usuarios masivos al canal WhatsApp del POC. El cliente
llena la planilla, ValueData corre el script y devuelve los activation
links listos para compartir.

## Archivos
- `build_onboarding_template.py` — script que genera `onboarding_template.xlsx`. Correr una vez.
- `onboarding_template.xlsx` — la planilla pre-armada para entregar al cliente.
- `bulk_onboard.py` — script que procesa la planilla llena y crea las personas vía API.

## Workflow
1. Ejecutar `python build_onboarding_template.py` una vez (ya hecho; el `.xlsx` queda en este dir).
2. Entregar `onboarding_template.xlsx` al cliente, decirle que llene la pestaña **Personas**.
3. Cuando devuelva el archivo, correr:
   ```
   pip install openpyxl requests
   python bulk_onboard.py <ruta-archivo-lleno>.xlsx \
     --admin-email admin@falabella.cl \
     --admin-password admin123
   ```
4. El script crea cada persona, genera el wa.me link y lo escribe en la columna **Activation Link**.
5. Devolver el archivo con la columna llena al cliente.
6. Cada persona hace click en SU link, manda el mensaje pre-rellenado, queda activa.

## Tipos soportados
- **user** — Acceso a la app web. Roles: `falabella_admin`, `falabella_ops`, `transport_manager`.
- **driver** — Conductor. Solo WhatsApp. Necesita empresa + vehículo.
- **contacto** — Recipient extra de alertas por empresa. Solo WhatsApp. Roles: `coordinador`, `dispatcher`, `jefe`, `otro`.

## Modo dry-run
```
python bulk_onboard.py archivo.xlsx --dry-run
```
Valida la planilla sin crear nada. Útil para chequear formato antes de tirarse a producción.

## Caveats
- La planilla tiene 1 fila de ejemplo en cada tipo (en gris cursiva). El script las saltea automáticamente; si querés borrarlas, dale.
- Si una fila falla, el script marca la celda en rojo con el error y sigue con las siguientes (no aborta).
- Filas exitosas quedan en verde con el link como hyperlink clickeable.
- Filas saltadas (ejemplo o vacías) quedan en amarillo o sin marcar.
