# Convenciones de siniestros, versión 6

Confirmadas por el usuario con su equipo en noviembre de 2026.

- Fuente preferida: centralizacion-datos.analytics_curated.mm_final_netos. No necesita tipo ni cod_tipo.
- Gross por defecto. Pagos: pago_gross; netos explícitos: pago_neto. Son movimientos del mes.
- Fecha de pagos: fecha_pago. Ocurrencia explícita: fecha_accidente.
- Honorarios e indemnizaciones: etiquetas observadas de cod_estimacion. HONORARIOS Y GASTOS contiene ambos conceptos.
- Reserva actual bruta: reserva_gross; anterior: reserva_gross_anterior. Netas: reserva_actual_neto y reserva_anterior_neto.
- Variación bruta aprobada: reserva_gross - reserva_gross_anterior. Incurrido bruto: pago_gross + variación. Netos: columnas variacion_neto e incurrido_neto.
- Corte mensual todavía pendiente: esos componentes devuelven resultado parcial y no inventan fechas. Reservas multimés deben ser una serie, sin sumar saldos entre meses.
- Desgloses incluyen total de las filas completas, calculado antes del redondeo. Los totales son de presentación y no vuelven a alimentar estadísticas sobre los resultados.
- El catálogo dictionary_v3.json contiene las descripciones del Excel aportado. BigQuery determina existencia y tipos reales. El archivo Excel original permanece local.

## Validación en desarrollo

La batería local cubre pagos brutos y netos con ambos motores, regresiones de conversación, recuperación, CLI y diccionario. Para validar importes de producción falta ejecutar la conciliación con datos reales en Workbench.

## Diagnóstico y conciliación en Workbench

Desde la terminal de Workbench, con el agente instalado y las credenciales de
BigQuery disponibles, revisa el script y ejecuta las consultas de lectura con
`--execute`:

```bash
python scripts/validate_workbench.py --diagnose --execute --report claims-diagnostic.json
```

El reporte local incluye el esquema, los campos de fecha candidatos, las sumas de
pagos por mes y categoría, los importes nulos y los signos. Compara filas y llaves
distintas cuando existe `llave`. Se puede repetir con
`--period-candidate NOMBRE_DEL_CAMPO` para perfilar campos de cierre que aparezcan en
el esquema. El primer paso usa solo BigQuery y no requiere credenciales del modelo.
El diagnóstico usa septiembre y octubre de 2026 y comparte un presupuesto de
estimaciones de 10 GiB entre sus consultas.

Contrasta el mes y el grano con un cierre o informe oficial del equipo. Las
distribuciones observadas por sí solas no prueban que una fecha sea la del cierre.
Una vez revisado, define `ANALYTICS_AGENT_CLAIMS_PERIOD_FILE` en el entorno o
en `.env` con la ruta de un JSON local. Campos:

| Campo | Valor |
|---|---|
| `mode` | `historical_movements`, `monthly_closes` o `latest_close`. |
| `start`, `end` | Inicio de cobertura e inicio del primer mes fuera de cobertura, en formato AAAA-MM-01. En `latest_close` delimitan un único mes. |
| `period_field` | Campo real del mes de corte verificado para los modos de cierre. |
| `grain` | Descripción de qué representa cada fila y su llave. |
| `reviewed_reference` | Referencia al informe o cierre con el que se reconcilió. |

Con movimientos históricos se habilitan pagos en la cobertura revisada. Los
cierres habilitan reservas, variación e incurrido para meses completos. Los
pagos sobre una fuente de cierres se validan un mes por consulta. Un contrato
ausente deja pendientes los saldos y advierte que la cobertura histórica de
pagos todavía requiere conciliación.

```bash
python scripts/validate_workbench.py --execute --report workbench-validation.json
```

La batería contrasta el SQL del agente con consultas independientes, guarda
trabajos y tiempos y exige una diferencia máxima de un centavo. Reporta
`partial` si el contrato no cubre ambos meses de aceptación o si todavía no se
pueden verificar los saldos. Un reporte parcial no certifica siniestros.
