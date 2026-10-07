# Piloto privado de Analytics Agent en Teams

## Cómo se conectan las piezas

- Un gateway público HTTPS en Cloud Run recibe actividades verificadas por el SDK de Teams y aplica una lista de `aadObjectId` autorizados.
- El gateway escribe una solicitud en Pub/Sub. El worker en Workbench la procesa con la misma librería, configuración de BigQuery y diccionario que la CLI.
- El worker publica la respuesta. El gateway la entrega en el chat privado que originó la solicitud.
- Workbench conserva la memoria SQLite por tenant y usuario. Una base SQLite aparte conserva las filas de resultados durante 24 horas para paginarlas en Teams. Firestore conserva las solicitudes y entregas técnicas durante siete días mediante TTL; no guarda filas de BigQuery ni respuestas analíticas.
- Las correcciones que escriben los usuarios quedan en SQLite para revisión humana y se consultan con `analytics-agent --env-file .env teams feedback list`.

El SDK de Teams para Python requiere Python 3.12. Por eso el gateway se construye con [Dockerfile.teams](../Dockerfile.teams); el worker utiliza el Python instalado en Workbench. El bot y el worker nunca escriben en BigQuery: las consultas siguen pasando por las reglas de solo lectura del agente.

## 1. Preparar el tenant y las identidades

Solicita a TI:

1. El `tenantId` del tenant corporativo y los `aadObjectId` de las personas del piloto.
2. La aprobación para registrar una aplicación propia de Teams y habilitar su instalación para esos usuarios.
3. Una identidad de aplicación para Teams (`CLIENT_ID`) y su secreto (`CLIENT_SECRET`). No guardes el secreto en Git; almacénalo en Secret Manager para Cloud Run y en el `.env` local de Workbench con los permisos de archivo restringidos.
4. El nombre de la cuenta de servicio del gateway, la cuenta de servicio de autenticación OIDC para el push de Pub/Sub y los permisos descritos abajo.

El registro de la aplicación puede hacerse con Teams Developer CLI desde un entorno autorizado. La guía actual ofrece `teams app create` y la opción predeterminada de bot administrado por Teams; la instalación por usuarios depende de la política del tenant. [Guía para registrar y ejecutar en Teams](https://learn.microsoft.com/en-us/microsoftteams/platform/teams-sdk/getting-started/running-in-teams/overview).

## 2. Crear recursos de GCP

Ejecuta estos comandos en el proyecto aprobado. En esta configuración se usa `sbscol-dbreplication-prd` y la región `us-east1`; cambia el proyecto únicamente si TI asigna otro para el gateway.

```bash
export PROJECT_ID=sbscol-dbreplication-prd
export REGION=us-east1

gcloud services enable run.googleapis.com pubsub.googleapis.com firestore.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com \
  --project "$PROJECT_ID"

gcloud firestore databases create --database='(default)' --location="$REGION" \
  --type=firestore-native --project="$PROJECT_ID"

gcloud pubsub topics create analytics-agent-teams-requests --project="$PROJECT_ID"
gcloud pubsub topics create analytics-agent-teams-responses --project="$PROJECT_ID"
gcloud pubsub subscriptions create analytics-agent-teams-requests-workbench \
  --topic=analytics-agent-teams-requests --enable-message-ordering --ack-deadline=600 \
  --project="$PROJECT_ID"

gcloud firestore fields ttls update expires_at --collection-group=items \
  --enable-ttl --database='(default)' --project="$PROJECT_ID"
```

Si Firestore ya tiene una base `(default)` en otra región, úsala y verifica con TI su ubicación; no intentes crearla otra vez. Configura la caducidad TTL de Firestore para el campo `expires_at` en el grupo de colecciones `items`.

La cuenta de servicio que ejecutará Workbench necesita `roles/pubsub.subscriber` en la suscripción de solicitudes y `roles/pubsub.publisher` en el topic de respuestas. Conserva los permisos BigQuery que ya funcionan en Workbench. La cuenta de Cloud Run necesita `roles/pubsub.publisher` en el topic de solicitudes, `roles/datastore.user` para el estado de entrega y acceso únicamente al secreto del bot. Configura la autenticación OIDC de la suscripción push según [la guía de Pub/Sub](https://docs.cloud.google.com/pubsub/docs/authenticate-push-subscriptions).

La cuenta de servicio OIDC de la suscripción push debe pertenecer al mismo proyecto que Pub/Sub. Sustituye `SUBSCRIPTION_CREATOR` por un principal con el prefijo `user:` o `serviceAccount:`. Define los valores con TI y concede los permisos antes de crear la suscripción:

```bash
export PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
export OIDC_SERVICE_ACCOUNT="<cuenta-oidc>@$PROJECT_ID.iam.gserviceaccount.com"
export SUBSCRIPTION_CREATOR="<usuario-o-cuenta-que-creará-la-suscripción>"
export PUBSUB_SERVICE_AGENT="service-$PROJECT_NUMBER@gcp-sa-pubsub.iam.gserviceaccount.com"

gcloud iam service-accounts add-iam-policy-binding "$OIDC_SERVICE_ACCOUNT" \
  --project="$PROJECT_ID" --member="serviceAccount:$PUBSUB_SERVICE_AGENT" \
  --role=roles/iam.serviceAccountTokenCreator

gcloud iam service-accounts add-iam-policy-binding "$OIDC_SERVICE_ACCOUNT" \
  --project="$PROJECT_ID" --member="$SUBSCRIPTION_CREATOR" \
  --role=roles/iam.serviceAccountUser
```

El primer permiso permite que el agente de servicio firme el token OIDC; el segundo permite que quien crea la suscripción adjunte esa cuenta. El endpoint valida firma, audiencia y correo de la cuenta OIDC.

## 3. Desplegar el gateway

En el clon del repositorio que tiene acceso a Cloud Build:

```bash
gcloud artifacts repositories create analytics-agent \
  --repository-format=docker --location="$REGION" --project="$PROJECT_ID"

gcloud builds submit --project="$PROJECT_ID" --region="$REGION" \
  --config=cloudbuild-teams.yaml --substitutions="_REGION=$REGION,_TAG=pilot" .
```

Crea primero el servicio para obtener su URL HTTPS. En esta primera versión, deja las credenciales de Teams sin configurar: `/healthz` responderá, mientras que `/api/messages` devolverá `503` hasta que el registro del bot esté configurado.

- `TEAMS_TENANT_ID`: tenant aprobado.
- `TEAMS_ALLOWED_USER_IDS`: lista separada por comas de `aadObjectId` del piloto.
- `TEAMS_GCP_PROJECT`: proyecto donde están Firestore y Pub/Sub.
- `TEAMS_REQUEST_TOPIC=analytics-agent-teams-requests`.
- `TENANT_ID`: se deriva de `TEAMS_TENANT_ID` durante el arranque.
- `TEAMS_PUBSUB_PUSH_AUDIENCE=analytics-agent-teams-pubsub` (audience estable del token OIDC).
- `TEAMS_PUBSUB_PUSH_SERVICE_ACCOUNT`: identidad OIDC configurada en la suscripción push.

```bash
gcloud run deploy analytics-agent-teams-gateway \
  --project="$PROJECT_ID" --region="$REGION" \
  --image="$REGION-docker.pkg.dev/$PROJECT_ID/analytics-agent/teams-gateway:pilot" \
  --service-account="<cuenta-servicio-gateway>" --allow-unauthenticated --max=1 \
  --set-env-vars="^|^TEAMS_TENANT_ID=<tenant>|TEAMS_ALLOWED_USER_IDS=<aad-id-1>,<aad-id-2>|TEAMS_GCP_PROJECT=$PROJECT_ID|TEAMS_REQUEST_TOPIC=analytics-agent-teams-requests|TEAMS_PUBSUB_PUSH_AUDIENCE=analytics-agent-teams-pubsub|TEAMS_PUBSUB_PUSH_SERVICE_ACCOUNT=<cuenta-oidc>"
```

Obtén la URL y registra la aplicación con Teams Developer CLI. TI debe autorizar el registro y la instalación en el tenant:

```bash
npm install -g @microsoft/teams.cli
teams login
teams status

export SERVICE_URL="$(gcloud run services describe analytics-agent-teams-gateway \
  --project="$PROJECT_ID" --region="$REGION" --format='value(status.url)')"
teams app create --name "Analytics Agent" \
  --endpoint "$SERVICE_URL/api/messages" \
  --env .env.teams-registration
```

El archivo `.env.teams-registration` contiene los valores generados por Microsoft. Crea el secreto del bot en Secret Manager y actualiza el servicio con el `CLIENT_ID` aprobado y la referencia secreta `CLIENT_SECRET`:

```bash
gcloud run deploy analytics-agent-teams-gateway \
  --project="$PROJECT_ID" --region="$REGION" \
  --image="$REGION-docker.pkg.dev/$PROJECT_ID/analytics-agent/teams-gateway:pilot" \
  --service-account="<cuenta-servicio-gateway>" --allow-unauthenticated --max=1 \
  --set-env-vars="^|^TEAMS_TENANT_ID=<tenant>|TENANT_ID=<tenant>|TEAMS_ALLOWED_USER_IDS=<aad-id-1>,<aad-id-2>|TEAMS_GCP_PROJECT=$PROJECT_ID|TEAMS_REQUEST_TOPIC=analytics-agent-teams-requests|TEAMS_PUBSUB_PUSH_AUDIENCE=analytics-agent-teams-pubsub|TEAMS_PUBSUB_PUSH_SERVICE_ACCOUNT=<cuenta-oidc>|CLIENT_ID=<app-id>" \
  --set-secrets="CLIENT_SECRET=<secreto-teams>:latest"
```

Cloud Run permite llegar al endpoint de Teams sin autenticación IAM porque Microsoft debe invocarlo; con credenciales configuradas, el SDK valida cada actividad entrante. La ruta interna de Pub/Sub valida además su token OIDC y la identidad de la cuenta configurada.

Por último, crea la suscripción push de respuestas:

```bash
gcloud pubsub subscriptions create analytics-agent-teams-responses-cloudrun \
  --project="$PROJECT_ID" --topic=analytics-agent-teams-responses \
  --enable-message-ordering \
  --push-endpoint="$SERVICE_URL/internal/pubsub/results" \
  --push-auth-service-account="<cuenta-oidc>" \
  --push-auth-token-audience="analytics-agent-teams-pubsub" \
  --ack-deadline=60
```

El gateway no recibe `LLM_API_KEY` ni permisos de BigQuery. Revisa `/healthz` y `gcloud run services logs read analytics-agent-teams-gateway --region="$REGION"` para confirmar que inició.

## 4. Instalar y arrancar el worker en Workbench

En el repositorio de Workbench, con su entorno virtual activado:

```bash
python -m pip install -e ".[teams-worker]"
```

Agrega al `.env` del runtime:

```dotenv
TEAMS_TENANT_ID=<tenant-aprobado>
TEAMS_ALLOWED_USER_IDS=<aad-object-id-1>,<aad-object-id-2>
TEAMS_GCP_PROJECT=sbscol-dbreplication-prd
TEAMS_REQUEST_SUBSCRIPTION=analytics-agent-teams-requests-workbench
TEAMS_RESPONSE_TOPIC=analytics-agent-teams-responses
```

Verifica que ADC de Workbench corresponda a la cuenta de servicio con los permisos de Pub/Sub indicados. En una terminal persistente inicia:

```bash
analytics-agent --env-file .env teams-worker
```

El gateway considera que el worker está activo mientras reciba su heartbeat cada 30 segundos. Si Workbench se apaga, inicia de nuevo el worker; las solicitudes caducan tras 15 minutos.

## 5. Registrar e instalar la aplicación

Con TI o desde el entorno autorizado, sigue la guía oficial de Teams Developer CLI para registrar el endpoint `https://<servicio-cloud-run>/api/messages`, crear el manifiesto y obtener el enlace de instalación. Instala la aplicación solamente para los usuarios aprobados y prueba el chat personal. No se habilitan canales compartidos en esta fase.

## 6. Comprobar el piloto

1. Con el worker apagado, el bot informa que Workbench no está disponible; al encenderlo, el heartbeat habilita las consultas.
2. Un usuario autorizado consulta una métrica conocida y recibe el resumen y la primera página de la tabla.
3. Las acciones de página y de columnas recorren las filas guardadas sin enviar otro trabajo a BigQuery. La tabla de Teams presenta diez filas y cinco columnas por página; los demás campos también se pueden recorrer.
4. La acción de trazabilidad muestra SQL y referencias disponibles. La acción de corrección guarda la observación bajo la identidad de ese usuario.
5. Una pregunta que solicita aclaración se retoma cuando el mismo usuario responde en ese chat. La acción «Nueva consulta» abre otra sesión.
6. Un usuario fuera de la lista recibe el mensaje de acceso restringido; sus conversaciones y resultados no se mezclan con las de otro usuario.
7. Reentregar el mismo mensaje de Teams devuelve la respuesta registrada y reutiliza el identificador determinista del job de BigQuery.
8. Una consulta con más de diez filas y otra con más de cinco columnas confirman que la paginación no recorta los datos ni vuelve a ejecutar SQL.

## 7. Siguientes etapas de desarrollo

1. Cerrar definiciones de negocio con el área responsable: incurrido neto, reservas, prima emitida, fechas, moneda y tratamiento de anulaciones/endosos.
2. Convertir cada definición aprobada en casos de evaluación con pregunta, SQL esperado y cifra de referencia. Ejecutar esos casos antes de cambiar prompts, reglas o modelos.
3. Medir latencia por etapa (modelo, BigQuery y renderizado), reducir llamadas redundantes y mostrar estados de avance sin exponer SQL al usuario normal.
4. Después del piloto privado, revisar logs, permisos mínimos, retención, costo máximo, disponibilidad del worker y política de instalación; luego ampliar usuarios gradualmente.

La guía oficial del SDK requiere Python 3.12 o superior; el receptor está fijado a Python 3.12 en [Dockerfile.teams](../Dockerfile.teams). El despliegue real depende de la aprobación del tenant y de los permisos de GCP descritos arriba.
