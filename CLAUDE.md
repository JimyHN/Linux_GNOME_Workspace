# Linux_GNOME_Workspace

Replica el escritorio GNOME del ordenador de referencia en una máquina nueva:
extensiones con su configuración individual, atajos de teclado, tema y
Sublime Text.

**El fondo de pantalla no se toca y no se va a tocar.** Es cosa de cada
máquina. No añadas `/org/gnome/desktop/background/` a `data/dconf/`, ni
imágenes a `data/`, ni un paso que las copie. Se intentó y se quitó.

El origen es Ubuntu y **el destino habitual es una VM de Kali**. No es el
mismo sistema: Kali es Debian, y los nombres de paquete, los UUID de las
extensiones del sistema y las aplicaciones del dash cambian. Todo lo que
dependa de la distro va en `data/distros.json`, nunca escrito en el código.

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

## Diferencias entre Ubuntu y Kali

`detectar_distro()` lee `/etc/os-release` y busca el perfil por `ID` y luego
por cada `ID_LIKE`. Kali declara `ID=kali`, `ID_LIKE=debian`, así que cae en
el perfil `debian`.

| | Ubuntu | Debian / Kali |
|---|---|---|
| Extensiones de la distro | `gnome-shell-ubuntu-extensions` | `gnome-shell-extension-desktop-icons-ng`, `gnome-shell-extension-tiling-assistant` |
| UUID de tiling-assistant | `tiling-assistant@ubuntu.com` | `tiling-assistant@leleat-on-github` |
| Terminal del dash | `org.gnome.Ptyxis.desktop` | no existe |
| Firefox | snap, `firefox_firefox.desktop` | `firefox-esr.desktop` |
| Tema Yaru | preinstalado | en los repos (`yaru-theme-gtk`) |
| Variante de iconos | `Yaru-magenta-dark` existe | puede que solo `Yaru-magenta` |

Son forks distintos de la misma extensión, por eso el UUID no coincide;
`_mapear_uuids()` lo traduce antes de escribir `enabled-extensions`. Los
favoritos que no existan se caen del dash con `_filtrar_favoritos()` en vez
de dejar huecos muertos.

**Los temas se degradan solos.** Ubuntu genera `Yaru-magenta-dark` para GTK
y para iconos; fuera de Ubuntu el paquete de Yaru no tiene por qué generar
las mismas variantes, y una clave que apunte a un tema inexistente hace que
GNOME caiga en su defecto sin decir nada. `_ajustar_temas()` comprueba que
el tema exista y si no prueba `Yaru-magenta`, luego `Yaru-dark`, y si no hay
nada quita la clave para no pisar el tema propio de la distro.

**Los paquetes se instalan uno a uno.** En una sola llamada a `apt-get
install`, un paquete inexistente tumba la instalación de todos los demás:
pedir un paquete de Ubuntu en Kali te dejaba sin `curl` ni `dconf-cli`.
`_instalar_paquetes()` va de uno en uno y tolera fallos.

## Lo que no se transfiere tal cual

Tres cosas del equipo de referencia no son válidas en otra máquina. Si tocas
estas áreas, acuérdate de que hay código que las compensa:

**Identificador de monitor.** `dash-to-panel` guarda tamaño, posición y
anclaje del panel en un JSON indexado por monitor (aquí `BNQ-A1S0292601Q`).
`clave_monitor_d2p()` calcula la clave de esta máquina reproduciendo el
algoritmo de la extensión, copiado de su `panelSettings.js`:

```js
let [connector, vendor, product, serial] = logicalMonitor[5][0]
let id = i
if (vendor && serial) id = `${vendor}-${serial}`
if (ids[id]) id = connector && !ids[connector] ? connector : i
```

O sea `VENDOR-SERIAL` cuando los hay y el **índice del monitor lógico**
cuando no, que es el caso de una VM: la clave acaba siendo `"0"`. Los datos
se piden a `org.gnome.Mutter.DisplayConfig.GetCurrentState`, la misma fuente
que usa la extensión.

Validación: ejecutado en el equipo de origen devuelve `BNQ-A1S0292601Q`,
exactamente la clave que dash-to-panel había escrito por su cuenta. Si
cambias este cálculo, comprueba eso mismo.

**Rutas absolutas.** Varias claves guardan rutas con el nombre de usuario
dentro: el `active-profile` de `burn-my-windows` y los `picture-uri` del
fondo y la pantalla de bloqueo. El exportador las deja como `@LGW_HOME@`
(`anonimizar()`) y `paso_dconf()` las resuelve al cargar, así el repo público
no lleva el usuario y la ruta vale en cualquier máquina.

**`enabled-extensions` se suma, no se sustituye.** Es el fallo que más veces
se ha repetido. La lista del repo no incluye ningún dock, porque en el equipo
de origen está apagado a favor de `dash-to-panel`. Escribirla tal cual apagaba
el dock de la distro —`ubuntu-dock` en Ubuntu, `dash-to-dock` en Kali— y si
además `dash-to-panel` no llegaba a instalarse, la sesión se quedaba **sin
ninguna barra**. Pasó en Kali.

`_fusionar_habilitadas()` construye la lista sumando las del repo que estén
instaladas más las que ya estuvieran activas, y solo apaga algo cuando su
reemplazo está instalado de verdad. El mapa de reemplazos está en
`data/distros.json` → `reemplaza`. Si añades una extensión que sustituye a
otra, declárala ahí; no la quites de la lista a mano.

**Atajos.** Los `customN` de `11-media-keys.ini` tienen que ir numerados sin
huecos y listados en el mismo orden en `custom-keybindings`: GNOME ignora
toda entrada cuya ruta no aparezca ahí. Al quitar uno hay que renumerar los
siguientes.

## Instalar extensiones: por qué vía el Shell

`paso_extensiones` le pide al Shell que las instale, con
`org.gnome.Shell.Extensions.InstallRemoteExtension` por D-Bus. Es lo mismo
que hace el interruptor de extensions.gnome.org en el navegador: GNOME
descarga, instala **y carga** la extensión de una vez.

No es un capricho. Dejar los ficheros en `~/.local/share/gnome-shell/extensions`
funciona, pero el Shell no se entera:

- solo escanea extensiones al arrancar;
- `ReloadExtension` responde `ReloadExtension is deprecated and does not work`;
- en Wayland no se puede reiniciar el Shell sin cerrar sesión.

El precio es un diálogo de confirmación por extensión, el mismo que sale al
instalarla desde el navegador. `--zip` usa la descarga directa, sin diálogos,
pero entonces hay que cerrar sesión. Si no hay Shell en el bus de sesión, se
cae a `--zip` solo.

## `gnome-extensions list` miente

`extensiones_instaladas()` lee el **disco**, no el Shell. No lo cambies por
`gnome-extensions list`.

Ese comando pregunta al Shell por D-Bus, y el Shell solo conoce las
extensiones que escaneó al arrancar. En Wayland no se puede reiniciar sin
cerrar sesión, así que una extensión recién instalada **no aparece ahí**
aunque esté en disco con su `metadata.json` válido. Comprobado creando una
carpeta a mano: `list` no la ve, el disco sí.

Esto causó el fallo que más tiempo costó encontrar. Las nueve extensiones se
descargaban e instalaban bien —`gnome-extensions install` devolvía 0— y acto
seguido `paso_dconf` las descartaba todas por "no instaladas", así que no se
habilitaba ninguna. Con dash-to-panel entre las descartadas, el escritorio
quedaba sin barra.

Por lo mismo, `_instalar_zip()` no se fía del código de salida: comprueba que
exista `metadata.json` en el destino y, si no está, descomprime a mano.

## Diagnóstico

`python3 LGW_installer.py -d` prueba la cadena completa con una sola
extensión —API, descarga, validez del zip, instalación— e informa de dónde
se rompe. Es lo primero que hay que pedir cuando alguien dice que las
extensiones no se instalan, en vez de teorizar: ya se descartaron por ese
camino el User-Agent, la falta de build por versión de GNOME y el 404.

## sudo

`paso_comprobaciones` pide la contraseña una sola vez con `sudo -v` y aborta
con un mensaje claro si falla. No se deja para el primer `apt`: ahí sudo
reintenta tres veces desde dentro de un subproceso con la salida capturada,
y desde fuera parece que el instalador se ha colgado.

`Ctx.correr()` además reconoce los fallos de autenticación que lleguen más
tarde. **Los mensajes de sudo están traducidos**, así que la lista de patrones
cubre las dos formas: `3 incorrect password attempts` y `3 intentos
incorrectos de contraseña`. Si añades patrones, añade los dos idiomas.

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
