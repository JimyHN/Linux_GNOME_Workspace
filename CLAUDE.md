# Linux_GNOME_Workspace

Replica el escritorio GNOME del ordenador de referencia en una máquina nueva
(normalmente una VM recién instalada): extensiones con su configuración
individual, atajos de teclado, tema y Sublime Text.

`LGW_installer.py` lee `data/` y lo aplica. `data/` es la única fuente de
verdad y **se mantiene a mano**: si cambias algo en el escritorio de
referencia y lo quieres conservar, vuelca la rama y edita el `.ini`.

```bash
dconf dump /org/gnome/shell/extensions/vitals/   # y pégalo bajo la cabecera
```

Hubo un `LGW_export.py` que lo generaba solo. Se eliminó a petición: la
configuración ya está capturada y el mantenimiento manual es suficiente.
Si lo echas de menos, está en el historial (`git log --diff-filter=D`).

## Entorno de referencia

Capturado el 2026-10-04 (ver `data/extensions.json` → `capturado_en`):

| Dato | Valor |
|---|---|
| Distribución | Ubuntu 26.04.1 LTS |
| GNOME Shell | 50.1 |
| Sesión | Wayland |
| Tema | Yaru-magenta-dark, acento `pink`, `prefer-dark` |
| Teclado | `xkb es` |
| Extensiones de usuario | 9 (se descargan de extensions.gnome.org) |
| Extensiones del sistema | 7 (vienen en `gnome-shell-ubuntu-extensions`) |
| Ramas de dconf | 19 ficheros, 160 claves |

El instalador **no** fija la versión de GNOME: pide a extensions.gnome.org la
build correspondiente al Shell de la máquina destino. Así el repo no caduca
cuando Ubuntu sube de versión.

## Estructura

```
LGW_installer.py          Instalador. Punto de entrada en la VM.
lgw/ui.py                 Salida en color.
data/
  extensions.json         Extensiones de usuario + entorno de captura
  dconf/*.ini             Una rama de dconf por fichero
  backgrounds/            Fondos a los que apuntan las claves picture-uri
  burn-my-windows/*.conf  Perfiles de efectos
```

### Formato de `data/dconf/*.ini`

Cada fichero es un volcado de `dconf dump` con una cabecera que declara su
destino. El instalador lee esa cabecera; **no** hay un manifiesto aparte que
pueda desincronizarse.

```ini
# dconf-path: /org/gnome/shell/extensions/vitals/
[/]
hot-sensors=['_system_load_1m_', '_memory_usage_']
```

El prefijo numérico fija el orden de aplicación: `00-`–`07-` escritorio,
`10-`–`13-` atajos, `30-ext-` extensiones.

Dos capturas que parecen redundantes y no lo son: `12-mutter` toma la rama
**entera**, no solo `keybindings/`, porque `edge-tiling` vive en la raíz y
`tiling-assistant` lo da por hecho (lo apunta en su `overridden-settings`).
Y `05-input-sources` lleva la distribución de teclado: sin ella la VM
arranca en US y es de las cosas más molestas de arreglar a mano.

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

**Rutas absolutas.** Varias claves guardan rutas con el nombre de usuario
dentro: el `active-profile` de `burn-my-windows` y los `picture-uri` del
fondo y la pantalla de bloqueo. El exportador las deja como `@LGW_HOME@`
(`anonimizar()`) y `paso_dconf()` las resuelve al cargar, así el repo público
no lleva el usuario y la ruta vale en cualquier máquina.

**Orden de los fondos.** `paso_fondos` va **antes** que `paso_dconf` a
propósito: si se escribe `picture-uri` apuntando a un fichero que todavía no
existe, el escritorio se queda en negro hasta el siguiente arranque.

**Extensiones que no se llegaron a instalar.** `01-shell.ini` fija
`enabled-extensions`, y esa lista **no** incluye `ubuntu-dock` porque en el
equipo de origen está apagado a favor de `dash-to-panel`. Si dash-to-panel
no se instala y se aplica la lista tal cual, GNOME desactiva el dock y no
pone nada en su lugar: sesión sin barra y sin dock, peor que no haber tocado
nada. `_filtrar_habilitadas()` quita de la lista lo que no esté realmente
instalado, así el dock que hubiera sobrevive. Fue un fallo real en una VM.

**Atajos.** Los `customN` de `11-media-keys.ini` tienen que ir numerados sin
huecos y listados en el mismo orden en `custom-keybindings`: GNOME ignora
toda entrada cuya ruta no aparezca ahí. Al quitar uno hay que renumerar los
siguientes.

## Reglas de trabajo

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

**Todo paso que modifique algo tiene que ser reversible.** `paso_respaldo`
fotografía las ramas de dconf antes de tocarlas y `--revert` las restaura.
Si añades un paso que escribe fuera de dconf, apunta lo que creó en el
manifiesto (`extensiones_puestas`, `fondos_puestos`) para que `revertir()`
pueda deshacerlo. El respaldo vive en `$XDG_STATE_HOME/LGW`, **fuera** del
repo: dentro, un `git clean` se lo llevaría justo cuando hace falta. No se
sobrescribe en ejecuciones posteriores, porque debe reflejar la máquina
virgen.

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
