# Configuración de Claude Code

- `settings.json` — permisos del proyecto, versionado y compartido.
  Los comandos de lectura de GNOME (`dconf dump`, `gsettings get`, …) están
  permitidos; los que **escriben** en la sesión en vivo requieren confirmación,
  y `dconf load` / `dconf reset` están denegados porque pueden dejar el
  escritorio inservible.
- `settings.local.json` — ajustes personales de cada máquina. Ignorado por git.

El contexto del proyecto (estructura, convenciones, reglas) vive en el
`CLAUDE.md` de la raíz, no aquí.
