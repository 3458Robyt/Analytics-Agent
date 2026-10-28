# Analytics Agent

Agente de análisis en Python para consultar BigQuery en lenguaje natural. Incluye una CLI para ejecutar preguntas desde una terminal; no requiere un notebook.

El agente descubre tablas visibles en los proyectos configurados, usa el esquema actual de BigQuery y toma del Excel solo las descripciones que coinciden exactamente con una columna vigente. Genera y ejecuta consultas de solo lectura, procesa todas las páginas y presenta los resultados en tablas con formato numérico español. Las respuestas del proveedor se solicitan con `store=false` de forma predeterminada.

Para reducir latencia, las métricas con una definición confirmada se compilan en Python y no requieren una llamada adicional de revisión del modelo. Para consultas exploratorias se usa el dry run de BigQuery para validar columnas y tipos; cuando falta una decisión de negocio que cambie materialmente la cifra, el agente pregunta dentro de la misma ejecución. Las respuestas y tablas provienen de las filas leídas desde BigQuery; el modelo no puede reemplazarlas por cifras redactadas.

## Requisitos

- Python 3.10 o posterior.
- Una identidad de Google Cloud con acceso a los metadatos y datos de BigQuery necesarios.
- Acceso de red al endpoint del proveedor de IA.
- Acceso de lectura a la URI del diccionario si se quiere usar ese archivo.

La conexión de BigQuery usa Application Default Credentials (ADC) del entorno donde corre el proceso. Para consultas reales, ejecútalo en el runtime autorizado de Workbench: las credenciales locales de otro ordenador no trasladan el acceso de red ni los permisos del runtime de GCP.

## Instalar y configurar

```bash
git clone https://github.com/3458Robyt/Analytics-Agent.git
cd Analytics-Agent
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
cp .env.example .env
```

Edita `.env` y pega una clave vigente en `LLM_API_KEY`. El archivo `.env` está excluido de Git. No subas credenciales al repositorio ni compartas la salida de comandos que impriman variables de entorno.

Las variables del entorno del proceso prevalecen sobre `.env`. Para usar otro archivo:

```bash
analytics-agent --env-file /ruta/a/configuracion.env doctor
```

## Ejecutar desde Vertex AI Workbench

1. Abre la instancia `sbs-analytics-python-notebook` y en JupyterLab selecciona **File > New > Terminal**. Si no aparece Terminal, esa opción debe estar habilitada para la instancia por un administrador.
2. Clona el repositorio privado con una sesión GitHub autenticada. Si la instancia no tiene salida a GitHub o no permite autenticarse, sube `pyproject.toml`, `.env.example` y la carpeta `src/analytics_agent/` a `gs://buckets-analytics-staging/proyectos/librerias/analytics-agent-source/` desde Cloud Storage Console. En la terminal del runtime, copia esos archivos a una carpeta local, por ejemplo `~/Analytics-Agent`; no subas `.env` ni una clave al bucket.
3. Si usaste el bucket para transferir los archivos, cópialos al runtime con:

   ```bash
   mkdir -p ~/Analytics-Agent/src
   gcloud storage cp gs://buckets-analytics-staging/proyectos/librerias/analytics-agent-source/pyproject.toml ~/Analytics-Agent/
   gcloud storage cp gs://buckets-analytics-staging/proyectos/librerias/analytics-agent-source/.env.example ~/Analytics-Agent/
   gcloud storage cp --recursive gs://buckets-analytics-staging/proyectos/librerias/analytics-agent-source/analytics_agent ~/Analytics-Agent/src/
   ```

   Al subir desde Cloud Storage Console, coloca la carpeta `analytics_agent` directamente dentro de `analytics-agent-source/`.
4. En la terminal, instala y configura la CLI:

   ```bash
   cd ~/Analytics-Agent
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -e .
   cp .env.example .env
   nano .env
   analytics-agent doctor
   ```

   Para BigQuery, el proceso usará ADC del runtime; el diccionario se descarga desde GCS si la identidad tiene acceso.
5. Al publicar cambios desde el ordenador de desarrollo, actualiza el clon de Workbench con `git pull` antes de probarlos.

La disponibilidad de Terminal depende de la configuración de Workbench. La [guía de solución de problemas de Google](https://docs.cloud.google.com/vertex-ai/docs/general/troubleshooting-workbench) indica que, si no está disponible, la instancia debe crearse con acceso a terminal.

## Comandos

```bash
# Comprueba configuración y acceso a metadatos; no llama al modelo ni ejecuta SQL
analytics-agent doctor

# Ejecuta una pregunta
analytics-agent ask "¿Cuál fue la prima emitida por ramo durante el primer semestre de 2026?"

# Inicia una conversación; /new abre otra sesión y /exit termina
analytics-agent chat

# Lista las sesiones guardadas en la cuenta local
analytics-agent sessions list

# Inspecciona pregunta, respuesta, SQL y referencias de una sesión
analytics-agent sessions show ID_SESION

# Lista preferencias, procedimientos y propuestas de aprendizaje
analytics-agent learn list

# Revisa un aprendizaje o filtra las definiciones pendientes
analytics-agent learn show ID_APRENDIZAJE
analytics-agent learn list --status proposed

# Desactiva una enseñanza incorrecta
analytics-agent learn disable ID_APRENDIZAJE

# Procesa la cola durable de aprendizaje si la ejecución en segundo plano no pudo iniciarla
analytics-agent learn process

# Reactiva una preferencia o procedimiento desactivado
analytics-agent learn enable ID_APRENDIZAJE

# Compara un prompt candidato con el actual sin ejecutar consultas reales
analytics-agent evaluate --candidate-prompt /ruta/al/prompt-candidato.txt

# Reanuda una conversación en la CLI
analytics-agent chat --resume ID_SESION
```

Dentro del chat puedes usar `/feedback comentario`, `/new`, `/sessions`, `/resume ID` y `/history ID`. El feedback y la navegación se procesan en Python, sin consumir tokens ni consultar BigQuery. El operador puede revisar comentarios con `analytics-agent feedback list`, y tiempos, errores y patrones recurrentes con `analytics-agent usage report`. La guía de estas funciones está en [docs/FEEDBACK_MEMORY.md](docs/FEEDBACK_MEMORY.md).

Si no indicas fechas y no hay un periodo reciente en la conversación, el agente estima la consulta mediante un dry run. Si supera **1 GiB** o no se puede estimar su volumen, pide un periodo antes de ejecutarla. Puedes responder con fechas o con «todo el histórico». Las consultas pequeñas continúan sin esta pregunta; las reglas de pagos y reservas siguen aplicándose.

`doctor` valida ADC mediante el inventario de metadatos de BigQuery. La ejecución de `ask` y `chat` puede generar varias consultas y costos según los datos consultados. El agente conserva la restricción de solo lectura: no genera DML, DDL ni scripts mutantes. El diccionario no necesita contener todas las columnas; BigQuery aporta el esquema disponible.

En Workbench usa `analytics-agent ask "pregunta"` para una consulta puntual o `analytics-agent chat` para mantener el contexto entre preguntas. Teams está aplazado: no necesitas configurar Pub/Sub, Cloud Run, Firestore ni variables `TEAMS_*` para ejecutar esta POC.

## Reglas de pagos y reservas

Las consultas generales de siniestros usan `centralizacion-datos.analytics_curated.mm_final` y suman **`incurrido_total`**, conservando signos y todos los registros del periodo:

| Métrica (`metric_key`) | Valor de `tipo` | Fecha |
| --- | --- | --- |
| `pagos_siniestros` | Pagos | `fecha_accidente` por defecto; `fecha_pago` si se solicita |
| `reservas_avisadas` | Avisado | `mes_mmalla` |
| `avisos_recobro` | aviso de recobro | `mes_mmalla` |
| `avisados_salvamentos` | avisado de salvamentos | `mes_mmalla` |

Las reservas de varios meses suman todo el rango solicitado. Recobros y salvamentos se muestran por separado y no se restan automáticamente de pagos ni de reservas. Esta definición interna de reservas avisadas no certifica un saldo actuarial al último corte. Las consultas explícitamente netas requieren verificar su propio mapeo; estas reglas no se trasladan automáticamente a `mm_final_netos`.

`honorarios` e `indemnizaciones` aplican además un filtro sobre **valores reales de `clase_pago`**. El agente consulta las etiquetas antes de filtrarlas y las guarda en una caché de una hora durante el proceso. Si su significado es ambiguo, pide las etiquetas exactas. La clase de indemnización identifica el concepto y no demuestra por sí sola quién recibió el dinero.

Ejemplos desde la terminal de Workbench:

```bash
analytics-agent ask "Total de pagos de septiembre de 2026 por ramo"
analytics-agent ask "Reservas avisadas del primer semestre de 2026"
analytics-agent ask "Honorarios pagados en septiembre de 2026"
analytics-agent ask "Indemnizaciones pagadas en septiembre de 2026"
analytics-agent ask "Avisos de recobro y avisados de salvamentos de septiembre de 2026, separados"
```

El glosario vigente se impone sobre procedimientos antiguos de la memoria. Los nombres de campos escritos en la pregunta se comprueban con el esquema de BigQuery o con el dry run: una errata inequívoca se corrige y se comunica; varias equivalencias requieren aclaración. Una descripción faltante no bloquea una columna existente. Para fechas de pago TIMESTAMP se usa `ANALYTICS_AGENT_TIMEZONE` (por defecto `America/Bogota`); `mes_mmalla` conserva el mes de cierre almacenado sin desplazarlo de mes por la zona horaria.

## Análisis combinados y clasificación organizacional

`run_analysis` calcula varias métricas en una consulta y las agrega de forma independiente para evitar duplicar importes. La prima devengada usa `analytics-487218.functions.prima_devengada`, con firma comprobada y vigencias que se solapen con cada mes. La siniestralidad interna predeterminada es **pagos / prima devengada × 100**; no representa automáticamente un indicador sectorial de siniestralidad incurrida.

El catálogo organizacional incluido tiene **82 códigos locales únicos** y 98 relaciones de subprofit. «Por área» significa línea mayor. Copropiedades se identifica con código local **34**, separado del ramo comercial. Los reintentos y cambios de fecha conservan el alcance del análisis activo; `/new` inicia otro. «Últimos seis meses» incluye el mes actual y señala que es parcial.

Los detalles, límites y ejemplos para contrastar cifras en Workbench están en [docs/BUSINESS_KNOWLEDGE.md](docs/BUSINESS_KNOWLEDGE.md). No se necesitan servicios adicionales para usar estas funciones desde la terminal de Workbench.

## Memoria y glosario

La CLI guarda automáticamente sesiones, preguntas, respuestas, SQL, resultados agregados y referencias a trabajos de BigQuery en SQLite durante **90 días**. Los resultados completos se guardan localmente comprimidos durante **24 horas**, para analizarlos o exportarlos después dentro de la misma sesión. Ambos plazos son configurables. Por defecto el archivo está en `~/.local/share/analytics-agent/state.sqlite3`; los permisos locales se restringen a la cuenta del runtime. `ANALYTICS_AGENT_STATE_DIR` permite cambiar la carpeta. `/clear` inicia otra sesión y conserva las anteriores; usa `/new` para lo mismo.

La memoria pertenece a la cuenta de sistema que ejecuta la CLI. En la POC, ejecuta siempre desde la misma cuenta de Workbench para conservar el historial. Esta separación local no autentica usuarios que compartan una cuenta de sistema.

El prompt principal versionado se encuentra en `src/analytics_agent/prompts/agent_system_v2.txt`. `analytics-agent evaluate` ejecuta la batería local con ese prompt y con el archivo candidato, usando BigQuery simulado; el comando no cambia el prompt vigente. La evaluación sí llama al proveedor de IA para medir el comportamiento real del modelo, por lo que consume tokens.

El glosario compartido, versionado junto al código, está en `src/analytics_agent/data/business_glossary.json`. Las entradas validadas de `terms` pueden compilar SQL; `concepts` aporta contexto de seguros/finanzas o hipótesis candidatas, pero no se ejecuta como fórmula validada. El agente no modifica el glosario. Las definiciones nuevas quedan como propuestas locales hasta que se revisen y agreguen mediante cambios en Git.

Un grafo local derivado de ese glosario relaciona métricas con tablas, importes, fechas y filtros obligatorios, conservando la procedencia y versión. El agente puede consultarlo con `search_knowledge`; para inspeccionarlo desde Workbench usa `analytics-agent knowledge search "pagos y reservas"`. No requiere un servicio externo ni transforma comentarios de usuarios en fórmulas compartidas.

Las aclaraciones ocurren en la misma pregunta de `ask` o `chat`. Si el usuario confirma una regla de negocio, queda guardada como definición personal en la SQLite local; no se incorpora al glosario compartido. `analytics-agent definitions list`, `disable` y `enable` permiten inspeccionarla y administrarla.

Después de responder, el agente pone en cola la extracción de preferencias y procedimientos para procesarla en segundo plano; eso evita sumar la llamada del curador al tiempo que se espera por la respuesta. Las preferencias requieren una declaración explícita; los procedimientos requieren dos usos distintos verificados. El curador no recibe filas detalladas. El aprendizaje no modifica el prompt o el código por sí mismo.

Durante la POC, la presentación predeterminada `audit` incluye SQL, referencias de BigQuery, bytes y tiempos para que se puedan revisar los cálculos. Usa `analytics-agent ask --presentation business "pregunta"`, `analytics-agent chat --presentation business` o configura `ANALYTICS_AGENT_PRESENTATION=business` cuando quieras mostrar únicamente el resultado para negocio. El modo `business` oculta esos detalles al imprimir; la sesión sigue conservando el SQL para inspección local.

El agente envía al modelo todas las filas cuando el resultado serializado cabe en `ANALYTICS_AGENT_EVIDENCE_MAX_CHARS` (por defecto 40.000 caracteres). Para resultados mayores envía un perfil calculado sobre todas las filas y muestras; puede inspeccionar después el resultado completo desde la memoria local. El perfil incluye valores nulos, cardinalidad y estadísticas numéricas cuando aplican.

Pide en `chat` algo como «Exporta el último resultado a Excel». El archivo `.xlsx` se guarda en `ANALYTICS_AGENT_EXPORT_DIR` (por defecto `./exports`, relativa a la carpeta desde la que ejecutaste el comando). En Workbench abre esa carpeta en el navegador de archivos de JupyterLab y descarga el Excel. Incluye hojas con datos, resumen de consulta y parte en hojas adicionales si supera el máximo de filas de Excel. Para continuar la misma conversación o exportar un resultado anterior, usa `analytics-agent chat --resume ID_SESION`; cada `ask` puntual inicia una sesión nueva.

## Caracteres de la terminal

El agente repara caracteres dañados recibidos de la terminal antes de guardar preguntas, aclaraciones o feedback. `?`, `¿`, tildes, ñ y emojis válidos se conservan. Si aparece el aviso **«Se repararon caracteres dañados»**, los caracteres irrecuperables se muestran como `�`; revisa el texto y vuelve a escribir la parte afectada si cambia su sentido. La sesión continúa abierta y no requiere cambiar permisos ni borrar el historial.

## Pruebas locales

Las pruebas usan clientes simulados, no llaman a BigQuery ni al proveedor de IA:

```bash
python -m unittest discover -s tests -v
```

## Datos enviados al proveedor

El agente envía al endpoint configurado las preguntas, el SQL generado, el esquema, las descripciones relevantes y la evidencia de resultados descrita arriba. El Excel se crea localmente cuando el usuario lo solicita y complementa el análisis conversacional. `LLM_STORE_RESPONSES=false` solicita que el proveedor no almacene las respuestas; confirma que el endpoint cumpla las condiciones aprobadas para la POC.

## Integración futura con Microsoft Teams

La integración y los requisitos de despliegue están en [docs/TEAMS_PILOT.md](docs/TEAMS_PILOT.md). Teams se conecta a un receptor en Cloud Run; el proceso que ejecuta BigQuery sigue en Workbench y se comunica por Pub/Sub. El gateway no recibe ni necesita `LLM_API_KEY`. Para el receptor se requiere Python 3.12; el worker de Workbench se instala por separado con `pip install -e ".[teams-worker]"`.
