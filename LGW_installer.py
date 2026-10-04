#!/usr/bin/env python3
"""Replica en esta maquina el escritorio GNOME definido en data/.

Pensado para una VM recien instalada: clonas el repo, lo ejecutas y te deja
las extensiones, sus ajustes individuales, los atajos de teclado, el tema y
Sublime Text tal y como estan en el ordenador de referencia.

El fondo de pantalla NO se toca: es cosa de cada maquina.

    git clone https://github.com/JimyHN/Linux_GNOME_Workspace.git
    cd Linux_GNOME_Workspace
    python3 LGW_installer.py

Antes de tocar nada guarda un respaldo, asi que siempre se puede deshacer con
    python3 LGW_installer.py --revert
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lgw.ui import Consola  # noqa: E402

RAIZ = Path(__file__).resolve().parent
DATA = RAIZ / "data"
EGO = "https://extensions.gnome.org"

# El respaldo vive fuera del repo: si se guardase dentro, un git clean o un
# pull con conflictos se lo llevaria por delante justo cuando hace falta.
RESPALDO = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "LGW"
MANIFIESTO = RESPALDO / "manifiesto.json"

# Los nombres de paquete y los UUID de las extensiones del sistema cambian
# entre distribuciones: viven en data/distros.json, no aqui.
PERFIL_POR_DEFECTO = "debian"

MARCADOR_HOME = "@LGW_HOME@"

D2P_CLAVES_POR_MONITOR = [
    "panel-anchors", "panel-element-positions", "panel-lengths",
    "panel-positions", "panel-sizes",
]

# extensions.gnome.org responde 403 a algunos clientes sin User-Agent.
CABECERAS = {"User-Agent": "LGW-installer (+https://github.com/JimyHN/Linux_GNOME_Workspace)"}


class Fallo(Exception):
    """Error que aborta un paso pero no el instalador entero."""


def abrir(url: str, timeout: int = 30):
    return urllib.request.urlopen(urllib.request.Request(url, headers=CABECERAS), timeout=timeout)


# ---------------------------------------------------------------------------
# contexto
# ---------------------------------------------------------------------------

class Ctx:
    def __init__(self, consola: Consola, dry_run: bool, forzar_zip: bool = False):
        self.c = consola
        self.dry_run = dry_run
        self.forzar_zip = forzar_zip
        self.hechos = 0
        self.fallos = 0
        self.notas: list[str] = []
        self.extensiones_puestas: list[str] = []

    def correr(self, cmd: list[str], *, root: bool = False, entrada: str | None = None,
               tolerante: bool = False) -> str:
        if root:
            cmd = ["sudo", "-n", *cmd] if self._sudo_sin_clave() else ["sudo", *cmd]
        if self.dry_run:
            self.c.detalle("(dry-run) " + " ".join(cmd))
            return ""
        r = subprocess.run(cmd, input=entrada, capture_output=True, text=True)
        if r.stdout:
            self.c.detalle(r.stdout)
        if r.returncode != 0:
            if r.stderr:
                self.c.detalle(r.stderr)
            motivo = (r.stderr or r.stdout or "").strip()
            # sudo traduce sus mensajes, asi que hay que cubrir las dos formas.
            if any(p in motivo.lower() for p in
                   ("incorrect password", "contraseña incorrecta", "sorry, try again",
                    "authentication failure", "a password is required",
                    "intentos incorrectos de contraseña", "incorrect password attempts",
                    "se necesita una contraseña", "lo siento, int")):
                raise Fallo("contraseña de sudo incorrecta o caducada; "
                            "vuelve a lanzar el instalador")
            if not tolerante:
                raise Fallo(motivo.splitlines()[-1] if motivo else f"codigo {r.returncode}")
        return r.stdout

    _sudo_ok: bool | None = None

    def _sudo_sin_clave(self) -> bool:
        if Ctx._sudo_ok is None:
            Ctx._sudo_ok = subprocess.run(["sudo", "-n", "true"],
                                          capture_output=True).returncode == 0
        return Ctx._sudo_ok


# ---------------------------------------------------------------------------
# utilidades
# ---------------------------------------------------------------------------

def detectar_distro(os_release: Path = Path("/etc/os-release")) -> tuple[str, dict]:
    """Devuelve (nombre legible, perfil) segun /etc/os-release.

    Kali declara ID=kali e ID_LIKE=debian, asi que se busca primero por ID
    y luego por cada ID_LIKE. Sin esto el instalador pide paquetes de
    Ubuntu en Kali y falla entero el paso de paquetes."""
    campos: dict[str, str] = {}
    try:
        for linea in os_release.read_text().splitlines():
            if "=" in linea:
                k, v = linea.split("=", 1)
                campos[k.strip()] = v.strip().strip('"')
    except OSError:
        pass

    try:
        perfiles = json.loads((DATA / "distros.json").read_text())
    except (OSError, json.JSONDecodeError):
        raise Fallo("no se pudo leer data/distros.json")

    candidatos = [campos.get("ID", "")] + campos.get("ID_LIKE", "").split()
    for c in candidatos:
        if c and c in perfiles:
            return campos.get("PRETTY_NAME", c), perfiles[c]
    return (campos.get("PRETTY_NAME", "desconocida"),
            perfiles.get(PERFIL_POR_DEFECTO, {}))


def desktop_existe(nombre: str) -> bool:
    """Si hay un .desktop con ese nombre en las rutas habituales."""
    bases = [
        Path("/usr/share/applications"),
        Path("/usr/local/share/applications"),
        Path.home() / ".local/share/applications",
        Path("/var/lib/snapd/desktop/applications"),
        Path("/var/lib/flatpak/exports/share/applications"),
        Path.home() / ".local/share/flatpak/exports/share/applications",
    ]
    return any((b / nombre).exists() for b in bases)


def version_shell() -> str:
    """Version mayor de GNOME Shell de ESTA maquina."""
    try:
        salida = subprocess.run(["gnome-shell", "--version"],
                                capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        raise Fallo("no se pudo determinar la version de GNOME Shell")
    m = re.search(r"(\d+)(?:\.\d+)*", salida)
    if not m:
        raise Fallo(f"version de GNOME Shell no reconocida: {salida.strip()}")
    return m.group(1)


def leer_ini(ruta: Path) -> tuple[str, str]:
    texto = ruta.read_text()
    m = re.search(r"^#\s*dconf-path:\s*(\S+)", texto, re.M)
    if not m:
        raise Fallo(f"{ruta.name} no declara '# dconf-path:'")
    return m.group(1), texto


def ramas_objetivo() -> list[tuple[Path, str]]:
    """Los .ini de data/dconf con la rama a la que va cada uno."""
    carpeta = DATA / "dconf"
    if not carpeta.is_dir():
        return []
    return [(f, leer_ini(f)[0]) for f in sorted(carpeta.glob("*.ini"))]


# Forma de un monitor logico en la respuesta de GetCurrentState:
# (x, y, escala, uint32 transform, primario, [(connector, vendor, product, serial)], {...})
_RE_LOGICO = re.compile(
    r"\((-?\d+), (-?\d+), ([\d.]+), uint32 (\d+), (true|false), \[\((.*?)\)\]")


def clave_monitor_d2p() -> str | None:
    """Calcula la clave con la que dash-to-panel indexa este monitor.

    Reproduce tal cual lo que hace su panelSettings.js (setMonitorsInfo):

        let [connector, vendor, product, serial] = logicalMonitor[5][0]
        let id = i
        if (vendor && serial) id = `${vendor}-${serial}`
        if (ids[id]) id = connector && !ids[connector] ? connector : i

    Es decir VENDOR-SERIAL cuando los hay, y si no el indice del monitor
    logico. En una VM no suele haber ni vendor ni serial, asi que la clave
    acaba siendo "0" — adivinarla permite dejar el panel con el tamaño y la
    posicion correctos sin que el usuario toque nada.
    """
    try:
        salida = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.DisplayConfig",
             "--object-path", "/org/gnome/Mutter/DisplayConfig",
             "--method", "org.gnome.Mutter.DisplayConfig.GetCurrentState"],
            capture_output=True, text=True, check=True, timeout=20).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None

    vistos: set[str] = set()
    claves: list[tuple[str, bool]] = []
    for i, m in enumerate(_RE_LOGICO.finditer(salida)):
        campos = re.findall(r"'([^']*)'", m.group(6))
        connector = campos[0] if len(campos) > 0 else ""
        vendor = campos[1] if len(campos) > 1 else ""
        serial = campos[3] if len(campos) > 3 else ""

        clave = str(i)
        if vendor and serial:
            clave = f"{vendor}-{serial}"
        if clave in vistos:
            clave = connector if connector and connector not in vistos else str(i)
        vistos.add(clave)
        claves.append((clave, m.group(5) == "true"))

    if not claves:
        return None
    return next((c for c, primario in claves if primario), claves[0][0])


def extensiones_instaladas() -> set[str]:
    """Lee el DISCO, no el Shell. No cambies esto por 'gnome-extensions list'.

    'gnome-extensions list' pregunta al Shell por D-Bus y el Shell solo
    conoce lo que escaneo al arrancar. En Wayland no se puede reiniciar sin
    cerrar sesion, asi que una extension recien instalada NO aparece ahi,
    aunque este en disco con su metadata.json. Comprobado.

    Era la causa de que no se habilitara ninguna: se instalaban las nueve
    correctamente y acto seguido se descartaban por "no instaladas"."""
    encontradas: set[str] = set()
    bases = [Path.home() / ".local/share/gnome-shell/extensions"]
    for d in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":"):
        if d:
            bases.append(Path(d) / "gnome-shell/extensions")
    for base in bases:
        if not base.is_dir():
            continue
        for carpeta in base.iterdir():
            if (carpeta / "metadata.json").is_file():
                encontradas.add(carpeta.name)
    return encontradas


# ---------------------------------------------------------------------------
# pasos
# ---------------------------------------------------------------------------

def paso_comprobaciones(x: Ctx) -> None:
    c = x.c
    if os.geteuid() == 0:
        raise Fallo("no lo ejecutes con sudo: la configuracion es del usuario, "
                    "no de root (el script pedira sudo solo para apt)")

    sesion = os.environ.get("XDG_CURRENT_DESKTOP", "")
    if "GNOME" not in sesion.upper():
        c.aviso(f"la sesion actual es '{sesion or 'desconocida'}', no GNOME")
    else:
        c.ok(f"sesion GNOME detectada ({os.environ.get('XDG_SESSION_TYPE', '?')})")

    nombre, perfil = detectar_distro()
    if perfil:
        c.ok(f"{nombre} → perfil '{perfil.get('nombre', '?')}'")
    else:
        c.aviso(f"{nombre}: no hay perfil para esta distro; se usara el de Debian")

    ver = version_shell()
    c.ok(f"GNOME Shell {ver}")

    # Sin estas dos cosas el paso de extensiones no puede hacer nada, y vale
    # mas decirlo ahora que fallar nueve veces seguidas mas abajo.
    if not shutil.which("gnome-extensions"):
        raise Fallo("falta el comando 'gnome-extensions' (paquete gnome-shell); "
                    "sin el no se pueden instalar extensiones")
    c.ok("comando gnome-extensions disponible")

    try:
        with abrir(f"{EGO}/extension-info/?uuid=user-theme%40gnome-shell-extensions"
                   f".gcampax.github.com&shell_version={ver}", timeout=15):
            pass
        c.ok("extensions.gnome.org responde")
    except urllib.error.HTTPError as e:
        c.ok(f"extensions.gnome.org responde (HTTP {e.code})")
    except urllib.error.URLError as e:
        raise Fallo(f"no hay acceso a extensions.gnome.org ({e.reason}); "
                    "comprueba la red de la VM antes de seguir")

    origen = {}
    if (DATA / "extensions.json").is_file():
        origen = json.loads((DATA / "extensions.json").read_text()).get("capturado_en", {})
    if origen.get("gnome_shell", "").split(".")[0] not in ("", ver):
        c.aviso(f"el repo se capturo en GNOME {origen['gnome_shell']} y aqui hay {ver}; "
                "se pediran las versiones de extension correspondientes a esta")

    if not shutil.which("sudo"):
        raise Fallo("hace falta sudo para instalar paquetes")
    if x._sudo_sin_clave():
        c.ok("sudo ya autorizado")
    elif x.dry_run:
        c.info("sudo pediria la contraseña (no se pide en simulacion)")
    else:
        # Pedirla aqui y una sola vez: si se deja para el primer apt, sudo
        # reintenta tres veces desde dentro de un subproceso con la salida
        # capturada y parece que el instalador se ha quedado colgado.
        c.info("sudo necesita tu contraseña para instalar paquetes")
        if subprocess.run(["sudo", "-v"]).returncode != 0:
            raise Fallo("contraseña incorrecta: no se ha hecho ningun cambio, "
                        "vuelve a lanzar el instalador")
        c.ok("contraseña correcta")


def paso_respaldo(x: Ctx) -> None:
    """Fotografia el estado actual antes de tocar nada, para --revert."""
    c = x.c
    ramas = ramas_objetivo()
    if not ramas:
        raise Fallo("no hay nada en data/dconf/; el repo esta incompleto")

    if MANIFIESTO.is_file():
        try:
            previo = json.loads(MANIFIESTO.read_text()).get("fecha", "?")
            c.info(f"habia un respaldo del {previo}; se conserva el original")
            c.saltado("no se sobrescribe: el respaldo debe reflejar la maquina virgen")
            return
        except json.JSONDecodeError:
            c.aviso("el respaldo anterior estaba corrupto; se rehace")

    c.accion("Guardando", f"estado actual en {RESPALDO}")
    if not x.dry_run:
        (RESPALDO / "dconf").mkdir(parents=True, exist_ok=True)

    guardadas = []
    for fichero, ruta in ramas:
        volcado = subprocess.run(["dconf", "dump", ruta],
                                 capture_output=True, text=True).stdout
        nombre = fichero.name
        if not x.dry_run:
            (RESPALDO / "dconf" / nombre).write_text(volcado)
        guardadas.append({"fichero": nombre, "ruta": ruta, "vacia": not volcado.strip()})
        c.detalle(f"{ruta} → {len(volcado.splitlines())} lineas")

    datos = {
        "fecha": time.strftime("%Y-%m-%d %H:%M:%S"),
        "gnome_shell": version_shell(),
        "ramas": guardadas,
        "extensiones_antes": sorted(extensiones_instaladas()),
        "extensiones_puestas": [],
    }
    if not x.dry_run:
        MANIFIESTO.write_text(json.dumps(datos, indent=2, ensure_ascii=False) + "\n")
    c.ok(f"{len(guardadas)} ramas respaldadas · deshacer con --revert")


def _instalar_paquetes(x: Ctx, paquetes: list[str], etiqueta: str) -> list[str]:
    """Instala uno a uno y tolerando fallos.

    En una sola llamada a apt, un paquete inexistente tumba la instalacion
    de todos los demas. Como los nombres varian entre distros, aqui eso
    significaria quedarse sin curl por pedir un paquete de Ubuntu."""
    c = x.c
    fallidos = []
    for p in paquetes:
        if subprocess.run(["dpkg", "-s", p], capture_output=True).returncode == 0:
            c.saltado(f"{p} ya instalado")
            continue
        c.accion("Instalando", f"{p}  ({etiqueta})")
        try:
            x.correr(["apt-get", "install", "-y", p], root=True)
            c.ok(f"{p}")
        except Fallo as e:
            c.aviso(f"{p}: no se pudo instalar ({e})")
            fallidos.append(p)
    return fallidos


def paso_apt_base(x: Ctx) -> None:
    c = x.c
    nombre, perfil = detectar_distro()
    c.info(f"paquetes para {perfil.get('nombre', nombre)}")

    c.accion("Refrescando", "indices de apt")
    x.correr(["apt-get", "update", "-qq"], root=True, tolerante=True)

    fallidos = _instalar_paquetes(x, perfil.get("paquetes_base", []), "base")
    if fallidos:
        x.notas.append(f"dependencias base que faltan: {', '.join(fallidos)}")

    fallidos = _instalar_paquetes(
        x, perfil.get("paquetes_extensiones", []), "extensiones de la distro")
    if fallidos:
        x.notas.append(
            f"extensiones del sistema no instaladas: {', '.join(fallidos)}. "
            "Comprueba como las empaqueta tu distro y ajusta data/distros.json")

    # El tema Yaru no viene fuera de Ubuntu, pero esta en Debian, asi que en
    # Kali tambien se puede tener el magenta del equipo de origen.
    fallidos = _instalar_paquetes(x, perfil.get("paquetes_tema", []), "tema Yaru")
    if fallidos:
        x.notas.append(
            f"el tema Yaru no se pudo instalar ({', '.join(fallidos)}); "
            "el escritorio usara el tema por defecto de la distro")


def paso_sublime(x: Ctx) -> None:
    c = x.c
    if shutil.which("subl"):
        c.saltado("Sublime Text ya esta instalado")
        return
    llavero = Path("/etc/apt/keyrings/sublimehq-pub.gpg")
    lista = Path("/etc/apt/sources.list.d/sublime-text.list")

    c.accion("Descargando", "clave GPG de sublimehq")
    if not x.dry_run:
        try:
            clave = abrir("https://download.sublimetext.com/sublimehq-pub.gpg").read()
        except urllib.error.URLError as e:
            raise Fallo(f"no se pudo descargar la clave: {e}")
        armadura = subprocess.run(["gpg", "--dearmor"], input=clave, capture_output=True)
        if armadura.returncode != 0:
            raise Fallo("gpg --dearmor fallo al procesar la clave")
        tmp = Path(tempfile.mkstemp(suffix=".gpg")[1])
        tmp.write_bytes(armadura.stdout)
        x.correr(["install", "-D", "-m", "0644", str(tmp), str(llavero)], root=True)
        tmp.unlink(missing_ok=True)
    c.ok(f"clave en {llavero}")

    c.accion("Añadiendo", "repositorio apt/stable de Sublime Text")
    x.correr(["tee", str(lista)], root=True,
             entrada=f"deb [signed-by={llavero}] https://download.sublimetext.com/ apt/stable/\n")

    c.accion("Instalando", "sublime-text")
    x.correr(["apt-get", "update", "-qq"], root=True, tolerante=True)
    x.correr(["apt-get", "install", "-y", "sublime-text"], root=True)
    c.ok("Sublime Text instalado (ejecutable: subl)")


def shell_accesible() -> bool:
    """Si hay un GNOME Shell vivo en el bus de sesion al que pedirle cosas."""
    if not shutil.which("gdbus"):
        return False
    r = subprocess.run(
        ["gdbus", "call", "--session", "--dest", "org.gnome.Shell",
         "--object-path", "/org/gnome/Shell", "--method",
         "org.freedesktop.DBus.Properties.Get",
         "org.gnome.Shell.Extensions", "ShellVersion"],
        capture_output=True, text=True)
    return r.returncode == 0


def _instalar_via_shell(x: Ctx, uuid: str) -> bool:
    """Le pide al Shell que instale la extension, como hace el navegador.

    Es la unica forma de que GNOME la cargue sin cerrar sesion. Poner los
    ficheros en ~/.local/share no basta: el Shell solo escanea al arrancar,
    ReloadExtension esta deprecado y devuelve
    'ReloadExtension is deprecated and does not work', y en Wayland no se
    puede reiniciar el Shell. InstallRemoteExtension descarga, instala y
    carga de una vez, que es lo que pasa al pulsar el interruptor en
    extensions.gnome.org.

    A cambio muestra un dialogo de confirmacion por extension, el mismo que
    sale al instalarla desde el navegador."""
    if x.dry_run:
        x.c.detalle(f"(dry-run) InstallRemoteExtension {uuid}")
        return True
    try:
        r = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.gnome.Shell",
             "--object-path", "/org/gnome/Shell", "--method",
             "org.gnome.Shell.Extensions.InstallRemoteExtension", uuid],
            capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise Fallo("el dialogo de confirmacion no se respondio en 5 minutos")
    salida = (r.stdout + r.stderr).strip()
    x.c.detalle(salida)
    if r.returncode != 0:
        raise Fallo(salida.splitlines()[-1] if salida else f"codigo {r.returncode}")
    if "cancelled" in salida:
        raise Fallo("cancelada en el dialogo de confirmacion")
    return "successful" in salida


def _instalar_zip(x: Ctx, uuid: str, datos: bytes) -> None:
    """Instala el zip con gnome-extensions y, si falla, a mano.

    'gnome-extensions install' puede no estar disponible o negarse segun la
    version; descomprimir en ~/.local/share y compilar los schemas hace
    exactamente lo mismo, asi que no merece la pena rendirse al primer error.
    """
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
        f.write(datos)
        tmp = f.name
    destino = Path.home() / ".local/share/gnome-shell/extensions" / uuid
    try:
        try:
            x.correr(["gnome-extensions", "install", "--force", tmp])
        except Fallo as e:
            x.c.detalle(f"gnome-extensions install fallo ({e}); se descomprime a mano")

        # No vale fiarse del codigo de salida: se comprueba que el
        # metadata.json este en disco, que es lo unico que importa.
        if (destino / "metadata.json").is_file():
            return

        x.c.detalle("no hay metadata.json tras el install; se descomprime a mano")
        destino.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(tmp) as z:
            z.extractall(destino)
        schemas = destino / "schemas"
        if schemas.is_dir() and shutil.which("glib-compile-schemas"):
            x.correr(["glib-compile-schemas", str(schemas)], tolerante=True)
        if not (destino / "metadata.json").is_file():
            raise Fallo(f"el zip no dejo metadata.json en {destino}")
    finally:
        Path(tmp).unlink(missing_ok=True)


def paso_extensiones(x: Ctx) -> None:
    c = x.c
    manifiesto = DATA / "extensions.json"
    if not manifiesto.is_file():
        raise Fallo("falta data/extensions.json; el repo esta incompleto")
    lista = json.loads(manifiesto.read_text()).get("extensiones_usuario", [])
    if not lista:
        c.saltado("no hay extensiones de usuario que instalar")
        return

    ver = version_shell()
    instaladas = extensiones_instaladas()
    puestas, sin_build, con_error = [], [], []

    # Por defecto se pide al Shell que las instale: es lo unico que las deja
    # cargadas sin cerrar sesion. Con --zip se bajan a mano, sin dialogos,
    # pero entonces hace falta reiniciar la sesion.
    via_shell = not x.forzar_zip and shell_accesible()
    if via_shell:
        c.info("se instalaran a traves de GNOME Shell: saldra un dialogo de "
               "confirmacion por extension y quedaran activas al momento")
    else:
        motivo = "--zip" if x.forzar_zip else "no hay GNOME Shell en el bus de sesion"
        c.info(f"descarga directa ({motivo}): sin dialogos, pero habra que "
               "cerrar sesion para que GNOME las cargue")

    for e in lista:
        uuid, nombre = e["uuid"], e["nombre"]
        if uuid in instaladas:
            c.saltado(f"{nombre} ya instalada")
            continue

        if via_shell:
            c.accion("Instalando", f"{nombre} (confirma en el dialogo)")
            try:
                _instalar_via_shell(x, uuid)
            except Fallo as err:
                c.error(f"{nombre}: {err}")
                con_error.append(nombre)
                continue
            puestas.append(uuid)
            c.ok(f"{nombre} instalada y cargada")
            continue

        c.accion("Consultando", f"{nombre}")
        url = f"{EGO}/extension-info/?uuid={urllib.parse.quote(uuid)}&shell_version={ver}"
        try:
            info = json.loads(abrir(url).read())
        except urllib.error.HTTPError as err:
            if err.code == 404:
                # Puede que la extension exista pero no para esta version de
                # GNOME. Distinguirlo cambia por completo el consejo a dar.
                try:
                    generico = json.loads(abrir(
                        f"{EGO}/extension-info/?uuid={urllib.parse.quote(uuid)}").read())
                    compat = ", ".join(sorted(generico.get("shell_version_map", {}),
                                              key=lambda v: [int(n) for n in v.split(".")]))
                    c.aviso(f"{nombre}: no hay build para GNOME {ver} "
                            f"(disponible para {compat or 'ninguna version conocida'})")
                except (urllib.error.URLError, json.JSONDecodeError, ValueError):
                    c.aviso(f"{nombre}: no hay build para GNOME {ver}")
                sin_build.append(nombre)
            else:
                c.error(f"{nombre}: HTTP {err.code} al consultar la API")
                con_error.append(nombre)
            continue
        except urllib.error.URLError as err:
            c.error(f"{nombre}: sin conexion con extensions.gnome.org ({err.reason})")
            con_error.append(nombre)
            continue
        except json.JSONDecodeError:
            c.error(f"{nombre}: la API devolvio algo que no es JSON")
            con_error.append(nombre)
            continue

        descarga = info.get("download_url")
        if not descarga:
            c.aviso(f"{nombre}: la API no da enlace de descarga para GNOME {ver}")
            sin_build.append(nombre)
            continue

        c.accion("Instalando", f"{nombre} v{info.get('version', '?')}")
        if x.dry_run:
            puestas.append(uuid)
            continue
        try:
            datos = abrir(EGO + descarga, timeout=120).read()
            c.detalle(f"{len(datos) // 1024} KB descargados")
            _instalar_zip(x, uuid, datos)
        except (urllib.error.URLError, Fallo, zipfile.BadZipFile, OSError) as err:
            c.error(f"{nombre}: {err}")
            con_error.append(nombre)
            continue
        puestas.append(uuid)

    x.extensiones_puestas = puestas
    if puestas:
        c.ok(f"{len(puestas)} extensiones instaladas")
    if sin_build:
        x.notas.append(f"sin build para GNOME {ver}: {', '.join(sin_build)}. "
                       "Instalalas a mano desde extensions.gnome.org o quitalas "
                       "de data/extensions.json")
    if con_error:
        x.fallos += len(con_error)
        x.notas.append(f"fallaron por red o instalacion: {', '.join(con_error)}. "
                       "Repite con -v para ver el detalle")
    if not puestas and not sin_build and not con_error:
        c.info("no habia nada que instalar: ya estaban todas")
    if via_shell:
        c.info("ya estan cargadas: no hace falta cerrar sesion")
    else:
        c.info("GNOME las carga al reiniciar la sesion, no antes")


def _remapear_monitor(contenido: str, monitor: str | None) -> tuple[str, str | None]:
    """Reescribe las claves de dash-to-panel al monitor de esta maquina.

    Guarda tamaño, posicion y anclaje del panel en un JSON indexado por
    monitor. Antes, si no se identificaba el monitor, se borraban esas
    claves y el panel salia con el tamaño por defecto; ahora la clave se
    calcula igual que la calcula la extension, asi que el panel queda listo
    sin tener que abrir sus preferencias."""
    claves = "|".join(D2P_CLAVES_POR_MONITOR)
    origen = set(re.findall(r'"([A-Za-z0-9]+-[A-Za-z0-9]+)"\s*:', contenido))
    if not origen:
        return contenido, None
    if monitor:
        for viejo in origen:
            contenido = contenido.replace(f'"{viejo}"', f'"{monitor}"')
        return contenido, None
    contenido = re.sub(rf"^({claves})=.*\n", "", contenido, flags=re.M)
    return contenido, ("no se pudo preguntar a Mutter por el monitor; el panel "
                       "quedara con tamaño y posicion por defecto")


def _fusionar_habilitadas(contenido: str, instaladas: set[str], previas: set[str],
                          reemplaza: dict[str, list[str]]) -> tuple[str, list[str], list[str]]:
    """Combina las extensiones del repo con las que ya estaban activas.

    Escribir la lista del repo tal cual era destructivo: en Kali apagaba su
    dash-to-dock, que no aparece en la lista porque en el equipo de origen
    el dock esta sustituido por dash-to-panel. Si encima dash-to-panel no
    llega a instalarse, la sesion se queda sin ninguna barra, que es peor
    que no haber tocado nada.

    Asi que la lista se construye sumando, no sustituyendo, y solo se apaga
    algo cuando su reemplazo esta instalado de verdad.
    """
    ausentes: list[str] = []
    apagadas: list[str] = []

    def _sub(m: re.Match) -> str:
        nuestras = re.findall(r"'([^']+)'", m.group(1))
        ausentes.extend(u for u in nuestras if u not in instaladas)

        final = {u for u in nuestras if u in instaladas} | {u for u in previas if u in instaladas}

        for sustituto, desplazadas in reemplaza.items():
            if sustituto in final:
                for d in desplazadas:
                    if d in final:
                        final.discard(d)
                        apagadas.append(d)

        # Orden estable: primero las del repo, luego las heredadas.
        orden = [u for u in nuestras if u in final] + sorted(final - set(nuestras))
        return "enabled-extensions=[" + ", ".join(f"'{u}'" for u in orden) + "]"

    return re.sub(r"enabled-extensions=\[(.*?)\]", _sub, contenido), ausentes, apagadas


def _mapear_uuids(contenido: str, mapa: dict[str, str]) -> list[tuple[str, str]]:
    """Traduce los UUID de extensiones del sistema al nombre de esta distro."""
    cambios = []
    for viejo, nuevo in mapa.items():
        if f"'{viejo}'" in contenido:
            cambios.append((viejo, nuevo))
    return cambios


def _filtrar_favoritos(contenido: str) -> tuple[str, list[str]]:
    """Quita del dash los .desktop que no existen en esta maquina.

    Los favoritos del equipo de origen son de Ubuntu: Ptyxis como terminal
    y Firefox como snap ('firefox_firefox.desktop'). En Kali ninguno de los
    dos existe y el dash saldria con huecos muertos."""
    ausentes: list[str] = []

    def _sub(m: re.Match) -> str:
        apps = re.findall(r"'([^']+)'", m.group(1))
        vivas = [a for a in apps if desktop_existe(a)]
        ausentes.extend(a for a in apps if not desktop_existe(a))
        if not ausentes:
            return m.group(0)
        return "favorite-apps=[" + ", ".join(f"'{a}'" for a in vivas) + "]"

    return re.sub(r"favorite-apps=\[(.*?)\]", _sub, contenido), ausentes


def _tema_disponible(clase: str, nombre: str) -> bool:
    sub = "themes" if clase == "gtk-theme" else "icons"
    casa = Path.home() / (".themes" if clase == "gtk-theme" else ".icons")
    return any((b / nombre).is_dir()
               for b in (Path("/usr/share") / sub, Path("/usr/local/share") / sub, casa))


def _ajustar_temas(contenido: str) -> tuple[str, list[str]]:
    """Degrada gtk-theme e icon-theme a una variante que exista aqui.

    Ubuntu trae Yaru-magenta-dark para tema e iconos, pero fuera de Ubuntu
    el paquete de Yaru no tiene por que generar las mismas variantes. Si se
    deja la clave apuntando a un tema inexistente, GNOME cae en su defecto
    sin decir nada y el escritorio sale distinto sin explicacion. Mejor
    probar 'Yaru-magenta', luego 'Yaru-dark', y si no hay nada quitar la
    clave para no pisar el tema propio de la distro."""
    avisos = []
    for clase in ("gtk-theme", "icon-theme"):
        m = re.search(rf"^{clase}='([^']+)'$", contenido, re.M)
        if not m:
            continue
        pedido = m.group(1)
        if _tema_disponible(clase, pedido):
            continue
        alternativas = [pedido.removesuffix("-dark"), "Yaru-dark", "Yaru"]
        elegido = next((a for a in alternativas if a != pedido and _tema_disponible(clase, a)), None)
        if elegido:
            contenido = re.sub(rf"^{clase}='[^']+'$", f"{clase}='{elegido}'", contenido, flags=re.M)
            avisos.append(f"{clase}: '{pedido}' no existe aqui, se usa '{elegido}'")
        else:
            contenido = re.sub(rf"^{clase}='[^']+'\n", "", contenido, flags=re.M)
            avisos.append(f"{clase}: '{pedido}' no existe y no hay alternativa Yaru; "
                          "se deja el tema de la distro")
    return contenido, avisos


def paso_dconf(x: Ctx) -> None:
    c = x.c
    ramas = ramas_objetivo()
    if not ramas:
        raise Fallo("no hay ningun .ini en data/dconf/")

    monitor = clave_monitor_d2p()
    instaladas = extensiones_instaladas()
    previas = set(subprocess.run(
        ["gsettings", "get", "org.gnome.shell", "enabled-extensions"],
        capture_output=True, text=True).stdout.strip().strip("[]").replace("'", "").split(", ")) - {""}
    _, perfil = detectar_distro()
    mapa = perfil.get("uuid_sistema", {})

    for fichero, ruta in ramas:
        contenido = fichero.read_text().replace(MARCADOR_HOME, str(Path.home()))
        etiqueta = fichero.stem.split("-", 1)[1] if "-" in fichero.stem else fichero.stem

        for viejo, nuevo in _mapear_uuids(contenido, mapa):
            contenido = contenido.replace(f"'{viejo}'", f"'{nuevo}'")
            c.info(f"{viejo} → {nuevo} (nombre en esta distro)")

        if "gtk-theme=" in contenido or "icon-theme=" in contenido:
            contenido, avisos = _ajustar_temas(contenido)
            for a in avisos:
                c.aviso(a)
                x.notas.append(a)

        if "favorite-apps=" in contenido:
            contenido, sin_app = _filtrar_favoritos(contenido)
            if sin_app:
                c.aviso(f"fuera del dash (no instaladas): {', '.join(sin_app)}")
                x.notas.append(
                    f"{len(sin_app)} favoritos del dash no existen en esta distro "
                    f"({', '.join(sin_app)}); anclalos tu con los equivalentes")

        if "enabled-extensions=" in contenido:
            contenido, ausentes, apagadas = _fusionar_habilitadas(
                contenido, instaladas, previas, perfil.get("reemplaza", {}))
            if ausentes:
                c.aviso(f"no se habilitan (no instaladas): {', '.join(ausentes)}")
                x.notas.append(
                    f"{len(ausentes)} extensiones del repo no estan instaladas y no se "
                    "han habilitado. Las que ya tenias activas siguen activas, asi que "
                    "no te quedas sin barra")
            if apagadas:
                c.info(f"se apagan por tener reemplazo instalado: {', '.join(apagadas)}")
            finales = set(re.findall(r"'([^']+)'", re.search(
                r"enabled-extensions=\[(.*?)\]", contenido).group(1)))
            heredadas = sorted((previas & instaladas & finales) - set(re.findall(
                r"'([^']+)'", re.search(r"enabled-extensions=\[(.*?)\]",
                                        fichero.read_text()).group(1))))
            if heredadas:
                c.info(f"se conservan activas las que ya tenias: {', '.join(heredadas)}")

        if fichero.stem.endswith("dash-to-panel"):
            contenido, nota = _remapear_monitor(contenido, monitor)
            if monitor and not nota:
                c.info(f"panel de dash-to-panel ajustado al monitor '{monitor}'")
            if nota:
                c.aviso(nota)
                x.notas.append(nota)
        c.accion("Aplicando", f"{etiqueta}  → {ruta}")
        x.correr(["dconf", "load", ruta], entrada=contenido)
    c.ok(f"{len(ramas)} ramas de dconf aplicadas")


def paso_retoques(x: Ctx) -> None:
    c = x.c
    perfiles = DATA / "burn-my-windows"
    if perfiles.is_dir() and any(perfiles.glob("*.conf")):
        destino = Path.home() / ".config/burn-my-windows/profiles"
        c.accion("Copiando", f"perfiles de burn-my-windows → {destino}")
        if not x.dry_run:
            destino.mkdir(parents=True, exist_ok=True)
        activo = None
        for f in sorted(perfiles.glob("*.conf")):
            if not x.dry_run:
                shutil.copy2(f, destino / f.name)
            activo = destino / f.name
        if activo:
            c.accion("Enlazando", f"active-profile → {activo}")
            x.correr(["dconf", "write",
                      "/org/gnome/shell/extensions/burn-my-windows/active-profile",
                      f"'{activo}'"])
            c.ok("perfil de efectos enlazado a la ruta de este usuario")
    else:
        c.saltado("no hay perfiles de burn-my-windows")

    faltan = []
    media = DATA / "dconf" / "11-media-keys.ini"
    if media.is_file():
        for orden in re.findall(r"^command='([^']+)'", media.read_text(), re.M):
            binario = orden.split()[0]
            if not shutil.which(binario):
                faltan.append(binario)
    if faltan:
        c.aviso(f"atajos que apuntan a programas no instalados: {', '.join(sorted(set(faltan)))}")
        x.notas.append("estos atajos no haran nada hasta que instales: "
                       + ", ".join(sorted(set(faltan))))
    else:
        c.ok("todos los programas de los atajos personalizados estan disponibles")

    # Apuntar en el manifiesto lo que este pase ha añadido, para --revert.
    if not x.dry_run and MANIFIESTO.is_file():
        try:
            datos = json.loads(MANIFIESTO.read_text())
            datos["extensiones_puestas"] = sorted(
                set(datos.get("extensiones_puestas", [])) | set(x.extensiones_puestas))
            MANIFIESTO.write_text(json.dumps(datos, indent=2, ensure_ascii=False) + "\n")
        except json.JSONDecodeError:
            c.aviso("no se pudo actualizar el manifiesto de respaldo")


PASOS = [
    ("comprobaciones", "Comprobaciones previas", paso_comprobaciones),
    ("respaldo",       "Respaldo del estado actual", paso_respaldo),
    ("base",           "Paquetes base", paso_apt_base),
    ("sublime",        "Sublime Text", paso_sublime),
    ("extensiones",    "Extensiones de GNOME Shell", paso_extensiones),
    ("ajustes",        "Ajustes de escritorio, atajos y extensiones", paso_dconf),
    ("retoques",       "Retoques dependientes de la maquina", paso_retoques),
]


# ---------------------------------------------------------------------------
# revertir
# ---------------------------------------------------------------------------

def revertir(c: Consola, asumir_si: bool, dry_run: bool) -> int:
    c.titulo("LGW · deshacer la instalacion")

    if not MANIFIESTO.is_file():
        c.aviso("todavia no se ha ejecutado el instalador en esta maquina")
        c.info(f"no hay ningun respaldo en {RESPALDO}")
        c.info("no hay nada que deshacer")
        return 1

    try:
        datos = json.loads(MANIFIESTO.read_text())
    except json.JSONDecodeError:
        c.error(f"el respaldo de {MANIFIESTO} esta corrupto; no me fio para restaurar")
        return 1

    ramas = datos.get("ramas", [])
    extras = datos.get("extensiones_puestas", [])

    c.info(f"respaldo del {datos.get('fecha', '?')} (GNOME {datos.get('gnome_shell', '?')})")
    c.bruto()
    c.entrada("Restaura", f"{len(ramas)} ramas de dconf al estado anterior")
    c.entrada("Desinstala", f"{len(extras)} extensiones que puso el instalador"
                            + (f" ({', '.join(extras)})" if extras else ""))
    c.entrada("NO toca", "los paquetes de apt: Sublime Text y las dependencias se quedan")

    if not asumir_si and not dry_run:
        if not c.preguntar("¿Volver al estado anterior a la instalacion?", por_defecto=False):
            c.saltado("cancelado: no se ha tocado nada")
            return 130

    fallos = 0

    c.paso("Restaurando dconf")
    for r in ramas:
        ruta, nombre = r["ruta"], r["fichero"]
        copia = RESPALDO / "dconf" / nombre
        c.accion("Restaurando", ruta)
        if dry_run:
            continue
        try:
            # Primero reset: si no, las claves que el instalador añadio y no
            # existian antes sobrevivirian al dconf load.
            subprocess.run(["dconf", "reset", "-f", ruta], check=True, capture_output=True)
            if copia.is_file() and copia.read_text().strip():
                subprocess.run(["dconf", "load", ruta], input=copia.read_text(),
                               text=True, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            c.error(f"{ruta}: {(e.stderr or b'').decode(errors='replace').strip() or e}")
            fallos += 1
    c.ok(f"{len(ramas) - fallos} de {len(ramas)} ramas restauradas")

    if extras:
        c.paso("Desinstalando extensiones")
        for uuid in extras:
            c.accion("Quitando", uuid)
            if dry_run:
                continue
            r = subprocess.run(["gnome-extensions", "uninstall", uuid], capture_output=True)
            if r.returncode != 0:
                carpeta = Path.home() / ".local/share/gnome-shell/extensions" / uuid
                if carpeta.is_dir():
                    shutil.rmtree(carpeta, ignore_errors=True)
        c.ok(f"{len(extras)} extensiones desinstaladas")

    c.paso("Listo")
    if dry_run:
        c.aviso("dry-run: no se ha deshecho nada")
        return 0
    if fallos:
        c.aviso(f"{fallos} ramas no se pudieron restaurar; revisa con -v")
    c.info("cierra sesion y vuelve a entrar para ver el escritorio anterior")
    c.info(f"el respaldo sigue en {RESPALDO} por si hace falta otra vez")
    return 1 if fallos else 0


def diagnosticar(c: Consola) -> int:
    """Comprueba pieza por pieza por que no se instalan las extensiones."""
    c.titulo("LGW · diagnostico de extensiones")

    nombre, perfil = detectar_distro()
    c.entrada("Distro", f"{nombre} → perfil '{perfil.get('nombre', '?')}'")
    try:
        ver = version_shell()
    except Fallo as e:
        c.error(str(e))
        return 1
    c.entrada("GNOME Shell", ver)
    c.entrada("Sesion", f"{os.environ.get('XDG_CURRENT_DESKTOP', '?')} / "
                        f"{os.environ.get('XDG_SESSION_TYPE', '?')}")
    c.entrada("Python", sys.version.split()[0])
    c.entrada("gnome-extensions", shutil.which("gnome-extensions") or "NO ENCONTRADO")
    c.entrada("glib-compile-schemas", shutil.which("glib-compile-schemas") or "NO ENCONTRADO")
    carpeta = Path.home() / ".local/share/gnome-shell/extensions"
    c.entrada("Carpeta destino", f"{carpeta} ({'existe' if carpeta.is_dir() else 'no existe'})")

    c.paso("Extensiones que ve GNOME ahora")
    for u in sorted(extensiones_instaladas()):
        c.info(u)

    c.paso("Prueba real con una sola extension")
    uuid = "caffeine@patapon.info"
    url = f"{EGO}/extension-info/?uuid={urllib.parse.quote(uuid)}&shell_version={ver}"
    c.accion("Consultando", url)
    try:
        info = json.loads(abrir(url, timeout=20).read())
        c.ok(f"la API responde: v{info.get('version')} · {info.get('download_url')}")
    except urllib.error.HTTPError as e:
        c.error(f"HTTP {e.code} — la API rechaza la peticion")
        c.info("si es 404, no hay build de esa extension para GNOME " + ver)
        return 1
    except urllib.error.URLError as e:
        c.error(f"sin conexion: {e.reason}")
        c.info("la VM no llega a extensions.gnome.org (DNS, proxy o sin red)")
        return 1
    except json.JSONDecodeError:
        c.error("la respuesta no es JSON (un portal cautivo o un proxy por medio)")
        return 1

    c.accion("Descargando", EGO + info["download_url"])
    try:
        datos = abrir(EGO + info["download_url"], timeout=120).read()
        c.ok(f"{len(datos) // 1024} KB descargados")
    except urllib.error.URLError as e:
        c.error(f"la descarga falla: {e.reason}")
        return 1

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
        f.write(datos)
        tmp = f.name
    try:
        try:
            with zipfile.ZipFile(tmp) as z:
                c.ok(f"el zip es valido ({len(z.namelist())} ficheros)")
        except zipfile.BadZipFile:
            c.error("lo descargado no es un zip valido")
            return 1

        c.accion("Instalando", "con gnome-extensions install --force")
        r = subprocess.run(["gnome-extensions", "install", "--force", tmp],
                           capture_output=True, text=True)
        if r.returncode == 0:
            c.ok("instalada correctamente")
        else:
            c.error(f"codigo {r.returncode}")
            for linea in (r.stderr or r.stdout or "").strip().splitlines():
                c.info(linea)
            c.info("el instalador probaria entonces a descomprimir a mano")
    finally:
        Path(tmp).unlink(missing_ok=True)

    c.paso("Resultado")
    en_disco = (Path.home() / ".local/share/gnome-shell/extensions" / uuid
                / "metadata.json").is_file()
    via_shell = uuid in subprocess.run(["gnome-extensions", "list"],
                                       capture_output=True, text=True).stdout.split()
    c.entrada("En disco", "sí" if en_disco else "NO")
    c.entrada("La ve el Shell", "sí" if via_shell else "no (normal hasta reiniciar sesion)")
    if en_disco:
        c.ok("la cadena completa funciona: la extension esta instalada")
        if not via_shell:
            c.info("el Shell no la vera hasta que cierres sesion; no es un error")
    else:
        c.error("nada en disco: aqui si hay un problema real de instalacion")
    return 0


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def construir_parser() -> argparse.ArgumentParser:
    nombres = ", ".join(n for n, _, _ in PASOS)
    return argparse.ArgumentParser(
        prog="LGW_installer.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Replica en esta maquina el escritorio GNOME de Linux_GNOME_Workspace:\n"
            "extensiones con sus ajustes individuales, atajos de teclado, tema y\n"
            "Sublime Text. El fondo de pantalla no se toca.\n"
            "\n"
            "Antes de tocar nada guarda un respaldo, asi que --revert siempre\n"
            "puede devolver la maquina al estado en que estaba."),
        epilog=(
            f"Pasos disponibles para --only y --skip:\n  {nombres}\n"
            "\n"
            "Ejemplos:\n"
            "  python3 LGW_installer.py\n"
            "      Pregunta si quieres instalarlo todo y lo hace.\n"
            "\n"
            "  python3 LGW_installer.py -y\n"
            "      Lo instala todo sin preguntar nada (util por SSH o en un script).\n"
            "\n"
            "  python3 LGW_installer.py -r\n"
            "      Deshace la instalacion y vuelve al estado anterior.\n"
            "\n"
            "  python3 LGW_installer.py -d\n"
            "      Diagnostica por que fallan las extensiones y para donde.\n"
            "\n"
            "  python3 LGW_installer.py -n\n"
            "      Enseña lo que haria sin tocar nada.\n"
            "\n"
            "  python3 LGW_installer.py --only extensiones,ajustes -v\n"
            "      Solo extensiones y configuracion, enseñando el detalle de cada\n"
            "      comando. Util para diagnosticar por que falla algo.\n"
            "\n"
            "Al terminar hay que cerrar sesion y volver a entrar para que GNOME\n"
            "cargue las extensiones nuevas."),
        **{"add_help": True},
    )


def main() -> int:
    p = construir_parser()
    p.add_argument("-y", "--yes", action="store_true",
                   help="no preguntar nada: instalar y configurar todo directamente")
    p.add_argument("-r", "--revert", action="store_true",
                   help="deshacer la instalacion y volver al estado anterior")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="simular sin modificar el sistema")
    p.add_argument("--only", metavar="PASOS", help="ejecutar solo estos pasos (comas)")
    p.add_argument("--skip", metavar="PASOS", help="ejecutar todo menos estos pasos (comas)")
    p.add_argument("--zip", action="store_true",
                   help="descargar las extensiones a mano en vez de pedirselo a "
                        "GNOME: sin dialogos, pero hay que cerrar sesion despues")
    p.add_argument("-d", "--diagnose", action="store_true",
                   help="comprobar paso a paso por que fallan las extensiones")
    p.add_argument("-l", "--list-steps", action="store_true", help="listar los pasos y salir")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="mostrar la salida de los comandos que se ejecutan")
    p.add_argument("--no-color", action="store_true",
                   help="salida sin color (tambien se respeta NO_COLOR)")
    args = p.parse_args()

    c = Consola(color=False if args.no_color else None, verboso=args.verbose)

    if args.list_steps:
        c.titulo("LGW · pasos del instalador")
        for nombre, desc, _ in PASOS:
            c.entrada(nombre, desc)
        return 0

    if args.diagnose:
        return diagnosticar(c)

    if args.revert:
        return revertir(c, args.yes, args.dry_run)

    c.titulo("LGW · instalador del escritorio GNOME")
    if args.dry_run:
        c.aviso("modo simulacion: no se va a modificar nada")

    validos = {n for n, _, _ in PASOS}
    pedidos = {s.strip() for s in args.only.split(",") if s.strip()} if args.only else None
    omitidos = {s.strip() for s in (args.skip or "").split(",") if s.strip()}
    for s in (pedidos or set()) | omitidos:
        if s not in validos:
            c.error(f"paso desconocido: '{s}' (validos: {', '.join(sorted(validos))})")
            return 2
    pasos = [q for q in PASOS
             if (pedidos is None or q[0] in pedidos) and q[0] not in omitidos]

    c.info("se van a ejecutar estas fases: " + ", ".join(n for n, _, _ in pasos))
    if not args.yes and not args.dry_run:
        if not c.preguntar("¿Instalar y configurar todo el escritorio?", por_defecto=True):
            c.saltado("cancelado por el usuario")
            return 130

    x = Ctx(c, args.dry_run, args.zip)
    c.plan(len(pasos))
    for nombre, desc, fn in pasos:
        c.paso(desc)
        try:
            fn(x)
            x.hechos += 1
        except Fallo as e:
            c.error(str(e))
            x.fallos += 1
            if nombre in ("comprobaciones", "respaldo"):
                c.error(f"'{nombre}' es imprescindible; no se continua")
                return 1
        except KeyboardInterrupt:
            c.bruto()
            c.error("interrumpido")
            return 130

    c.titulo("LGW · terminado")
    c.resumen(x.hechos, c.saltados, x.fallos)
    for n in x.notas:
        c.aviso(n)
    if not args.dry_run:
        c.info("cierra sesion y vuelve a entrar para que GNOME cargue las extensiones")
        c.info("si algo no te convence: python3 LGW_installer.py --revert")
    return 1 if x.fallos else 0


if __name__ == "__main__":
    sys.exit(main())
