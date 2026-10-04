# Linux_GNOME_Workspace

Replica el escritorio GNOME del ordenador de referencia en una máquina nueva
(normalmente una VM recién instalada): extensiones con su configuración
individual, atajos de teclado, tema y Sublime Text.

Son dos scripts que se miran al espejo:

| Script | Dónde se ejecuta | Qué hace |
|---|---|---|
| `LGW_export.py` | Ordenador de referencia | Lee el sistema y escribe `data/`. Solo lee. |
| `LGW_installer.py` | VM destino | Lee `data/` y lo aplica. |

`data/` es la única fuente de verdad y se **genera**, no se escribe a mano.
Si cambias algo en el escritorio de referencia y quieres conservarlo, ejecuta
`LGW_export.py` y commitea el diff.

## Entorno de referencia

Capturado el 2026-10-04 (ver `data/extensions.json` → `capturado_en`):

| Dato | Valor |
|---|---|
| Distribución | Ubuntu 26.04.1 LTS |
| GNOME Shell | 50.1 |
| Sesión | Wayland |
| Tema | Yaru-magenta-dark, acento `pink`, `prefer-dark` |
| Extensiones de usuario | 9 (se descargan de extensions.gnome.org) |
| Extensiones del sistema | 7 (vienen en `gnome-shell-ubuntu-extensions`) |

El instalador **no** fija la versión de GNOME: pide a extensions.gnome.org la
build correspondiente al Shell de la máquina destino. Así el repo no caduca
cuando Ubuntu sube de versión.

## Estructura

```
LGW_installer.py          Instalador. Punto de entrada en la VM.
LGW_export.py             Exportador. Se ejecuta en el equipo de referencia.
lgw/ui.py                 Salida en color, compartida por los dos.
data/
  extensions.json         Extensiones de usuario + entorno de captura
  dconf/*.ini             Una rama de dconf por fichero
  burn-my-windows/*.conf  Perfiles de efectos
```

### Formato de `data/dconf/*.ini`

Cada fichero es un volcado de `dconf dump` con una cabecera que declara su
destino. El instalador lee esa cabecera; **no** hay un manifiesto aparte que
pueda desincronizarse.

```ini
# dconf-path: /org/gnome/shell/extensions/vitals/
# Generado por LGW_export.py — no editar a mano.
[/]
hot-sensors=['_system_load_1m_', '_memory_usage_']
```

El prefijo numérico fija el orden de aplicación: `00-` escritorio, `10-`
atajos, `30-ext-` extensiones.

## Lo que no se transfiere tal cual

Tres cosas del equipo de referencia no son válidas en otra máquina. Si tocas
estas áreas, acuérdate de que hay código que las compensa:

**Identificador de monitor.** `dash-to-panel` guarda tamaño, posición y
anclaje del panel en un JSON indexado por `VENDOR-SERIAL` (aquí
`BNQ-A1S0292601Q`). `_remapear_monitor()` lo sustituye por el monitor de la
VM; si no puede identificarlo —lo normal en virtualizado— **borra solo esas
cinco claves** y conserva las otras 33 (colores, estilo de los puntos,
márgenes), porque un panel con el color correcto y tamaño por defecto es mejor
que uno sin configurar.

**Rutas absolutas.** `burn-my-windows` guarda en dconf la ruta completa de su
perfil activo, con el nombre de usuario dentro. `paso_retoques()` copia el
`.conf` y reescribe la clave.

**Extensiones huérfanas.** Desinstalar una extensión deja su UUID en
`enabled-extensions` y sus ajustes en dconf. El exportador los descarta al
vuelo (`limpiar_enabled()`), por eso `search-light` y `just-perfection`
aparecen en este escritorio pero no en `data/`.

## Reglas de trabajo

**El exportador nunca escribe en el escritorio.** Solo `dconf dump`,
`gsettings get` y lecturas de `~/.local/share`. Si necesitas añadir una
captura, que sea de lectura.

**El instalador no se ejecuta con sudo.** La configuración es del usuario; con
sudo acabaría en el dconf de root y el escritorio no cambiaría. Lo comprueba y
aborta. Para apt llama a `sudo` por dentro.

**No apliques cambios al escritorio del usuario sin que lo pida.** Para probar
`dconf load` usa una rama de usar y tirar
(`/org/gnome/shell/extensions/lgw-test-scratch/`) y bórrala con `dconf reset
-f` al acabar. Nunca pruebes contra las ramas reales.

**Todo paso nuevo va en `PASOS`.** La lista de tuplas
`(nombre, descripción, función)` alimenta la ejecución, `--only`, `--skip` y
`--list-steps` a la vez. Un paso recibe un `Ctx` y lanza `Fallo` para abortar
solo ese paso; cualquier otra excepción tumba el instalador.

**Los pasos son idempotentes.** Se ejecutan dos veces seguidas sin romper
nada: comprueban si la extensión ya está, si el paquete ya está instalado.

**Añade siempre `--dry-run`.** Un paso nuevo tiene que respetar `x.dry_run`.
`Ctx.correr()` ya lo hace por ti; si escribes ficheros directamente con
`shutil` o `Path.write_text`, protégelo a mano.

**Nada de secretos en el repo.** Es público. Los volcados de dconf pueden
traer rutas con el usuario, SSIDs o tokens de cuentas en línea: lee el diff
completo de cualquier `.ini` antes de commitear. Los certificados de
emparejamiento de GSConnect (`~/.config/gsconnect`) no se capturan nunca.

## Salida en terminal

`lgw/ui.py` tiene la paleta. **No se usa blanco en ningún sitio**, ni para el
texto corriente: va en lavanda (`198,184,214`) y los apagados en malva. Es un
requisito del proyecto, no una preferencia estética. Usa los métodos de
`Consola` (`accion`, `ok`, `aviso`, `error`, `info`, `saltado`) en vez de
`print`, y respeta `NO_COLOR` y la detección de tty.

## Convenciones

- Python 3.11+, solo biblioteca estándar. Nada de dependencias: el script
  tiene que correr en una VM recién instalada sin `pip install` previo.
- Código, mensajes y comentarios en español. Los identificadores de Python
  también (`paso_extensiones`, `Fallo`, `Consola`).
- Los literales de código van sin tildes para evitar problemas de codificación
  en terminales de VM; los textos de la interfaz sí las llevan.
- Commits en español, imperativo, con prefijo de área:
  `installer: remapear el monitor de dash-to-panel`.
