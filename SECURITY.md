# Seguridad

## Reportar una vulnerabilidad
Usa **GitHub → Security → Report a vulnerability** (aviso privado) en este repositorio. No abras un issue público para fallos de seguridad.

## Modelo de seguridad
- `awsorg` no almacena ni escribe tokens o claves. El login y la renovación los hace el AWS CLI.
- `init` y `discover` leen el token SSO del caché del AWS CLI solo en memoria para consultar el portal de AWS (`portal.sso.<región>.amazonaws.com`) y nunca lo imprimen ni lo registran.
- `orgs.toml` no contiene secretos. Aun así, se validan todos sus valores antes de escribir `~/.aws/config`.
- Las instrucciones instaladas en agentes de IA les prohíben leer `~/.aws` y exportar credenciales. Considéralas una guía, no una barrera: protege tu equipo con los mecanismos de cada agente (sandbox, reglas de permisos).
