# Ciclo de mejora en Workbench

Cada versión candidata tiene su propia validación. Usa la terminal de Workbench
y la configuración del piloto. GitHub Actions sigue ejecutando las pruebas
locales con Python 3.10 y 3.12.

## Revisar feedback

1. Ejecuta `analytics-agent feedback list --status new` y
   `analytics-agent usage report --days 30`. Prioriza cifras incorrectas, bloqueos,
   errores de contexto repetidos y luego rendimiento.
2. Consulta `analytics-agent feedback show ID` y el turno asociado desde Workbench
   cuando exista. Reduce el problema a preguntas desidentificadas y a una
   condición comprobable. Usa una prueba local para fallos de lógica y un caso
   de Workbench cuando intervengan el modelo, el esquema o los datos.
3. Crea una rama y valida cada versión candidata. Incluye el identificador del
   caso, el cambio y el resumen de verificación en el PR. Conserva los detalles
   dentro de Workbench. Tras verificar que se usa la versión corregida, ejecuta
   `analytics-agent feedback resolve ID --note "Versión verificada …"`.

## Comparar con main

Desde el checkout candidato, usa el mismo intérprete y configuración para las
dos versiones. El reporte se guarda en un directorio existente de Workbench.

```bash
git fetch origin main
git worktree add --detach ../analytics-agent-base origin/main
python -m pip check
python -m unittest discover -s tests -q
python scripts/validate_workbench.py --execute \
  --baseline ../analytics-agent-base --env-file /ruta/al/.env \
  --report /ruta/local/comparacion.json
```

La comparación usa LangGraph en procesos separados y sesiones temporales.
Los dos procesos comparten un límite de 10 GiB estimados. El reporte marca
fallos de aserción, infraestructura y presupuesto, y registra consultas, bytes
procesados, tokens y tiempos por etapa. La mediana compara solo casos que
pasaron en las dos versiones. Ante un aumento de latencia, repite el escenario
con la misma configuración y `--case ID` antes de atribuirlo al cambio. Cuenta la repetición
dentro del mismo presupuesto de validación usando `--budget-bytes` con el saldo del informe.
Un PR con fallos de la candidata o validación técnica incompleta queda en
borrador. El equipo revisa y decide el merge.

Para conciliar siniestros ejecuta `--diagnose --execute`. El reporte perfila
`mm_final_netos` y contrasta los pagos con el criterio anterior de `mm_final`.
La concordancia con SQL independiente valida la implementación. La aceptación
financiera requiere conciliar el periodo, la fuente, el grano y el importe con
un cierre oficial. Usa `--reviewed-claims --execute` cuando exista un contrato
temporal revisado y conserva la evidencia del cierre en Workbench.
