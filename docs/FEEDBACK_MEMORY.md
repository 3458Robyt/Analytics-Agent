# Feedback, conversaciones y conocimiento

Estas funciones están disponibles en la CLI de Workbench. Los mismos comandos y tarjetas quedan preparados para Teams cuando se despliegue la integración. No requieren una base de datos externa ni nuevos permisos de Google Cloud o Microsoft Graph.

## Uso desde Workbench

Actualiza el código y conserva la misma carpeta de estado:

```bash
git pull --ff-only
source .venv/bin/activate
python -m pip install -e .
analytics-agent chat
```

Comandos dentro del chat:

| Comando | Resultado |
| --- | --- |
| `/feedback La respuesta debería incluir el total` | Guarda el comentario asociado a la última respuesta, cuando existe. |
| `/feedback` | Muestra cómo enviar el comentario; en Teams abre un formulario. |
| `/new` o `/clear` | Inicia otra conversación y conserva el historial y las preferencias. |
| `/sessions` | Lista tus conversaciones, de diez en diez. |
| `/resume ID` | Retoma una conversación y sus aclaraciones pendientes. |
| `/history ID` | Muestra preguntas, respuestas y tablas resumen, un turno por página. |
| `/history ID 1` | Abre la segunda página; las páginas empiezan en cero. |
| `/help` | Muestra los comandos disponibles. |

Los comandos de navegación y feedback no llaman al modelo ni a BigQuery. Puedes utilizarlos mientras hay una aclaración pendiente. `/new` deja esa consulta en su conversación anterior; `/resume ID` permite volver a ella. `/cancel` cancela la captura de feedback, no la consulta pendiente. En la CLI, `/exit` termina el chat.

La identidad de la CLI corresponde a la cuenta del sistema operativo. En Teams corresponde al tenant y al usuario autenticado; cada persona ve únicamente sus conversaciones. Una identidad de Teams y una cuenta de Workbench no se vinculan automáticamente. Compartir una misma cuenta de sistema comparte su historial local.

## Consultas sin fechas

1. Se usa el periodo indicado por la persona o el último periodo explícito de esa misma conversación.
2. Si no existe, se prepara una consulta sin inventar fechas y se estima con un dry run.
3. Si supera el umbral de 1 GiB o el volumen es desconocido, se pregunta por las fechas antes de ejecutar la consulta.
4. Puedes responder «enero de 2026» o «todo el histórico». Una respuesta que no resuelva el alcance mantiene la pregunta pendiente.

«Incluye todos los movimientos» no equivale a solicitar todos los años. La confirmación del histórico no crea una nueva definición financiera. El umbral controla cuándo pedir un periodo; no es un presupuesto máximo de facturación. Un periodo indicado o el histórico confirmado pueden superar ese volumen. Las validaciones de solo lectura y de las métricas revisadas se mantienen; no se introduce un límite de 50 filas.

## Revisar feedback y rendimiento

Ejecuta estos comandos en la terminal de Workbench, fuera del chat:

```bash
analytics-agent feedback list --status new
analytics-agent feedback show ID_FEEDBACK
analytics-agent feedback resolve ID_FEEDBACK --note "Corregido y verificado"
analytics-agent feedback list --json
analytics-agent usage report --days 30
analytics-agent usage report --days 30 --json
```

El feedback tiene categoría, autor, canal, conversación, turno, estado y resolución. En Teams el formulario permite seleccionar sugerencia, error, corrección de cifras u otro. La retransmisión de una misma solicitud no registra dos comentarios. El comando anterior `analytics-agent teams feedback list` sigue disponible como alias de consulta.

El reporte de uso registra solicitudes, estado, tiempo total y por etapa, tokens reportados por el proveedor, consultas ejecutadas, bytes, reintentos por patrón y errores. Conserva versiones del modelo, prompt y definiciones. Agrupa SQL por su estructura sustituyendo literales, de modo que las fechas o valores diferentes no fragmenten el patrón. Las cifras de bytes y tokens permiten analizar consumo; no representan una factura monetaria. Los comandos administrativos consultan el estado local de todos los usuarios del worker y no están disponibles desde Teams ni como herramientas del modelo.

## Retención y actualización del estado

Añade o ajusta en `.env`:

```dotenv
ANALYTICS_AGENT_HISTORY_RETENTION_DAYS=90
ANALYTICS_AGENT_RESULT_RETENTION_HOURS=24
BQ_UNSCOPED_QUERY_THRESHOLD_BYTES=1073741824
```

Los valores deben ser enteros positivos. La carpeta por defecto es `~/.local/share/analytics-agent`; puedes fijarla con `ANALYTICS_AGENT_STATE_DIR`. Guarda esa carpeta en el disco persistente de Workbench.

| Información | Plazo local predeterminado |
| --- | --- |
| Preguntas, respuestas, tablas resumen, acciones, SQL, feedback y métricas | 90 días |
| Filas completas y copias de tarjetas en la caché de resultados | 24 horas |
| Recibos de entrega de Teams sin resultados, para evitar repetir una solicitud | 7 días |
| Preferencias, procedimientos aprendidos y glosario revisado | Se conservan hasta administrarlos o cambiar su versión. |

La limpieza se realiza al abrir el estado y durante el uso. El historial conserva la respuesta y su tabla resumen aunque las filas completas hayan vencido; para analizarlas o exportarlas de nuevo será necesario repetir la consulta. Los archivos Excel ya exportados y los mensajes que Microsoft Teams haya entregado siguen sus propios ciclos de retención. Estos plazos no los eliminan.

La actualización añade tablas a SQLite; no requiere borrar el estado existente. El feedback de la tabla antigua de Teams se copia una sola vez al registro unificado si está dentro del plazo vigente. Antes de eliminar evidencias de conversaciones vencidas, se conservan sus conteos para no reiniciar la validación de procedimientos aprendidos. La primera apertura aplica la retención configurada también a los registros anteriores; haz una copia de la carpeta de estado con los procesos detenidos si necesitas conservarlos para otra finalidad.

## Grafo y aprendizaje

```bash
analytics-agent knowledge search "pagos y reservas"
analytics-agent learn list --status proposed
```

El grafo SQLite conecta concepto, tabla, importe, fecha, dimensiones y filtros obligatorios. Cada relación incluye procedencia, estado validado y versión del glosario. Se reconstruye cuando cambia `business_glossary.json`; el modelo recibe relaciones relevantes y puede buscar otras mediante `search_knowledge`.

El grafo compartido se deriva únicamente de definiciones revisadas. Las preferencias explícitas siguen aprendiendo de manera personal y los procedimientos siguen su proceso de verificación. Una aclaración o un feedback no publica una fórmula: las nuevas reglas financieras necesitan revisión y un cambio versionado del glosario en Git. Esto permite mejorar la memoria sin cambiar importes por una enseñanza incorrecta.

## Teams

Actualiza tanto el worker como el gateway siguiendo [TEAMS_PILOT.md](TEAMS_PILOT.md). Reinicia el worker conservando su carpeta de estado. El gateway actualizado recibe las acciones nuevas de formularios y navegación; un worker nuevo con un gateway antiguo no dispone de todos los botones.

El usuario puede escribir los mismos comandos en el chat privado, pulsar **Mis conversaciones**, retomar o leer una conversación, abrir su tabla resumen y enviar feedback desde una tarjeta. Estas funciones no añaden permisos de Microsoft Graph. No se presenta una barra lateral de historial como la de ChatGPT. La integración necesita el despliegue y permisos de Teams ya descritos en la guía; el uso local en Workbench continúa de forma independiente.

## Validación

```bash
PYTHONPATH=src python3 -m unittest discover -q
```

Las pruebas usan BigQuery y modelos simulados, y SQLite temporal. Incluyen aislamiento de usuarios, repetición de solicitudes, migración de feedback, expiración de resultados e historial, conservación de aprendizaje y el bloqueo de ejecución por falta de fechas. La validación en el tenant de Teams y con las tablas reales se realiza después del despliegue.
