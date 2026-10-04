# Linux_GNOME_Workspace

Repositorio de configuración personal (dotfiles) del escritorio GNOME: ajustes de
`dconf`/GSettings, extensiones de GNOME Shell, temas y scripts de instalación y
respaldo. El objetivo es poder reconstruir el entorno completo en una máquina
nueva ejecutando los scripts de este repo.

## Entorno de referencia

| Dato | Valor |
|---|---|
| Distribución | Ubuntu (kernel 7.0.0-34-generic) |
| Escritorio | GNOME |
| Shell del usuario | zsh |
| Ruta local | `~/Documentos/Linux_GNOME_Workspace` |

Antes de asumir versiones, compruébalo:

```bash
gnome-shell --version
echo $XDG_CURRENT_DESKTOP $XDG_SESSION_TYPE   # p. ej. "GNOME wayland"
lsb_release -ds
```

## Estructura prevista

```
dconf/        Volcados de dconf por rama (.ini), uno por área
extensions/   Extensiones de GNOME Shell propias o forks
themes/       Temas GTK/Shell e iconos
scripts/      install.sh, backup.sh, restore.sh y utilidades
packages/     Listas de paquetes (apt, flatpak, snap)
config/       Ficheros sueltos de ~/.config que no son dconf
```

Crea las carpetas cuando haya contenido real que poner en ellas, no antes.

## Reglas de trabajo

**Nunca apliques cambios al escritorio del usuario sin que lo pida.** Comandos
como `dconf load`, `dconf reset -f`, `gsettings set` o `gnome-extensions
disable` modifican la sesión en vivo y no son triviales de revertir. Escribe el
script o el `.ini`, explica qué hace, y deja que el usuario lo ejecute.

**Leer sí, escribir no.** `dconf dump`, `gsettings get`, `gnome-extensions list`
y `flatpak list` son seguros y se usan libremente para inspeccionar el estado.

**Volcados de dconf acotados por rama.** Nunca `dconf dump /` entero: arrastra
rutas de aplicaciones, historiales y datos personales. Usa ramas concretas:

```bash
dconf dump /org/gnome/desktop/     > dconf/desktop.ini
dconf dump /org/gnome/shell/       > dconf/shell.ini
dconf dump /org/gnome/settings-daemon/plugins/media-keys/ > dconf/media-keys.ini
```

**Revisa los volcados antes de commitear.** Los `.ini` de dconf pueden contener
rutas absolutas con el nombre de usuario, tokens de cuentas en línea, SSIDs o
listas de archivos recientes. Lee el diff completo de cualquier `.ini` antes de
añadirlo.

**Los scripts son idempotentes.** `install.sh` debe poder ejecutarse dos veces
seguidas sin romper nada. Usa `set -euo pipefail` y comprueba antes de crear.

**Nada de secretos en el repo.** Ni claves SSH/GPG, ni `~/.config` de
aplicaciones con credenciales, ni tokens. El repositorio es público.

## Convenciones

- Scripts en bash con `set -euo pipefail` y shebang `#!/usr/bin/env bash`.
- Los scripts se validan con `shellcheck` antes de commitear.
- Mensajes de commit en español, imperativo y con prefijo de área:
  `dconf: añadir atajos de teclado del workspace`.
- Rutas en los scripts relativas a la raíz del repo o vía `$HOME`, nunca
  `/home/jaime` escrito a mano.
