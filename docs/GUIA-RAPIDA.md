# Guía rápida de awsorg (para dummies)

> **En una frase:** cada mañana escribes `awslogin`, apruebas en el navegador, y listo. Tú y tu agente de IA podéis usar todas tus cuentas y roles de AWS durante toda la jornada, sin copiar claves.

## 1. Instalar (una vez por equipo)

```bash
uv tool install awsorg
awsorg setup
```

`awsorg setup` te pregunta tus organizaciones de AWS: nombre corto, URL del portal y región. Después inicias sesión y **descubre tus cuentas y roles solo**. También deja `~/.local/bin` en tu PATH y configura tus agentes de IA.

Si te dice «abre una terminal nueva», hazlo, o ejecuta `exec $SHELL`.

## 2. Cada día

```bash
awslogin
```

1. Aparece un **código** y un enlace. Ábrelo en el navegador, en el PC o en el celular.
2. Comprueba que el código coincide y pulsa **Confirmar** y **Permitir**.
3. Listo: `awsorg status` muestra ✓.

> **No copies las «claves de acceso» del portal.** Duran poco y se rompen. `awslogin` las sustituye.

## 3. Con tu agente de IA

Dile: *«usa los perfiles y roles de AWS que necesites»*, o nombra la cuenta: *«lista los buckets de prod»*. El agente elige el perfil, por ejemplo `empresa-prod-admin`, y te dice cuál usó.

**Si te dice «la sesión SSO terminó»:** ejecuta `awslogin <org>` y dile *«sigue»*.

**Si te vas y lo dejas trabajando:** dile *«me voy»*. Te pedirá que ejecutes:

```bash
awsorg run --login --max 6h
```

Así renuevas la sesión desde ese momento y compruebas que alcanza. Si aun así expira, puedes aprobar `awslogin` desde el celular.

## 4. Tus perfiles

Se llaman `organización-cuenta-rol`. Para verlos:

```bash
awsorg list
```

Para usar uno tú mismo:

```bash
AWS_PROFILE=empresa-prod-admin aws s3 ls
```

## 5. Agregar o cambiar cosas

| Quiero… | Hago… |
|---|---|
| Agregar otra organización | `awsorg init` → opción **1** → `awsorg sync` → `awslogin <org>` |
| Agregar una cuenta o un rol que me acaban de dar | `awsorg init` → opción **2** → elijo la org → `awsorg sync` |
| Editar a mano | `nano ~/.config/awsorg/orgs.toml` → `awsorg sync --dry-run` → `awsorg sync` |
| Quitar una organización | La borro de `orgs.toml` → `awsorg sync --prune` |
| Ver cuentas y roles reales | `awsorg discover <org>` (con sesión iniciada) |
| Configurar otro agente de IA | `awsorg agent install codex` (o `kiro`, `copilot`…) |
| Perfil por defecto en un repo | `awsorg envrc <perfil> > .envrc && direnv allow` |

### Agregar una cuenta a mano

```toml
[[org.accounts]]
name = "billing"            # nombre corto, minúsculas, sin espacios
id = "123456789012"         # ID de la cuenta (12 dígitos, entre comillas)
roles = ["admin"]           # alias de [org.roles] o el nombre exacto del rol en el portal
```

Va **debajo** de su `[[org]]`, antes del siguiente `[[org]]`.

## 6. Problemas comunes

| Veo esto | Qué hago |
|---|---|
| `✗ … sin sesión · ejecuta: awslogin …` | `awslogin <org>` |
| `command not found: awsorg` | Abre una terminal nueva o ejecuta `awsorg setup` otra vez |
| `The config profile (x) could not be found` | Revisa el nombre con `awsorg list` |
| `ForbiddenException` en un perfil | No tienes ese rol en esa cuenta: corrige `roles` en `orgs.toml` |
| `sync` dice que hay secciones que «chocan» | Tienes un perfil antiguo con el mismo nombre en `~/.aws/config`: renómbralo o bórralo |
| `no es TOML válido … line N` | Error de escritura en `orgs.toml` en esa línea |
| `awsorg run` dice «No arranco» | La sesión no alcanza: usa `--login` o un `--max` menor |
| Algo raro en `~/.aws/config` | Restaura la última copia `~/.aws/config.bak-FECHA` |

## 7. Glosario

- **Organización:** un portal de Identity Center. Un `awslogin` por organización.
- **Cuenta:** cada fila del portal.
- **Rol / permission set:** lo que aparece debajo de cada cuenta (Administrator, ReadOnly…).
- **Perfil:** organización + cuenta + rol (`empresa-prod-admin`).
- **Sesión del portal:** cuánto dura tu `awslogin`. Mientras dura, todo se renueva solo.
