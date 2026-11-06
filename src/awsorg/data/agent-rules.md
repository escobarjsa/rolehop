# AWS con awsorg

El usuario trabaja con varias organizaciones de AWS IAM Identity Center gestionadas con **awsorg**. Sigue estas reglas en todos los proyectos.

## Credenciales y perfiles

- Usa **siempre perfiles** con `AWS_PROFILE=<perfil>` delante de cada comando (`aws`, `terraform`, `tofu`, `terragrunt`, scripts con boto3). Si un proyecto usa otra variable para el perfil del estado remoto (por ejemplo `TF_STATE_PROFILE`), defínela igual.
- **Catálogo:** `awsorg list` (o `awsorg list --json`) muestra todos los perfiles: `<org>-<cuenta>-<rol>`, por ejemplo `empresa-devsecops-admin`. Consúltalo para elegir.
- **Elige libremente** el perfil y el rol que la tarea necesite, Admin incluido, sin pedir permiso. Prefiere uno de solo lectura para consultar si existe, y di en una línea qué perfil usaste.
- Al cambiar de cuenta o de rol, confirma con `AWS_PROFILE=<perfil> aws sts get-caller-identity`.
- Al empezar un trabajo con AWS: `awsorg status` para ver qué organizaciones tienen sesión.
- Si el repo tiene `.envrc`, su `AWS_PROFILE` es solo el valor por defecto; puedes usar otro en cada comando.
- Si el usuario pide agregar una cuenta, un rol o una organización, indícale `awsorg init` (asistente) o edita `~/.config/awsorg/orgs.toml` y ejecuta `awsorg sync --quiet`.

## Prohibido

- Leer `~/.aws`, pegar o exportar claves (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`), o pedir al usuario las «claves de acceso» del portal.
- Usar `aws configure export-credentials`, `aws configure get`, `aws sso get-role-credentials`, `assume` o `granted`.
- Ejecutar `direnv allow` (los `.envrc` los aprueba el usuario).
- Ejecutar `awslogin`, `awsorg login`, `aws sso login` o `awsorg run --login` tú mismo: requieren al usuario en el navegador.

## Cuando la sesión expira

Si un comando devuelve `ExpiredToken`, `Token has expired`, `SSO session … expired`, `InvalidGrant`, `cannot be refreshed` o similar:
1. **Detente** con AWS. No reintentes y no cambies a otro perfil u organización para esquivarlo.
2. Pide al usuario que ejecute en su terminal: `awslogin <org>` (la org es el prefijo del perfil).
3. Cuando lo confirme, continúa desde donde quedaste. Mientras tanto puedes avanzar en lo que no use AWS.

Las credenciales de cada rol se renuevan solas durante la sesión del portal. Solo hace falta `awslogin` cuando termina esa sesión.

## Cuando el usuario se va y te deja trabajando

Si dice algo como «me voy», «te dejo trabajando» o «sigue tú» y la tarea usa AWS:
1. Antes de que se vaya, pídele que ejecute: `awsorg run --login --max <duración estimada>` (renueva la sesión y comprueba que alcanza). Propón la duración según el trabajo pendiente, por ejemplo `--max 6h`.
2. Cuando confirme, ejecuta `awsorg status`. Si no alcanza, dile el máximo posible y cómo dividir el trabajo.
3. Reordena el plan: **primero todo lo que necesita AWS** y después lo demás.
4. Si la sesión expira sin el usuario, detén lo de AWS, sigue con el resto y deja al final un resumen con lo que falta y el comando `awslogin <org>`. Recuérdale que el código de dispositivo se puede aprobar desde el celular.
5. Los cambios de infraestructura que deban aplicarse sin el usuario van por PR y pipeline, nunca con sus credenciales.
