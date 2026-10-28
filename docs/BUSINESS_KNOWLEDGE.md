# Conocimiento y continuidad del análisis

## Cambios revisados el 28 de octubre de 2026

Se revisaron los comentarios, la transcripción y el Excel organizacional entregados por David Centeno. Se incorporaron reglas explícitas; las cifras de aquella conversación no se guardaron como resultados reutilizables.

| Concepto | Regla vigente |
| --- | --- |
| Prima emitida | `SUM(vrprima)`, por `fecha_emision`, con signos y movimientos completos. |
| Pagos | `SUM(incurrido_total)` con `tipo = Pagos`; por `fecha_accidente` salvo petición explícita de `fecha_pago`. Honorarios e indemnizaciones mantienen esa base temporal. |
| Reservas y avisos | Se conservan las reglas anteriores por `mes_mmalla`; no se cambia a accidente ni pago. |
| Prima devengada | Función interna `analytics-487218.functions.prima_devengada`; vigencias solapadas con cada mes, incluidas emisiones anteriores. |
| Siniestralidad interna | Pagos / prima devengada × 100. El denominador emitido requiere solicitud explícita. |
| Área | Línea mayor: `linea_mayor` en primas; `lob_l2` en siniestros. LOB1 es VP y LOB3 línea menor. |
| Copropiedades | Clasificación organizacional: código local 34, VP Generales, línea mayor/menor Copropiedades, MOR PYMES. Un ramo comercial explícito sigue siendo un filtro de ramo. |
| Ramo numérico | «Ramo 201» filtra únicamente el código comercial `cod_ramo_com`, con parámetro del tipo real de la columna. No añade un filtro de nombre con el valor «201». Una corrección a código reemplaza el filtro de nombre previo y conserva el identificador y el periodo. Los códigos técnico y contable se mantienen separados. |
| Últimos seis meses | Seis meses calendario incluyendo el actual, hasta hoy; el actual se identifica como parcial. «Meses cerrados» excluye el actual. |

Estas son definiciones internas revisadas para esta POC. No convierten automáticamente pagos en siniestralidad incurrida, reservas agregadas en saldos actuariales ni importes de la fuente en una moneda determinada.

## Cómo se calcula

1. El modelo propone `run_analysis` con las métricas solicitadas. Python resuelve el periodo y la clasificación del usuario.
2. Se conserva un análisis estructurado por sesión: métricas, fechas, población, filtros, agrupación y base temporal de pagos. Un reintento conserva sus fechas originales; una petición nueva y `/new` crean otro alcance.
3. Se comprueban los campos y la firma real de la función por `get_routine`; si esa API no está disponible, se consultan `INFORMATION_SCHEMA.PARAMETERS` y `ROUTINES`. Se conserva el orden real de sus cinco argumentos, sin adivinarlo. La firma SQL interna se almacena en caché una hora.
4. La devengada se evalúa por cada mes en una sola consulta. El final exclusivo del periodo se convierte al final inclusivo que usa esta función. La prima emitida tiene su filtro propio; el de emisión no recorta la devengada.
5. Cada métrica se agrega antes de combinar los resultados. No se unen los movimientos individuales de primas y siniestros entre sí ni con las 98 filas originales del Excel.
6. Python produce el resumen y la tabla directamente. La ruta normal requiere una propuesta del modelo y un trabajo de datos; el descubrimiento de firma o clases puede agregar consultas la primera vez. Las consultas exploratorias conservan el ciclo de herramientas.

Si la firma o una columna necesaria no se puede verificar, se conservan los componentes calculables y se identifica el resultado parcial. Un error de BigQuery relacionado expresamente con la rutina también permite devolver los otros componentes. Otros errores de ejecución todavía requieren corregir la consulta o resolver el acceso; no se inventan resultados.

`NULL`, cero, ausencia de movimientos y denominador cero se distinguen. Los porcentajes se calculan en SQL y se presentan con `%`; las columnas auxiliares de control no aparecen en la tabla. Las consultas de diagnóstico no reemplazan la tabla principal. La auditoría conserva SQL, parámetros y trabajos de BigQuery. El aprendizaje queda en la cola existente, fuera del tiempo de respuesta.

## Catálogo mantenible

Los datos revisados se incluyen en `src/analytics_agent/data/organization_catalog.json`, separados del Excel opcional de descripciones de columnas. El archivo contiene una dimensión con un registro por código local y un puente subprofit–código local. El código subprofit 51 **no equivale** al código local 51; por ejemplo, se relaciona con Copropiedades mediante código local 34.

Para preparar una versión nueva desde un Excel con los seis encabezados originales:

```bash
python -m analytics_agent.organization /ruta/clasificacion.xlsx \
  --output /ruta/organization_catalog.candidato.json
```

Revisa el archivo candidato antes de sustituir el catálogo incluido y versionarlo en Git. El importador rechaza códigos locales con clasificaciones contradictorias. El catálogo actual no contiene fechas de vigencia histórica: no prueba cómo se clasificaban las áreas en años anteriores.

El grafo local relaciona métricas, componentes, función, códigos y jerarquías, con procedencia. Se regenera al cambiar la versión del conocimiento. Los comentarios o aprendizajes personales no publican nuevas fórmulas compartidas.

## Contrastar en Workbench

Actualiza el clon e instala desde la terminal del runtime:

```bash
git pull --ff-only
source .venv/bin/activate
python -m pip install -e .
analytics-agent chat
```

Prueba en una conversación nueva:

```text
Pagos, prima emitida, prima devengada y siniestralidad de Copropiedades en septiembre de 2026
Usa fecha_pago
Vuelve a intentarlo
/new
Pagos, prima emitida, prima devengada y siniestralidad de Copropiedades de los últimos seis meses
Exporta el resultado a Excel
```

Comprueba que la primera consulta use código local 34 en primas y el alcance equivalente comprobado en siniestros; que el seguimiento solo cambie la base temporal de pagos; y que el reintento mantenga fechas y población. Contrasta importes con una consulta de referencia revisada por negocio y comprueba la firma de la UDF disponible en tu runtime.

Las pruebas del repositorio son locales con clientes simulados y un motor SQLite independiente para los cálculos. Verifican los periodos, joins, filtros, porcentajes y continuidad; **no certifican la fórmula ni las cifras reales de la UDF de producción**. No se ejecutaron consultas reales para desarrollar estos cambios.
