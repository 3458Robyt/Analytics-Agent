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

Las pruebas dirigidas cubren reglas nuevas, pagos brutos/netos con ambos motores, recuperación, CLI y diccionario. La batería histórica completa aún necesita migrar sus fixtures y expectativas de las convenciones anteriores; no se considera aceptada esta entrega hasta resolver esos fallos y contrastar datos reales.
