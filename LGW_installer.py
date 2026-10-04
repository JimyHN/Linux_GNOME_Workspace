#!/usr/bin/env python3
"""Replica en esta maquina el escritorio GNOME definido en data/.

Pensado para una VM recien instalada: clonas el repo, lo ejecutas y te deja
las extensiones, sus ajustes individuales, los atajos de teclado y Sublime
Text tal y como estan en el ordenador de referencia.

    git clone https://github.com/JimyHN/Linux_GNOME_Workspace.git
    cd Linux_GNOME_Workspace
    python3 LGW_installer.py
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lgw.ui import Consola  # noqa: E402

RAIZ = Path(__file__).resolve().parent
DATA = RAIZ / "data"
EGO = "https://extensions.gnome.org"

# Paquetes que hacen falta para el resto del proceso.
APT_BASE = ["curl", "unzip", "dconf-cli", "gnome-shell-extension-prefs"]
# Extensiones que en Ubuntu vienen empaquetadas (ding, tiling-assistant,
# appindicators...). En otras distros no existe: si falla, se avisa y sigue.
APT_EXTENSIONES_SISTEMA = ["gnome-shell-ubuntu-extensions"]

# Claves de dash-to-panel indexadas por monitor: su contenido es un JSON
# cuya clave es el identificador del monitor del equipo de origen y no
# coincidira con el de la VM.
# Marcador que LGW_export.py deja en lugar del home del equipo de origen.
MARCADOR_HOME = "@LGW_HOME@"

D2P_CLAVES_POR_MONITOR = [
    "panel-anchors", "panel-element-positions", "panel-lengths",
    "panel-positions", "panel-sizes",
]


class Fallo(Exception):
    """Error que aborta un paso pero no el instalador entero."""


# ---------------------------------------------------------------------------
# utilidades
# ---------------------------------------------------------------------------

class Ctx:
    def __init__(self, consola: Consola, dry_run: bool, asumir_si: bool):
        self.c = consola
        self.dry_run = dry_run
        self.asumir_si = asumir_si
        self.hechos = 0
        self.saltados = 0
        self.fallos = 0
        self.notas: list[str] = []

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
            if not tolerante:
                raise Fallo((r.stderr or r.stdout or "").strip().splitlines()[-1]
                            if (r.stderr or r.stdout).strip() else f"codigo {r.returncode}")
        return r.stdout

    _sudo_ok: bool | None = None

    def _sudo_sin_clave(self) -> bool:
        if Ctx._sudo_ok is None:
            Ctx._sudo_ok = subprocess.run(
                ["sudo", "-n", "true"], capture_output=True).returncode == 0
        return Ctx._sudo_ok


def version_shell() -> str:
    """Version mayor de GNOME Shell de ESTA maquina.

    Se pide a extensions.gnome.org la build correspondiente a la VM, no la
    version que tenia el equipo de origen: asi el repo no se queda obsoleto
    cada vez que GNOME sube de version."""
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
    """Devuelve (ruta_dconf, contenido). La ruta va en la cabecera del .ini."""
    texto = ruta.read_text()
    m = re.search(r"^#\s*dconf-path:\s*(\S+)", texto, re.M)
    if not m:
        raise Fallo(f"{ruta.name} no declara '# dconf-path:'")
    return m.group(1), texto


def monitor_actual() -> str | None:
    """Identificador de monitor al estilo dash-to-panel: VENDOR-SERIAL."""
    try:
        salida = subprocess.run(["gdctl", "show"], capture_output=True,
                                text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    vendor = re.search(r"Vendor:\s*(\S+)", salida)
    serial = re.search(r"Serial:\s*(\S+)", salida)
    if vendor and serial and vendor.group(1).lower() != "unknown":
        return f"{vendor.group(1)}-{serial.group(1)}"
    return None


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
        c.aviso(f"la sesion actual es '{sesion or 'desconocida'}', no GNOME; "
                "los ajustes se escribiran igual pero no se veran hasta entrar en GNOME")
    else:
        c.ok(f"sesion GNOME detectada ({os.environ.get('XDG_SESSION_TYPE', '?')})")

    ver = version_shell()
    c.ok(f"GNOME Shell {ver}")
    origen = {}
    manifiesto = DATA / "extensions.json"
    if manifiesto.is_file():
        origen = json.loads(manifiesto.read_text()).get("capturado_en", {})
    if origen.get("gnome_shell", "").split(".")[0] not in ("", ver):
        c.aviso(f"el repo se capturo en GNOME {origen['gnome_shell']} y aqui hay {ver}; "
                "se pediran las versiones de extension correspondientes a esta")
    if not shutil.which("sudo"):
        raise Fallo("hace falta sudo para instalar paquetes")
    if not x._sudo_sin_clave():
        c.info("sudo pedira la contraseña durante la instalacion de paquetes")


def paso_apt_base(x: Ctx) -> None:
    c = x.c
    c.accion("Refrescando", "indices de apt")
    x.correr(["apt-get", "update", "-qq"], root=True, tolerante=True)
    faltan = [p for p in APT_BASE
              if subprocess.run(["dpkg", "-s", p], capture_output=True).returncode != 0]
    if not faltan:
        c.saltado("las dependencias base ya estan instaladas")
    else:
        c.accion("Instalando", ", ".join(faltan))
        x.correr(["apt-get", "install", "-y", *faltan], root=True)
        c.ok(f"{len(faltan)} paquete{'s' if len(faltan) != 1 else ''} base instalado{'s' if len(faltan) != 1 else ''}")

    for p in APT_EXTENSIONES_SISTEMA:
        c.accion("Instalando", f"{p} (extensiones de la distro)")
        try:
            x.correr(["apt-get", "install", "-y", p], root=True)
            c.ok(f"{p} disponible")
        except Fallo:
            c.aviso(f"{p} no existe en esta distro; ding/tiling-assistant/"
                    "appindicators podrian no aparecer")
            x.notas.append(f"{p} no se pudo instalar: revisa si tu distro empaqueta "
                           "esas extensiones con otro nombre")


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
            with urllib.request.urlopen(
                    "https://download.sublimetext.com/sublimehq-pub.gpg", timeout=30) as r:
                clave = r.read()
        except urllib.error.URLError as e:
            raise Fallo(f"no se pudo descargar la clave: {e}")
        armadura = subprocess.run(["gpg", "--dearmor"], input=clave,
                                  capture_output=True)
        if armadura.returncode != 0:
            raise Fallo("gpg --dearmor fallo al procesar la clave")
        tmp = Path(tempfile.mkstemp(suffix=".gpg")[1])
        tmp.write_bytes(armadura.stdout)
        x.correr(["install", "-D", "-m", "0644", str(tmp), str(llavero)], root=True)
        tmp.unlink(missing_ok=True)
    c.ok(f"clave en {llavero}")

    c.accion("Añadiendo", "repositorio apt/stable de Sublime Text")
    repo = (f"deb [signed-by={llavero}] https://download.sublimetext.com/ apt/stable/\n")
    x.correr(["tee", str(lista)], root=True, entrada=repo)

    c.accion("Instalando", "sublime-text")
    x.correr(["apt-get", "update", "-qq"], root=True, tolerante=True)
    x.correr(["apt-get", "install", "-y", "sublime-text"], root=True)
    c.ok("Sublime Text instalado (ejecutable: subl)")


def paso_extensiones(x: Ctx) -> None:
    c = x.c
    manifiesto = DATA / "extensions.json"
    if not manifiesto.is_file():
        raise Fallo("falta data/extensions.json; ejecuta LGW_export.py en el equipo de origen")
    lista = json.loads(manifiesto.read_text()).get("extensiones_usuario", [])
    if not lista:
        c.saltado("no hay extensiones de usuario que instalar")
        return

    ver = version_shell()
    destino = Path.home() / ".local/share/gnome-shell/extensions"
    instaladas = set(subprocess.run(["gnome-extensions", "list"],
                                    capture_output=True, text=True).stdout.split())
    ok = 0
    for e in lista:
        uuid, nombre = e["uuid"], e["nombre"]
        if uuid in instaladas:
            c.saltado(f"{nombre} ya instalada")
            continue
        c.accion("Instalando", f"{nombre} ({uuid})")
        try:
            url = f"{EGO}/extension-info/?uuid={urllib.parse.quote(uuid)}&shell_version={ver}"
            with urllib.request.urlopen(url, timeout=30) as r:
                info = json.loads(r.read())
        except urllib.error.HTTPError as err:
            if err.code == 404:
                c.aviso(f"{nombre}: no hay version compatible con GNOME {ver}")
                x.notas.append(f"{nombre} no tiene build para GNOME {ver}; instalala a mano")
                continue
            c.error(f"{nombre}: {err}")
            x.fallos += 1
            continue
        except (urllib.error.URLError, json.JSONDecodeError) as err:
            c.error(f"{nombre}: {err}")
            x.fallos += 1
            continue

        c.info(f"v{info['version']} desde extensions.gnome.org")
        if x.dry_run:
            ok += 1
            continue
        try:
            with urllib.request.urlopen(EGO + info["download_url"], timeout=120) as r:
                datos = r.read()
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
                f.write(datos)
                zip_tmp = f.name
            x.correr(["gnome-extensions", "install", "--force", zip_tmp])
            Path(zip_tmp).unlink(missing_ok=True)
        except (urllib.error.URLError, Fallo) as err:
            c.error(f"{nombre}: {err}")
            x.fallos += 1
            continue
        ok += 1

    if ok:
        c.ok(f"{ok} extensiones instaladas en {destino}")
    c.info("se activaran al aplicar los ajustes; GNOME las carga tras reiniciar la sesion")


def paso_fondos(x: Ctx) -> None:
    """Deja los fondos en ~/.local/share/backgrounds antes de aplicar dconf.

    Las claves picture-uri apuntan ahi por ruta relativa al home, asi que el
    fichero tiene que existir antes de que se escriban o el escritorio se
    queda en negro hasta el siguiente arranque."""
    c = x.c
    origen = DATA / "backgrounds"
    imagenes = sorted(origen.glob("*")) if origen.is_dir() else []
    if not imagenes:
        c.saltado("el repo no trae fondos de pantalla")
        return
    destino = Path.home() / ".local/share/backgrounds"
    if not x.dry_run:
        destino.mkdir(parents=True, exist_ok=True)
    for img in imagenes:
        c.accion("Copiando", f"{img.name} → {destino}")
        if not x.dry_run:
            shutil.copy2(img, destino / img.name)
    c.ok(f"{len(imagenes)} fondo{'s' if len(imagenes) != 1 else ''} en su sitio")


def paso_dconf(x: Ctx) -> None:
    c = x.c
    carpeta = DATA / "dconf"
    if not carpeta.is_dir():
        raise Fallo("falta data/dconf/; ejecuta LGW_export.py en el equipo de origen")
    archivos = sorted(carpeta.glob("*.ini"))
    if not archivos:
        raise Fallo("no hay ningun .ini en data/dconf/")

    monitor = monitor_actual()
    for f in archivos:
        ruta, contenido = leer_ini(f)
        contenido = contenido.replace(MARCADOR_HOME, str(Path.home()))
        etiqueta = f.stem.split("-", 1)[1] if "-" in f.stem else f.stem

        if f.stem.endswith("dash-to-panel"):
            contenido, nota = _remapear_monitor(contenido, monitor)
            if nota:
                c.aviso(nota)
                x.notas.append(nota)

        c.accion("Aplicando", f"{etiqueta}  → {ruta}")
        x.correr(["dconf", "load", ruta], entrada=contenido)
    c.ok(f"{len(archivos)} ramas de dconf aplicadas")


def _remapear_monitor(contenido: str, monitor: str | None) -> tuple[str, str | None]:
    """dash-to-panel guarda tamaño y posicion del panel por monitor.

    El identificador del equipo de origen no existe en la VM, asi que o lo
    sustituimos por el de aqui o quitamos esas claves para que la extension
    use sus valores por defecto. Dejarlas intactas haria que el panel se
    mostrase con el aspecto de serie sin ninguna pista de por que."""
    claves = "|".join(D2P_CLAVES_POR_MONITOR)
    origen = set(re.findall(r'"([A-Z0-9]+-[A-Z0-9]+)"\s*:', contenido))
    if not origen:
        return contenido, None

    if monitor:
        for viejo in origen:
            contenido = contenido.replace(f'"{viejo}"', f'"{monitor}"')
        return contenido, None

    # Sin vendor/serial fiables (lo normal en una VM): fuera las claves.
    contenido = re.sub(rf"^({claves})=.*\n", "", contenido, flags=re.M)
    return contenido, ("no se pudo identificar el monitor de esta maquina: el panel de "
                       "dash-to-panel quedara con tamaño y posicion por defecto "
                       "(ajustalo una vez en sus preferencias)")


def paso_retoques(x: Ctx) -> None:
    c = x.c
    # burn-my-windows guarda la ruta ABSOLUTA de su perfil activo, con el
    # nombre de usuario del equipo de origen dentro.
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

    # Los atajos personalizados apuntan a programas que quiza no esten aqui.
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


PASOS = [
    ("comprobaciones", "Comprobaciones previas", paso_comprobaciones),
    ("base",           "Paquetes base", paso_apt_base),
    ("sublime",        "Sublime Text", paso_sublime),
    ("extensiones",    "Extensiones de GNOME Shell", paso_extensiones),
    ("fondos",         "Fondos de pantalla", paso_fondos),
    ("ajustes",        "Ajustes de escritorio, atajos y extensiones", paso_dconf),
    ("retoques",       "Retoques dependientes de la maquina", paso_retoques),
]


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def construir_parser() -> argparse.ArgumentParser:
    nombres = ", ".join(n for n, _, _ in PASOS)
    p = argparse.ArgumentParser(
        prog="LGW_installer.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Replica en esta maquina el escritorio GNOME de Linux_GNOME_Workspace:\n"
            "extensiones con sus ajustes individuales, atajos de teclado, tema y\n"
            "Sublime Text. Pensado para una VM recien instalada."),
        epilog=(
            "Pasos disponibles para --only y --skip:\n"
            f"  {nombres}\n"
            "\n"
            "Ejemplos:\n"
            "  python3 LGW_installer.py\n"
            "      Pregunta si quieres instalarlo todo y lo hace.\n"
            "\n"
            "  python3 LGW_installer.py -y\n"
            "      Lo instala todo sin preguntar nada (util por SSH o en un script).\n"
            "\n"
            "  python3 LGW_installer.py -n\n"
            "      Enseña lo que haria sin tocar nada.\n"
            "\n"
            "  python3 LGW_installer.py --only extensiones,ajustes\n"
            "      Solo las extensiones y su configuracion, sin Sublime ni apt.\n"
            "\n"
            "Al terminar hay que cerrar sesion y volver a entrar para que GNOME\n"
            "cargue las extensiones nuevas."),
    )
    p.add_argument("-y", "--yes", action="store_true",
                   help="no preguntar nada: instalar y configurar todo directamente")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="simular la instalacion sin modificar el sistema")
    p.add_argument("--only", metavar="PASOS",
                   help="ejecutar solo estos pasos (separados por comas)")
    p.add_argument("--skip", metavar="PASOS",
                   help="ejecutar todo menos estos pasos (separados por comas)")
    p.add_argument("-l", "--list-steps", action="store_true",
                   help="listar los pasos y salir")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="mostrar la salida de los comandos que se ejecutan")
    p.add_argument("--no-color", action="store_true",
                   help="salida sin color (tambien se respeta NO_COLOR)")
    return p


def seleccionar(args, c: Consola) -> list:
    validos = {n for n, _, _ in PASOS}
    pedidos = None
    if args.only:
        pedidos = {s.strip() for s in args.only.split(",") if s.strip()}
    omitidos = {s.strip() for s in (args.skip or "").split(",") if s.strip()}
    for s in (pedidos or set()) | omitidos:
        if s not in validos:
            c.error(f"paso desconocido: '{s}' (validos: {', '.join(sorted(validos))})")
            sys.exit(2)
    return [p for p in PASOS
            if (pedidos is None or p[0] in pedidos) and p[0] not in omitidos]


def main() -> int:
    args = construir_parser().parse_args()
    c = Consola(color=False if args.no_color else None, verboso=args.verbose)

    if args.list_steps:
        c.titulo("LGW · pasos del instalador")
        for nombre, desc, _ in PASOS:
            c.entrada(nombre, desc)
        return 0

    c.titulo("LGW · instalador del escritorio GNOME")
    if args.dry_run:
        c.aviso("modo simulacion: no se va a modificar nada")

    pasos = seleccionar(args, c)
    c.info("se van a ejecutar estas fases: " + ", ".join(n for n, _, _ in pasos))

    if not args.yes and not args.dry_run:
        if not c.preguntar("¿Instalar y configurar todo el escritorio?", por_defecto=True):
            c.saltado("cancelado por el usuario")
            return 130

    x = Ctx(c, args.dry_run, args.yes)
    c.plan(len(pasos))
    for nombre, desc, fn in pasos:
        c.paso(desc)
        try:
            fn(x)
            x.hechos += 1
        except Fallo as e:
            c.error(str(e))
            x.fallos += 1
            if nombre == "comprobaciones":
                c.error("las comprobaciones previas han fallado; no se continua")
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
        if os.environ.get("XDG_SESSION_TYPE") == "x11":
            c.info("en X11 basta con Alt+F2 y escribir 'r'")
    return 1 if x.fallos else 0


if __name__ == "__main__":
    sys.exit(main())
