# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/); versionado [SemVer](https://semver.org/lang/es/).

## [1.2.0] - 2026-11-06
Primera versión publicada como paquete de Python.

### Añadido
- `awsorg setup`: todo en uno (PATH y direnv en la shell, asistente si falta configuración, perfiles y agentes de IA).
- `awsorg agent list|install|uninstall|rules`: instrucciones y hooks para Claude Code, Codex, Kiro, Copilot CLI, Gemini, Qwen, OpenCode, Amp, Pi, Devin Desktop/Windsurf y Cursor.
- `awsorg hook post-tool` y `awsorg hook cwd`: hooks en Python (sin jq), con formato por agente.
- `awsorg run`: comprueba y renueva la sesión antes de dejar trabajando a un agente; avisa de bloqueos al terminar.
- `awslogin` como comando del paquete.
- `envrc --state-var` para elegir la variable del perfil de estado remoto.

### Heredado de las versiones de script (1.0–1.1)
- Perfiles `sso-session` declarativos para N organizaciones, asistente `init` con descubrimiento por la API del portal (stdlib), `discover`, `status`, `login`, validación completa, escritura atómica, symlinks, copias de seguridad.
