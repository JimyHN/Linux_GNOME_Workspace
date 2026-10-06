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
# Segundos de espera a que respondas al dialogo de instalar una extension
# o cualquier otro paso interactivo antes de saltarlo.
TIMEOUT_DIALOGO = 180

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
    def __init__(self, consola: Consola, dry_run: bool, reconfigurar: bool = False):
        self.c = consola
        self.dry_run = dry_run
        self.reconfigurar = reconfigurar
        self.asumir_si = False
        self.auto_enter = False   # pulsar Ctrl+Enter solo en el dialogo
        self._ydotoold = None
        self.perfil: dict = {}
        self.so: str = ""
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
            # Pasa al ejecutar sin tty: por SSH sin -t, desde un hook o un
            # editor. El mensaje de sudo no deja claro que hacer.
            if "terminal is required" in motivo.lower() or "se requiere un terminal" in motivo.lower():
                raise Fallo("sudo no puede pedir la contraseña sin terminal; "
                            "lanza el instalador desde una terminal interactiva")
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

def cargar_perfiles() -> dict:
    try:
        datos = json.loads((DATA / "distros.json").read_text())
    except (OSError, json.JSONDecodeError):
        raise Fallo("no se pudo leer data/distros.json")
    return {k: v for k, v in datos.items() if not k.startswith("_")}


def detectar_distro(os_release: Path = Path("/etc/os-release")) -> tuple[str, str | None]:
    """Devuelve (nombre de /etc/os-release, clave de perfil o None).

    Kali declara ID=kali e ID_LIKE=debian; se prueba el ID y luego cada
    ID_LIKE contra la lista 'ids' de cada perfil."""
    campos: dict[str, str] = {}
    try:
        for linea in os_release.read_text().splitlines():
            if "=" in linea:
                k, v = linea.split("=", 1)
                campos[k.strip()] = v.strip().strip('"')
    except OSError:
        pass

    perfiles = cargar_perfiles()
    bonito = campos.get("PRETTY_NAME", campos.get("ID", "desconocido"))
    for candidato in [campos.get("ID", "")] + campos.get("ID_LIKE", "").split():
        for clave, perfil in perfiles.items():
            if candidato and candidato in perfil.get("ids", [clave]):
                return bonito, clave
    return bonito, None


def elegir_so(c: Consola, pedido: str | None, asumir_si: bool) -> tuple[str, dict]:
    """Decide el perfil: por --so, preguntando, o autodetectando con -y."""
    perfiles = cargar_perfiles()
    orden = ["kali", "ubuntu"]
    orden += [k for k in perfiles if k not in orden]
    bonito, detectado = detectar_distro()

    if pedido:
        clave = pedido.strip().lower()
        if clave not in perfiles:
            raise Fallo(f"sistema '{pedido}' no reconocido "
                        f"(validos: {', '.join(perfiles[k]['nombre'] for k in orden)})")
        c.ok(f"sistema indicado con --so: {perfiles[clave]['nombre']}")
        return clave, perfiles[clave]

    if asumir_si:
        if not detectado:
            raise Fallo(f"no se reconoce '{bonito}'; indicalo con --so Kali o --so Ubuntu")
        c.ok(f"{bonito} → {perfiles[detectado]['nombre']} (autodetectado)")
        return detectado, perfiles[detectado]

    c.bruto()
    c.info(f"/etc/os-release dice: {bonito}")
    c.opcion("0", "Salir")
    for i, clave in enumerate(orden, start=1):
        c.opcion(str(i), perfiles[clave]["nombre"],
                 "detectado" if clave == detectado else "")

    por_defecto = str(orden.index(detectado) + 1) if detectado in orden else ""
    while True:
        elegido = c.pedir("¿Que sistema es este?", por_defecto)
        if elegido == "0":
            raise SystemExit(130)
        if elegido.isdigit() and 1 <= int(elegido) <= len(orden):
            clave = orden[int(elegido) - 1]
            return clave, perfiles[clave]
        c.error(f"elige un numero entre 0 y {len(orden)}")


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

    c.ok(f"sistema: {x.perfil.get('nombre', '?')}")

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
    perfil = x.perfil
    c.info(f"paquetes para {perfil.get('nombre', '?')}")

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


def extensiones_en_shell() -> set[str]:
    """Lo que el Shell tiene cargado AHORA (via D-Bus), no lo que hay en disco."""
    return set(subprocess.run(["gnome-extensions", "list"],
                              capture_output=True, text=True).stdout.split())


YDOTOOL_SOCKET = "/run/.ydotool_socket"
# Codigos de tecla del kernel (linux/input-event-codes.h): Ctrl izq y Enter.
_KEY_CTRL, _KEY_ENTER = 29, 28


def preparar_ydotool(x: Ctx) -> bool:
    """Deja ydotool listo para confirmar el dialogo con Ctrl+Enter.

    En Wayland no se pueden enviar teclas por el protocolo de Mutter, pero
    ydotool inyecta por /dev/uinput (nivel kernel), que si funciona. Hace
    falta el demonio ydotoold como root; su socket queda en modo 0600 root,
    asi que ydotool tambien se llama con sudo."""
    c = x.c
    if x.dry_run:
        return False
    if not shutil.which("ydotool"):
        c.accion("Instalando", "ydotool (para confirmar los dialogos solo)")
        try:
            x.correr(["apt-get", "install", "-y", "ydotool"], root=True)
        except Fallo as e:
            c.aviso(f"no se pudo instalar ydotool ({e}); tendras que darle a Instalar a mano")
            return False
    if not shutil.which("ydotool"):
        return False
    if not Path(YDOTOOL_SOCKET).exists():
        try:
            x._ydotoold = subprocess.Popen(
                ["sudo", "ydotoold"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            c.aviso(f"no se pudo arrancar ydotoold ({e}); dalo a mano")
            return False
        for _ in range(24):
            if Path(YDOTOOL_SOCKET).exists():
                break
            time.sleep(0.25)
    if not Path(YDOTOOL_SOCKET).exists():
        c.aviso("ydotoold no dejo su socket; tendras que darle a Instalar a mano")
        return False
    c.ok("ydotool listo: los dialogos se confirman solos con Ctrl+Enter")
    return True


def _enviar_ctrl_enter(x: Ctx) -> None:
    subprocess.run(
        ["sudo", "ydotool", "key",
         f"{_KEY_CTRL}:1", f"{_KEY_ENTER}:1", f"{_KEY_ENTER}:0", f"{_KEY_CTRL}:0"],
        capture_output=True)


def parar_ydotool(x: Ctx) -> None:
    if x._ydotoold is not None:
        subprocess.run(["sudo", "pkill", "-f", "ydotoold"], capture_output=True)
        x._ydotoold = None


def _instalar_via_shell(x: Ctx, uuid: str) -> str:
    """Pide al Shell que instale la extension, como el interruptor de la web.

    Abre el dialogo nativo de GNOME; al darle a Instalar, la extension se
    descarga, se instala Y se carga en vivo, sin cerrar sesion. Devuelve
    'ok', 'cancel', 'timeout' o un texto de error."""
    if x.dry_run:
        x.c.detalle(f"(dry-run) InstallRemoteExtension {uuid}")
        return "ok"
    # El dialogo bloquea la llamada hasta que se responde. Si vamos a pulsar
    # Ctrl+Enter nosotros, hay que lanzar la llamada en segundo plano, esperar
    # a que salga el dialogo (2 s) y mandar la tecla.
    proc = subprocess.Popen(
        ["gdbus", "call", "--session", "--dest", "org.gnome.Shell",
         "--object-path", "/org/gnome/Shell", "--method",
         "org.gnome.Shell.Extensions.InstallRemoteExtension", uuid],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if x.auto_enter:
        time.sleep(2)
        _enviar_ctrl_enter(x)
    try:
        out, err = proc.communicate(timeout=TIMEOUT_DIALOGO)
    except subprocess.TimeoutExpired:
        proc.kill()
        return "timeout"
    salida = (out + err).strip()
    x.c.detalle(salida)
    if "successful" in salida:
        return "ok"
    if "cancelled" in salida:
        return "cancel"
    return salida.splitlines()[-1] if salida else f"codigo {proc.returncode}"


def _escribir_txt_extensiones(x: Ctx, ver: str, registro: list[str]) -> None:
    """Deja en la raiz del repo un txt con una extension por fila.

    Doble funcion: registro de lo que se descargo y diagnostico. Junto a
    cada UUID se apunta si su carpeta quedo en disco con metadata.json,
    que es lo unico que GNOME necesita para cargarla al reiniciar sesion."""
    base = Path.home() / ".local/share/gnome-shell/extensions"
    salida = RAIZ / "extensiones_descargadas.txt"
    cabecera = [
        f"# Generado por LGW_installer.py · GNOME {ver}",
        "# estado      uuid                                          en-disco",
        "",
    ]
    cuerpo = []
    for linea in registro:
        uuid = linea.split()[1]
        meta = base / uuid / "metadata.json"
        marca = "si" if meta.is_file() else "NO"
        cuerpo.append(f"{linea.split()[0]:<11} {uuid:<46} disco={marca}")
    texto = "\n".join(cabecera + cuerpo) + "\n"
    if x.dry_run:
        x.c.detalle("(dry-run) no se escribe extensiones_descargadas.txt")
        return
    try:
        salida.write_text(texto)
        x.c.info(f"lista escrita en {salida.name}")
    except OSError as e:
        x.c.aviso(f"no se pudo escribir {salida.name}: {e}")


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
    via_vivo = shell_accesible()
    en_shell = extensiones_en_shell() if via_vivo else set()
    puestas, saltadas, con_error = [], [], []
    registro: list[str] = []

    if via_vivo:
        x.auto_enter = preparar_ydotool(x)
        if x.auto_enter:
            c.info("instalacion en vivo: se confirma sola cada dialogo con Ctrl+Enter "
                   "(2 s para que salga, 3 s entre una y otra)")
        else:
            c.info("instalacion en vivo: dale a Instalar en el dialogo de cada una "
                   f"(se salta sola a los {TIMEOUT_DIALOGO}s sin respuesta)")
    else:
        c.aviso("no hay GNOME Shell en el bus de sesion; se bajan a disco y haran "
                "falta cerrar sesion para que carguen")

    for e in lista:
        uuid, nombre = e["uuid"], e["nombre"]

        if via_vivo:
            if uuid in en_shell:
                c.saltado(f"{nombre} ya instalada y activa")
                registro.append(f"ya-estaba   {uuid}")
                continue
            c.accion("Instalando", f"{nombre}  (dale a Instalar)")
            res = _instalar_via_shell(x, uuid)
            if res == "ok":
                c.ok(f"{nombre} instalada y activa")
                puestas.append(uuid)
                registro.append(f"instalada   {uuid}  (en vivo)")
            elif res == "cancel":
                c.aviso(f"{nombre}: cancelada en el dialogo")
                saltadas.append(nombre)
                registro.append(f"cancelada   {uuid}")
            elif res == "timeout":
                c.aviso(f"{nombre}: sin respuesta en {TIMEOUT_DIALOGO}s, se salta")
                saltadas.append(nombre)
                registro.append(f"timeout     {uuid}")
            else:
                c.error(f"{nombre}: {res}")
                con_error.append(nombre)
                registro.append(f"ERROR       {uuid}  ({res})")
            if x.auto_enter:
                time.sleep(3)
            continue

        # Sin Shell en el bus: descarga directa a disco (requiere cerrar sesion).
        instaladas = extensiones_instaladas()
        if uuid in instaladas:
            c.saltado(f"{nombre} ya en disco")
            registro.append(f"ya-estaba   {uuid}")
            continue
        c.accion("Descargando", nombre)
        url = f"{EGO}/extension-info/?uuid={urllib.parse.quote(uuid)}&shell_version={ver}"
        try:
            info = json.loads(abrir(url).read())
            descarga = info.get("download_url")
            if not descarga:
                raise Fallo(f"no hay build para GNOME {ver}")
            datos = abrir(EGO + descarga, timeout=120).read()
            _instalar_zip(x, uuid, datos)
            puestas.append(uuid)
            registro.append(f"descargada  {uuid}  v{info.get('version', '?')}")
        except (urllib.error.URLError, Fallo, zipfile.BadZipFile, OSError) as err:
            c.error(f"{nombre}: {err}")
            con_error.append(nombre)
            registro.append(f"ERROR       {uuid}  ({err})")

    parar_ydotool(x)
    x.extensiones_puestas = puestas
    _escribir_txt_extensiones(x, ver, registro)

    if puestas:
        c.ok(f"{len(puestas)} extensiones instaladas")
    if saltadas:
        x.notas.append(f"extensiones sin instalar (canceladas o sin respuesta): "
                       f"{', '.join(saltadas)}. Vuelve a lanzar el instalador para esas")
    if con_error:
        x.fallos += len(con_error)
        x.notas.append(f"extensiones con error: {', '.join(con_error)}")
    if not via_vivo and puestas:
        c.info("cierra sesion y vuelve a entrar para que GNOME las cargue")


AMO = "https://addons.mozilla.org/firefox/downloads/latest"

# Donde cada empaquetado de Firefox guarda los perfiles y lee las politicas.
FIREFOX_PERFILES = [
    "~/snap/firefox/common/.mozilla/firefox",          # snap de Ubuntu
    "~/.mozilla/firefox",                               # deb, firefox-esr de Kali
    "~/.var/app/org.mozilla.firefox/.mozilla/firefox",  # flatpak
]
FIREFOX_POLITICAS = ["/etc/firefox/policies", "/etc/firefox-esr/policies"]


def perfiles_firefox() -> list[Path]:
    """Perfiles reales, leidos de profiles.ini para no colar carpetas sueltas."""
    fuera = []
    for base in FIREFOX_PERFILES:
        raiz = Path(base).expanduser()
        ini = raiz / "profiles.ini"
        if not ini.is_file():
            continue
        for linea in ini.read_text(errors="replace").splitlines():
            if linea.startswith("Path="):
                ruta = linea.split("=", 1)[1].strip()
                perfil = Path(ruta) if ruta.startswith("/") else raiz / ruta
                if perfil.is_dir():
                    fuera.append(perfil)
    return fuera


def _crear_contenedores(x: Ctx, perfil: Path, contenedores: list[dict]) -> None:
    """Crea contenedores de Multi-Account Containers en containers.json.

    Es el fichero de la API de identidades de Firefox; escribirlo es la via
    sin GUI. Lo que NO se puede pre-cargar es la config de Container-proxy o
    FoxyProxy (vive en el IndexedDB de cada extension). Idempotente por
    nombre. Firefox tiene que estar cerrado o reescribe el fichero al salir."""
    c = x.c
    if not contenedores:
        return
    f = perfil / "containers.json"
    try:
        datos = json.loads(f.read_text()) if f.is_file() else {}
    except (OSError, json.JSONDecodeError):
        datos = {}
    datos.setdefault("version", 6)
    ids = datos.setdefault("identities", [])

    # Los contenedores internos de Firefox (thumbnail...) usan ids reservados
    # enormes (~2^32). Los publicos van 1, 2, 3... Hay que ignorar los grandes
    # o el siguiente id se dispararia.
    def _sano(n: object) -> bool:
        return isinstance(n, int) and 0 < n < (1 << 20)

    existentes = {i.get("name") for i in ids if i.get("public")}
    publicos = [i["userContextId"] for i in ids
                if i.get("public") and _sano(i.get("userContextId"))]
    ultimo = datos.get("lastUserContextId", 0)
    siguiente = max(publicos + ([ultimo] if _sano(ultimo) else []) + [0])
    nuevos = []
    for cont in contenedores:
        if cont["name"] in existentes:
            c.saltado(f"contenedor {cont['name']} ya existe")
            continue
        siguiente += 1
        ids.append({
            "userContextId": siguiente,
            "public": True,
            "icon": cont["icon"],
            "color": cont["color"],
            "name": cont["name"],
        })
        nuevos.append(cont["name"])
    datos["lastUserContextId"] = siguiente

    if nuevos:
        c.accion("Creando", f"contenedores: {', '.join(nuevos)}")
        if not x.dry_run:
            f.write_text(json.dumps(datos, indent=2, ensure_ascii=False) + "\n")
        c.ok(f"{len(nuevos)} contenedores creados")


def _firefox_corriendo() -> bool:
    for n in ("firefox", "firefox-esr", "firefox-bin"):
        if subprocess.run(["pgrep", "-x", n], capture_output=True).returncode == 0:
            return True
    return False


def paso_firefox(x: Ctx) -> None:
    c = x.c
    try:
        cfg = json.loads((DATA / "firefox.json").read_text())
    except (OSError, json.JSONDecodeError):
        raise Fallo("no se pudo leer data/firefox.json")

    perfiles = perfiles_firefox()
    if not perfiles:
        c.aviso("no hay ningun perfil de Firefox; abre Firefox una vez y repite")
        x.notas.append("Firefox no tenia perfil: abrelo una vez y vuelve a lanzar "
                       "el instalador para las pestañas y las extensiones")
        return

    prefs = cfg.get("prefs", {})
    exts = cfg.get("extensiones", [])
    puestas = yaestaban = 0

    # autoDisableScopes se lee al arrancar Firefox; un Firefox abierto reescribe
    # prefs.js al cerrarse. Mejor cerrarlo para que las extensiones se activen.
    if _firefox_corriendo():
        c.aviso("Firefox esta abierto: cierralo y vuelve a abrirlo para que las "
                "extensiones se activen")

    for perfil in perfiles:
        c.accion("Perfil", perfil.name)

        # --- pestañas a la izquierda ---
        destino = perfil / "user.js"
        lineas = "".join(f'user_pref("{k}", {json.dumps(v)});\n' for k, v in prefs.items())
        cabecera = ("// Generado por LGW_installer.py\n"
                    "// Activa las extensiones dejadas como .xpi en el perfil.\n")
        previo = destino.read_text(errors="replace") if destino.is_file() else ""
        if previo.startswith(cabecera) and lineas in previo:
            c.info("ajustes de extensiones ya puestos")
        else:
            c.accion("Configurando", "ajustes para activar las extensiones")
            if not x.dry_run:
                # Conservar lo que el usuario tuviera puesto a mano.
                conservado = "\n".join(
                    l for l in previo.splitlines()
                    if not any(f'"{k}"' in l for k in prefs) and not l.startswith("//"))
                destino.write_text(cabecera + lineas
                                   + (conservado.strip() + "\n" if conservado.strip() else ""))

        # --- extensiones: el xpi va en <perfil>/extensions/<id>.xpi ---
        carpeta = perfil / "extensions"
        if not x.dry_run:
            carpeta.mkdir(parents=True, exist_ok=True)
        for e in exts:
            xpi = carpeta / f"{e['id']}.xpi"
            if xpi.is_file() and xpi.stat().st_size > 0:
                c.saltado(f"{e['nombre']} ya instalada")
                yaestaban += 1
                continue
            c.accion("Instalando", e["nombre"])
            if x.dry_run:
                puestas += 1
                continue
            try:
                datos = abrir(f"{AMO}/{e['slug']}/latest.xpi", timeout=180).read()
            except urllib.error.URLError as err:
                c.error(f"{e['nombre']}: {err.reason}")
                x.fallos += 1
                continue
            if not datos[:2] == b"PK":
                c.error(f"{e['nombre']}: lo descargado no es un xpi")
                x.fallos += 1
                continue
            xpi.write_bytes(datos)
            c.ok(f"{e['nombre']} ({len(datos) // 1024} KB)")
            puestas += 1

        _crear_contenedores(x, perfil, cfg.get("contenedores", []))

    if puestas:
        c.ok(f"{puestas} extensiones de Firefox puestas")
    if yaestaban:
        c.info(f"{yaestaban} ya estaban")
    c.info("Firefox las activa al proximo arranque; reinicialo si esta abierto")


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


def _resolver_favoritos(contenido: str) -> tuple[str, list[tuple[str, str | None]]]:
    """Construye favorite-apps en el orden de data/favoritos.json.

    Cada entrada es un papel (Terminal, Archivos...) con varios candidatos,
    y se ancla el primer .desktop que exista. El terminal y Firefox no se
    llaman igual en Ubuntu y en Kali, asi que fijar nombres concretos dejaba
    huecos muertos en el dash."""
    try:
        orden = json.loads((DATA / "favoritos.json").read_text())["orden"]
    except (OSError, json.JSONDecodeError, KeyError):
        return contenido, []

    elegidos, resueltos = [], []
    for entrada in orden:
        papel = entrada.get("papel", "?")
        hallado = next((d for d in entrada.get("candidatos", []) if desktop_existe(d)), None)
        resueltos.append((papel, hallado))
        if hallado:
            elegidos.append(hallado)

    nueva = "favorite-apps=[" + ", ".join(f"'{a}'" for a in elegidos) + "]"
    if "favorite-apps=" in contenido:
        contenido = re.sub(r"favorite-apps=\[.*?\]", nueva, contenido)
    else:
        contenido = contenido.replace("[/]\n", f"[/]\n{nueva}\n", 1)
    return contenido, resueltos


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


def _claves_del_ini(ruta: str, contenido: str) -> dict[str, str]:
    """Aplana un .ini de dconf a {ruta_completa: valor}."""
    claves: dict[str, str] = {}
    seccion = ""
    for linea in contenido.splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        if linea.startswith("[") and linea.endswith("]"):
            dentro = linea[1:-1]
            seccion = "" if dentro == "/" else dentro.strip("/") + "/"
            continue
        if "=" not in linea:
            continue
        k, v = linea.split("=", 1)
        claves[f"{ruta}{seccion}{k.strip()}"] = v.strip()
    return claves


def _rama_ya_igual(ruta: str, contenido: str) -> tuple[bool, list[str]]:
    """Compara lo que escribiriamos con lo que hay puesto ahora.

    Sirve para no pisar en silencio los ajustes que el usuario haya tocado
    en la maquina destino: si difieren, se pregunta."""
    distintas = []
    for clave, valor in _claves_del_ini(ruta, contenido).items():
        actual = subprocess.run(["dconf", "read", clave],
                                capture_output=True, text=True).stdout.strip()
        if actual != valor:
            distintas.append(f"{clave.rsplit('/', 1)[-1]}: {actual or '(sin valor)'} → {valor}")
    return not distintas, distintas


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
    perfil = x.perfil
    mapa = perfil.get("uuid_sistema", {})
    aplicadas = 0

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

        if ruta == "/org/gnome/shell/":
            contenido, resueltos = _resolver_favoritos(contenido)
            faltan = [p for p, d in resueltos if not d]
            for papel, desktop in resueltos:
                if desktop:
                    c.info(f"barra: {papel} → {desktop}")
            if faltan:
                c.aviso(f"sin icono en la barra (no instalado): {', '.join(faltan)}")
                x.notas.append(
                    f"no se anclaron {', '.join(faltan)} porque no estan instalados; "
                    "instalalos y vuelve a lanzar el instalador")

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

        if ruta == "/org/gnome/shell/extensions/dash-to-panel/":
            contenido, nota = _remapear_monitor(contenido, monitor)
            if monitor and not nota:
                c.info(f"panel de dash-to-panel ajustado al monitor '{monitor}'")
            if nota:
                c.aviso(nota)
                x.notas.append(nota)
        # Si la rama ya esta igual no se toca; si difiere se pregunta, salvo
        # con -c o -y. Asi una extension ya configurada a mano no se pisa sin
        # avisar.
        igual, distintas = _rama_ya_igual(ruta, contenido)
        if igual:
            c.saltado(f"{etiqueta} ya tiene esta configuracion")
            continue
        if not x.dry_run and not x.reconfigurar:
            c.aviso(f"{etiqueta} ya existe con otra configuracion "
                    f"({len(distintas)} clave{'s' if len(distintas) != 1 else ''} distinta"
                    f"{'s' if len(distintas) != 1 else ''})")
            for d in distintas[:6]:
                c.info(d)
            if len(distintas) > 6:
                c.info(f"... y {len(distintas) - 6} mas")
            if not c.preguntar(f"¿Ajustar la configuracion de {etiqueta}?", por_defecto=True):
                c.saltado(f"{etiqueta} se deja como estaba")
                continue

        c.accion("Aplicando", f"{etiqueta}  → {ruta}")
        x.correr(["dconf", "load", ruta], entrada=contenido)
        aplicadas += 1
    c.ok(f"{aplicadas} de {len(ramas)} ramas de dconf aplicadas")


def paso_retoques(x: Ctx) -> None:
    c = x.c
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


def paso_sistema(x: Ctx) -> None:
    """Ultimo paso: actualizar el sistema o instalar las herramientas de Kali.

    Son operaciones enormes (GB), asi que con -y NO se lanzan solas: se
    preguntan siempre. Es una bifurcacion, no un si-a-todo."""
    c = x.c
    if x.dry_run:
        c.saltado("simulacion: no se pregunta por actualizaciones")
        return
    if x.asumir_si:
        c.saltado("con -y no se tocan las actualizaciones (son enormes); "
                  "lanza sin -y para el update del sistema o kali-linux-large")
        return

    if c.preguntar("¿Quieres actualizar los sistemas?", por_defecto=False):
        c.accion("Actualizando", "indices de apt")
        x.correr(["apt-get", "update", "-y"], root=True, tolerante=True)
        c.accion("Actualizando", "full-upgrade (puede tardar un buen rato)")
        try:
            x.correr(["apt-get", "full-upgrade", "-y"], root=True)
            c.ok("sistema actualizado")
        except Fallo as e:
            c.error(f"la actualizacion fallo: {e}")
            x.fallos += 1
        return

    if c.preguntar("¿Quieres instalar las herramientas large de Kali?", por_defecto=False):
        c.accion("Instalando", "kali-linux-large (descarga muy grande)")
        try:
            x.correr(["apt-get", "install", "-y", "kali-linux-large"], root=True)
            c.ok("kali-linux-large instalado")
        except Fallo as e:
            c.error(f"no se pudo instalar kali-linux-large: {e}")
            x.fallos += 1
    else:
        c.saltado("no se actualiza nada mas")


PASOS = [
    ("comprobaciones", "Comprobaciones previas", paso_comprobaciones),
    ("respaldo",       "Respaldo del estado actual", paso_respaldo),
    ("base",           "Paquetes base", paso_apt_base),
    ("sublime",        "Sublime Text", paso_sublime),
    ("extensiones",    "Extensiones de GNOME Shell", paso_extensiones),
    ("ajustes",        "Ajustes de escritorio, atajos y extensiones", paso_dconf),
    ("firefox",        "Firefox: pestañas verticales y extensiones", paso_firefox),
    ("retoques",       "Retoques dependientes de la maquina", paso_retoques),
    ("sistema",        "Actualizacion del sistema", paso_sistema),
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


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def construir_parser() -> argparse.ArgumentParser:
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
            "Ejemplos:\n"
            "  python3 LGW_installer.py\n"
            "      Pregunta si quieres instalarlo todo y lo hace.\n"
            "\n"
            "  python3 LGW_installer.py -y\n"
            "      Lo instala todo sin preguntar nada: detecta el sistema solo.\n"
            "\n"
            "  python3 LGW_installer.py -so Kali\n"
            "      Da el sistema por hecho y no pregunta cual es.\n"
            "\n"
            "  python3 LGW_installer.py -r\n"
            "      Deshace la instalacion y vuelve al estado anterior.\n"
            "\n"

            "  python3 LGW_installer.py -n\n"
            "      Enseña lo que haria sin tocar nada.\n"
            "\n"
            "  python3 LGW_installer.py -c\n"
            "      Si algo ya esta con otra configuracion, la ajusta sin preguntar.\n"
            "\n"
            "Al terminar hay que cerrar sesion y volver a entrar para que GNOME\n"
            "cargue las extensiones nuevas."),
        add_help=False,
    )


OPCIONES_AYUDA = [
    ("-h, --help", "mostrar esta ayuda y salir"),
    ("-y, --yes", "no preguntar nada: instalar y configurar todo"),
    ("-r, --revert", "deshacer la instalacion y volver al estado anterior"),
    ("-n, --dry-run", "simular sin modificar el sistema"),
    ("-c, --configurar", "si algo ya esta con otra config, ajustarla sin preguntar"),
    ("-so SISTEMA", "indicar el sistema (Kali o Ubuntu) y no preguntarlo"),
    ("-v, --verbose", "mostrar la salida de los comandos"),
    ("--no-color", "salida sin color (tambien se respeta NO_COLOR)"),
]

EJEMPLOS_AYUDA = [
    ("python3 LGW_installer.py", "Pregunta que sistema es y lo instala todo."),
    ("python3 LGW_installer.py -y", "Instala todo sin preguntar; detecta el sistema solo."),
    ("python3 LGW_installer.py -so Kali", "Da por hecho el sistema y no lo pregunta."),
    ("python3 LGW_installer.py -r", "Deshace la instalacion y vuelve al estado anterior."),
    ("python3 LGW_installer.py -n", "Enseña lo que haria sin tocar nada."),
    ("python3 LGW_installer.py -c", "Si algo tiene otra config, la ajusta sin preguntar."),
]


def imprimir_ayuda(c: Consola) -> None:
    c.titulo("LGW · instalador del escritorio GNOME")
    c.parrafo(
        "Replica en esta maquina el escritorio GNOME de Linux_GNOME_Workspace:\n"
        "extensiones, atajos de teclado, tema, Firefox y Sublime Text.\n"
        "El fondo de pantalla no se toca. Antes de nada guarda un respaldo,\n"
        "asi que --revert siempre puede dejar la maquina como estaba.")
    c.paso("Uso")
    c.parrafo("  python3 LGW_installer.py [opciones]")
    c.paso("Opciones")
    for flags, desc in OPCIONES_AYUDA:
        c.opcion_ayuda(flags, desc)
    c.paso("Ejemplos")
    for cmd, desc in EJEMPLOS_AYUDA:
        c.ejemplo(cmd, desc)
    c.bruto()
    c.info("al terminar, cierra sesion y vuelve a entrar para que GNOME cargue todo")


def main() -> int:
    p = construir_parser()
    p.add_argument("-h", "--help", action="store_true", dest="ayuda",
                   help="mostrar esta ayuda y salir")
    p.add_argument("-y", "--yes", action="store_true",
                   help="no preguntar nada: instalar y configurar todo directamente")
    p.add_argument("-r", "--revert", action="store_true",
                   help="deshacer la instalacion y volver al estado anterior")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="simular sin modificar el sistema")
    p.add_argument("-c", "--configurar", action="store_true", dest="reconfigurar",
                   help="si algo ya esta instalado con otra configuracion, ajustarla "
                        "sin preguntar")
    p.add_argument("-so", "--so", metavar="SISTEMA", dest="so",
                   help="indicar el sistema (Kali o Ubuntu) y no preguntarlo")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="mostrar la salida de los comandos que se ejecutan")
    p.add_argument("--no-color", action="store_true",
                   help="salida sin color (tambien se respeta NO_COLOR)")
    args = p.parse_args()

    c = Consola(color=False if args.no_color else None, verboso=args.verbose)

    if args.ayuda:
        imprimir_ayuda(c)
        return 0

    if args.revert:
        return revertir(c, args.yes, args.dry_run)

    c.titulo("LGW · instalador del escritorio GNOME")
    if args.dry_run:
        c.aviso("modo simulacion: no se va a modificar nada")

    try:
        so, perfil = elegir_so(c, args.so, args.yes or args.dry_run)
    except Fallo as e:
        c.error(str(e))
        return 2
    except SystemExit:
        c.saltado("cancelado")
        return 130

    c.info("fases: " + ", ".join(n for n, _, _ in PASOS))
    if not args.yes and not args.dry_run:
        if not c.preguntar("¿Instalar y configurar todo el escritorio?", por_defecto=True):
            c.saltado("cancelado por el usuario")
            return 130

    x = Ctx(c, args.dry_run, args.reconfigurar or args.yes)
    x.asumir_si = args.yes
    x.so, x.perfil = so, perfil
    c.plan(len(PASOS))
    for nombre, desc, fn in PASOS:
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
