# Validación de conceptos

El corpus de 120 preguntas está en `scripts/validation/corpus.tsv`. La primera
columna contiene el turno y la segunda su comprobación. El número de línea es
el identificador permanente. Añade variantes al final en futuras iteraciones.
La lista `LIVE` define los 36 casos con BigQuery. Los otros 84 usan el proveedor
real y un adaptador controlado.

Desde la terminal de Workbench, ejecuta ambas pistas.

```bash
python -m scripts.validate_concepts --execute --lane live --report conceptos-bq.json
python -m scripts.validate_concepts --execute --lane fixture --report conceptos-controlados.json
python scripts/validation/summarize.py conceptos-bq.json conceptos-controlados.json --output hallazgos.csv
```

Repite un caso con `--case ID`. El runner incluye los turnos previos de las
secuencias de ramo, fechas de pagos y aclaración de periodo. Escribe a otro
reporte y añade ese archivo al final del comando de resumen para conservar la
historia. Para una repetición con BigQuery, calcula el saldo hasta 10 GiB con
`estimated_bytes_reserved` y pásalo a `--budget-bytes`.

El registro detallado se queda en Workbench. Revisa primero las diferencias de
fuente, fecha, filtros e importes. Distingue un fallo de la expectativa o del
adaptador controlado de un fallo del agente. En los casos explicativos un
resultado de la rúbrica es una señal para revisión. Las pruebas unitarias
complementan las preguntas sobre recuperación, permisos de sesión, feedback y
exportación, cuyo comportamiento no se comprueba con una explicación textual.

El SQL que llama a la misma función de prima devengada comprueba la integración
de la función. Los importes de pagos y los saldos de siniestros requieren un
cierre oficial para su conciliación financiera.

La fecha relativa de los casos controlados se fija al 17 de noviembre de 2026.
Los informes indican el modelo y las huellas del corpus, runner y prompt.

La documentación de octubre sobre `incurrido_total`, `tipo = Pagos` y
`mes_mmalla` describe una versión anterior. Para esta validación se usan las
[convenciones v6](CLAIMS_V6.md), el esquema accesible y la evidencia del cierre
cuando esté disponible.
