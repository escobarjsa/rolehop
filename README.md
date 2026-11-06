# awsorg

**Perfiles AWS IAM Identity Center para varias organizaciones que se renuevan solos, listos para humanos y agentes de IA.**

> *English summary:* `awsorg` turns a small TOML file describing your AWS IAM Identity Center organizations, accounts and roles into native `sso-session` profiles in `~/.aws/config` (auto-refreshing, no static keys), gives you a one-command login (`awslogin`), and teaches AI coding agents (Claude Code, Codex, Kiro, Copilot CLI, Gemini, Qwen, OpenCode, Amp, Pi…) how to pick profiles, and what to do when the SSO session expires. Pure Python standard library, no dependencies. CLI messages are in Spanish.

---

## ¿Qué problema resuelve?

Copiar las «claves de acceso» del portal de Identity Center te da credenciales **fijas** que mueren con la duración del rol (a menudo 1 hora). Tú y tu agente de IA os quedáis sin acceso a mitad del trabajo.

Con `awsorg`:

- **Un archivo** (`~/.config/awsorg/orgs.toml`) describe tus organizaciones, cuentas y roles. Hay un asistente que lo crea por ti y descubre las cuentas solo.
- **Perfiles nativos `sso-session`**: el AWS CLI y los SDK (boto3, Terraform/OpenTofu, Terragrunt…) **renuevan solos** las credenciales de cada rol durante toda la sesión del portal.
- **Un comando** por jornada: `awslogin`. Funciona con código de dispositivo (ideal en WSL o SSH).
- **Agentes de IA**: instala instrucciones (y hooks donde se puede) para que el agente elija el perfil adecuado, nunca lea credenciales y se detenga a pedirte `awslogin` cuando la sesión termina.

| Reloj | Duración típica | ¿Quién lo renueva? |
|---|---|---|
| Credenciales del rol (permission set) | 1 h (Admin), 4 h, 8 h… | El AWS CLI y los SDK, solos |
| Sesión del portal de Identity Center | La configurada (8 h por defecto) | Tú: `awslogin` (una aprobación en el navegador) |

## Instalación

Requisitos: Python ≥ 3.11 y AWS CLI v2 ≥ 2.22. Linux, WSL o macOS.

```bash
uv tool install awsorg
awsorg setup
```

También con pipx (`pipx install awsorg`). Para la versión de GitHub antes de PyPI:

```bash
uv tool install git+https://github.com/escobarjsa/awsorg
```

`awsorg setup` hace todo en uno:
1. **PATH:** añade `~/.local/bin` a tu shell (zsh, bash o fish) y el hook de direnv si lo tienes.
2. **Organizaciones:** si no existe `orgs.toml`, lanza el asistente. Te pide nombre, URL del portal y región; inicias sesión y descubre tus cuentas y roles.
3. **Perfiles:** escribe los perfiles en `~/.aws/config`, solo dentro de bloques marcados y con copia de seguridad.
4. **Agentes de IA:** instala instrucciones y hooks en los agentes que detecta.

Después:

```bash
awslogin
awsorg status
```

## Uso diario

| Situación | Comando |
|---|---|
| Empezar el día | `awslogin` |
| Ver perfiles | `awsorg list` |
| Ver sesiones | `awsorg status` |
| Usar un perfil | `AWS_PROFILE=empresa-prod-admin aws s3 ls` |
| Agregar una organización, o actualizar cuentas y roles | `awsorg init` y luego `awsorg sync` |
| Perfil por defecto en un repo | `awsorg envrc empresa-dev-admin > .envrc && direnv allow` |
| Antes de dejar trabajando a tu agente | `awsorg run --login --max 6h` |
| Trabajo sin pantalla | `awsorg run --login --max 6h -- claude -p "tarea"` |

A tu agente: *«usa los perfiles y roles de AWS que necesites»*. Si la sesión expira, te pedirá `awslogin <org>`.

### Nombres de perfil

`<org>-<cuenta>-<rol>`, por ejemplo `empresa-devsecops-admin` o `personal-main-admin`.

## Configuración (`orgs.toml`)

```toml
[[org]]
name = "empresa"
start_url = "https://empresa.awsapps.com/start"
sso_region = "us-east-1"
default_role = "admin"

[org.roles]
admin = "Administrator"
ro = "ReadOnly"

[[org.accounts]]
name = "devsecops"
id = "111111111111"
roles = ["admin", "ro"]

# Rol IAM encadenado (opcional)
[[org.assume_roles]]
name = "deploy"
role_arn = "arn:aws:iam::222222222222:role/Deploy"
source = "devsecops-admin"
```

Plantilla completa: `awsorg` la copia en el asistente. Ver también [`docs/GUIA-RAPIDA.md`](docs/GUIA-RAPIDA.md).

## Agentes de IA soportados

```bash
awsorg agent list
awsorg agent install
awsorg agent install claude codex
awsorg agent uninstall --all
awsorg agent rules
```

- **`list`:** qué agentes detecta y qué hay instalado.
- **`install`:** instala en los agentes detectados, o en los que nombres.
- **`uninstall --all`:** lo quita todo.
- **`rules`:** imprime las instrucciones, por ejemplo para pegarlas en Cursor.

| Agente | Instrucciones globales | Hook «sesión expirada» |
|---|---|---|
| Claude Code | `~/.claude/rules/aws.md` | ✅ PostToolUse / PostToolUseFailure + reglas `deny` |
| OpenAI Codex CLI | bloque en `~/.codex/AGENTS.md` | ✅ `~/.codex/hooks.json` (apruébalo con `/hooks`) |
| Kiro (CLI e IDE) | `~/.kiro/steering/aws.md` | — |
| GitHub Copilot CLI | bloque en `~/.copilot/copilot-instructions.md` | ✅ `~/.copilot/hooks/awsorg.json` |
| Gemini CLI | bloque en `~/.gemini/GEMINI.md` | ✅ AfterTool |
| Qwen Code | bloque en `~/.qwen/QWEN.md` | ✅ PostToolUse |
| OpenCode · Amp · Pi · Devin Desktop (Windsurf) | bloque en su `AGENTS.md` / `global_rules.md` | — |
| Cursor | sin archivo global: pega `awsorg agent rules` en Settings → Rules | usa los hooks de Claude Code |

Todo es idempotente, con copia de seguridad, y respeta tu configuración existente: solo añade o reemplaza lo suyo.

## Seguridad

- `awsorg` **nunca escribe tokens ni claves**: el login y la renovación los hace el AWS CLI.
- `init` y `discover` leen el token SSO del caché del CLI **solo en memoria** para listar cuentas en el portal de AWS, y nunca lo imprimen.
- Todos los valores de `orgs.toml` se validan antes de escribir `~/.aws/config`, sin posibilidad de inyectar líneas. El resultado se comprueba con un parser INI antes de reemplazar el archivo, de forma atómica y con permisos 0600.
- Las instrucciones para agentes les prohíben leer `~/.aws`, exportar claves o lanzar logins interactivos. En Claude Code eso se refuerza con reglas `deny`.
- Si el usuario no está cuando termina la sesión del portal, el agente se detiene con AWS (por diseño del SSO). Para trabajo desatendido de infraestructura, usa pipelines con roles propios (OIDC).

## Limitaciones

- El tiempo restante de la sesión del portal es una **estimación** desde el último `awslogin` (`portal_session_hours`), porque AWS no lo expone.
- `awsorg status` puede dar ✓ con credenciales de rol en caché, aunque el portal ya haya expirado. El hook corrige el estado en cuanto un comando falla.
- Solo Linux, WSL y macOS.

## Desarrollo

```bash
uv run --group dev pytest
uv run --group dev ruff check src tests
uv build
```

Licencia: [MIT](LICENSE).
