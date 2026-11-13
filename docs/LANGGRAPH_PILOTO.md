# LangGraph en Analytics Agent

LangGraph está conectado y es el motor predeterminado de `ask`, `chat` y del worker de Teams. La instalación normal (`pip install -e .`) incluye `langgraph==1.2.20` y `langgraph-checkpoint-sqlite==3.1.3`. El extra histórico `[langgraph]` sigue funcionando. No se necesita LangSmith, una cuenta nueva ni servicios de GCP adicionales para esta integración.

## Actualizar en Workbench

En la terminal de la instancia autorizada, con el entorno virtual activo:

```bash
git fetch origin
git switch feat/langgraph-runtime-20261113
git pull --ff-only
python -m pip install -e .
python -m pip check
analytics-agent chat --engine langgraph
```

Conserva tu `.env`. No hace falta crear otro: cuando `ANALYTICS_AGENT_ENGINE` no está definido, el valor predeterminado es `langgraph`. Para fijarlo explícitamente:

```dotenv
ANALYTICS_AGENT_ENGINE=langgraph
# Opcional: debe ser un disco persistente local del runtime.
# ANALYTICS_AGENT_STATE_DIR=/home/jupyter/.local/share/analytics-agent
```

`--engine legacy` o `ANALYTICS_AGENT_ENGINE=legacy` selecciona el conductor secuencial anterior. No hay cambio automático de motor cuando falla el grafo. Ambos usan los mismos servicios de negocio; una ejecución creada por LangGraph se recupera con LangGraph.

## Ejecución y recuperación

```bash
analytics-agent ask --engine langgraph "Prima emitida por sucursal en septiembre de 2026"
analytics-agent runs list
analytics-agent runs show ID_EJECUCION
analytics-agent runs resume ID_EJECUCION
```

En el chat, `/runs` lista ejecuciones de la sesión y `/retry ID_EJECUCION` reanuda una de ellas. `/retry` sin ID usa la última de esa sesión. Si la consulta terminó, devuelve la respuesta guardada sin volver a consultar BigQuery ni llamar al modelo. Si requiere una aclaración, la pide en la misma terminal y continúa el mismo turno. Las respuestas pendientes también se pueden continuar como antes escribiendo la aclaración en el chat.

Una sesión admite una ejecución activa. Ante un error recuperable, usa `/retry` o abre otra conversación con `/new`. Para desistir definitivamente:

```bash
analytics-agent runs abandon ID_EJECUCION
```

Abandonar libera el análisis local cuando no lo está atendiendo otro proceso; **no cancela un trabajo de BigQuery ya enviado**. Una ejecución abandonada no se reanuda. El comando `runs show` muestra estado, versiones y referencias locales; la presentación `audit` conserva SQL, parámetros, trabajos, tiempos y el ID de ejecución. `business` presenta el resultado de negocio.

## Componentes y estado durable

- `turn_runtime.py` contiene los servicios compartidos del turno: contexto, decisión del modelo, compilación financiera, herramientas, revisión y presentación. El motor anterior sigue usando esos servicios.
- `durable_graph.py` conecta los nodos reales `decide`, `query`, `tool`, `review` y `clarify`. Las aclaraciones usan `interrupt()` y `Command(resume=...)`. Los checkpoints se guardan de forma síncrona.
- `execution_store.py` registra propietario, ejecución, versiones, bloqueo renovable, operaciones de BigQuery y páginas confirmadas. `graph_query.py` recupera filas desde el último offset confirmado.
- Los checkpoints contienen referencias a cargas locales comprimidas, no clientes, credenciales ni filas completas dentro del estado del grafo. Los resultados necesarios para continuar se guardan por separado en SQLite.

Una operación reserva su SQL, parámetros, proyecto, región e ID de trabajo antes de enviarla. Si el resultado de un envío es incierto, la ejecución se interrumpe: recuperar reutiliza la identidad del trabajo. Si ya se guardaron páginas, continúa desde el offset confirmado. El ID estable evita crear otro trabajo en un reintento; la llamada HTTP de envío puede repetirse y resolver el conflicto recuperando el trabajo existente. No se garantiza exactamente una llamada HTTP al proveedor de IA si el proceso cae antes de guardar su respuesta.

La escritura de cada página y su offset es atómica. Las acciones de memoria y la cola de aprendizaje admiten repetición sin duplicarse; los Excel solicitados usan un nombre determinista por ejecución/acción y se escriben mediante un reemplazo atómico. No se vuelve a aplicar un cambio de filtros o periodos solo por restaurar el grafo: se conserva el estado del análisis confirmado.

Cada ejecución comprueba propietario y huellas del modelo, prompt, glosario y catálogo organizacional antes de reanudarse. Un cambio incompatible exige iniciar otra pregunta. El bloqueo dura 120 segundos y se renueva cada 30; tras una terminación abrupta puede ser necesario esperar hasta dos minutos. Se reintentan hasta tres veces errores transitorios identificables del proveedor (408, 429, 5xx o fallos de conexión); las consultas con resultado incierto se recuperan por su ID, sin crear una operación distinta.

## Almacenamiento y límites

La carpeta predeterminada es `~/.local/share/analytics-agent`, configurable con `ANALYTICS_AGENT_STATE_DIR`. Contiene:

| Archivo | Contenido |
| --- | --- |
| `state.sqlite3` | Sesiones, memoria, feedback y estado de negocio existentes |
| `graph_executions.sqlite3` | Ejecuciones, operaciones, páginas y cargas referenciadas |
| `graph_checkpoints.sqlite3` | Checkpoints de LangGraph |

Los archivos locales tienen permisos restringidos a la cuenta del runtime. El aislamiento por propietario también se comprueba al recuperar una ejecución. No autentica personas que comparten la misma cuenta de sistema.

Los resultados intermedios y páginas vencen según `ANALYTICS_AGENT_RESULT_RETENTION_HOURS` (24 horas). Los registros de ejecución, respuestas finales y checkpoints se retienen según `ANALYTICS_AGENT_HISTORY_RETENTION_DAYS` (90 días). La limpieza se ejecuta al procesar otra pregunta. Una ejecución que necesita resultados ya vencidos falla de forma explícita; no vuelve a lanzar la consulta silenciosamente.

SQLite corresponde a una instancia de Workbench con disco persistente. No ofrece recuperación si se pierde esa carpeta ni coordinación entre máquinas con discos distintos. Se procesan todas las páginas; no se ha agregado un límite de 50 filas. Un resultado muy grande sigue consumiendo memoria y almacenamiento del runtime.

La compatibilidad del worker de Teams está conectada al mismo motor e ID estable de solicitud, pero esta entrega no despliega Teams ni modifica permisos, Cloud Run o Pub/Sub.

## Pruebas locales

```bash
python -m unittest discover -s tests -q
python -m pip check
python scripts/benchmark_runtime.py --samples 30
```

La batería incluye regresiones de negocio y conversación ejecutadas con ambos motores, recuperación con objetos nuevos, aislamiento de propietarios, control de versiones, aclaraciones persistidas, páginas confirmadas, envíos inciertos y conservación del último análisis cuando falla una consulta. GitHub Actions ejecuta la batería con Python 3.10 y 3.12.

El benchmark usa un proveedor simulado y prohíbe llamadas a BigQuery; informa mediana y percentil 95 de la orquestación local. No mide la latencia del endpoint real ni certifica un tiempo de respuesta con datos de producción.

## Aceptación con datos reales

La aceptación real sigue pendiente: la instancia `sbs-analytics-python-notebook` estaba `TERMINATED` y el intento de encenderla devolvió 403 por falta de `compute.instances.start`. No se ha certificado la coincidencia de cifras reales ni la latencia del endpoint desde Workbench.

Una vez encendida, desde la terminal del runtime autorizado:

```bash
python scripts/validate_workbench.py --execute --report workbench-validation.json
```

Revisa antes las preguntas y SQL de referencia del script. `--execute` confirma esta batería de consultas de lectura. Usa el `.env` existente, un estado temporal separado y LangGraph; no altera tus sesiones. Incluye primas por sucursal, ramo 201, comparación de quincenas, retirada del filtro de ramo, pagos, reservas y una lectura de `mm_final_netos`. Contrasta dimensiones y cifras con SQL independiente; exige una diferencia máxima de un centavo para importes. El presupuesto acumulado de estimaciones dry run es 10 GiB para la batería, incluyendo referencias, y no se impone a las consultas normales del agente. Los errores o discrepancias hacen que termine con código distinto de cero y escriba el reporte local; no inventa datos para aprobar.

El reporte contiene auditoría SQL y referencias de trabajos; revísalo localmente y no lo publiques sin comprobar su contenido. La rama queda disponible para probar y el PR permanece en borrador hasta completar esta aceptación.

Referencias oficiales: [persistencia](https://docs.langchain.com/oss/python/langgraph/persistence) y [aclaraciones con interrupt](https://docs.langchain.com/oss/python/langgraph/interrupts).
