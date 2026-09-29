# Analytics Agent

Agente de análisis en Python para consultar BigQuery en lenguaje natural. Incluye una CLI para ejecutar preguntas desde una terminal; no requiere un notebook.

El agente descubre tablas visibles en los proyectos configurados, lee los esquemas reales de BigQuery y utiliza el Excel como fuente opcional de descripciones. Genera y ejecuta consultas de solo lectura, procesa resultados paginados sin un límite total de filas y devuelve una explicación con una tabla de resumen cuando aplica. Las respuestas del proveedor se solicitan con `store=false` de forma predeterminada.

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

# Inicia una conversación interactiva; /clear limpia el contexto y /exit termina
analytics-agent chat
```

`doctor` valida ADC mediante el inventario de metadatos de BigQuery. La ejecución de `ask` y `chat` puede generar varias consultas y costos según los datos consultados. El agente conserva la restricción de solo lectura: no genera DML, DDL ni scripts mutantes. El diccionario no necesita contener todas las columnas; BigQuery aporta el esquema disponible.

## Pruebas locales

Las pruebas usan clientes simulados, no llaman a BigQuery ni al proveedor de IA:

```bash
python -m unittest discover -s tests -v
```

## Datos enviados al proveedor

El agente envía al endpoint configurado las preguntas, el SQL generado y los metadatos y filas que necesita para redactar la respuesta. El valor predeterminado `LLM_STORE_RESPONSES=false` solicita que el proveedor no almacene las respuestas. Configura y utiliza el endpoint de acuerdo con las condiciones aprobadas para la POC.
