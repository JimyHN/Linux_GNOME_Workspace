#!/usr/bin/env python3
"""Replica en esta maquina el escritorio GNOME definido en data/.

Pensado para una VM recien instalada: clonas el repo, lo ejecutas y te deja
las extensiones, sus ajustes individuales, los atajos de teclado, el fondo y
Sublime Text tal y como estan en el ordenador de referencia.

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

APT_BASE = ["curl", "unzip", "dconf-cli", "gnome-shell-extension-prefs"]
APT_EXTENSIONES_SISTEMA = ["gnome-shell-ubuntu-extensions"]

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
    def __init__(self, consola: Consola, dry_run: bool):
        self.c = consola
        self.dry_run = dry_run
        self.hechos = 0
        self.fallos = 0
        self.notas: list[str] = []
        self.extensiones_puestas: list[str] = []
        self.fondos_puestos: list[str] = []

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
                motivo = (r.stderr or r.stdout or "").strip()
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


def monitor_actual() -> str | None:
    try:
        salida = subprocess.run(["gdctl", "show"], capture_output=True,
                                text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    vendor = re.search(r"Vendor:\s*(\S+)", salida)
    serial = re.search(r"Serial:\s*(\S+)", salida)
    if vendor and serial and vendor.group(1).lower() not in ("unknown", "n/a"):
        return f"{vendor.group(1)}-{serial.group(1)}"
    return None


def extensiones_instaladas() -> set[str]:
    return set(subprocess.run(["gnome-extensions", "list"],
                              capture_output=True, text=True).stdout.split())


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
    if not x._sudo_sin_clave():
        c.info("sudo pedira la contraseña durante la instalacion de paquetes")


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
        "fondos_puestos": [],
    }
    if not x.dry_run:
        MANIFIESTO.write_text(json.dumps(datos, indent=2, ensure_ascii=False) + "\n")
    c.ok(f"{len(guardadas)} ramas respaldadas · deshacer con --revert")


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
        c.ok(f"{len(faltan)} paquete{'s' if len(faltan) != 1 else ''} base instalado"
             f"{'s' if len(faltan) != 1 else ''}")

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
    try:
        try:
            x.correr(["gnome-extensions", "install", "--force", tmp])
            return
        except Fallo as e:
            x.c.detalle(f"gnome-extensions install fallo ({e}); se descomprime a mano")

        destino = Path.home() / ".local/share/gnome-shell/extensions" / uuid
        destino.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(tmp) as z:
            z.extractall(destino)
        schemas = destino / "schemas"
        if schemas.is_dir() and shutil.which("glib-compile-schemas"):
            x.correr(["glib-compile-schemas", str(schemas)], tolerante=True)
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

    for e in lista:
        uuid, nombre = e["uuid"], e["nombre"]
        if uuid in instaladas:
            c.saltado(f"{nombre} ya instalada")
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
    c.info("GNOME las carga al reiniciar la sesion, no antes")


def paso_fondos(x: Ctx) -> None:
    c = x.c
    origen = DATA / "backgrounds"
    imagenes = sorted(p for p in origen.glob("*") if p.is_file()) if origen.is_dir() else []
    if not imagenes:
        c.saltado("el repo no trae fondos de pantalla")
        return
    destino = Path.home() / ".local/share/backgrounds"
    if not x.dry_run:
        destino.mkdir(parents=True, exist_ok=True)
    for img in imagenes:
        c.accion("Copiando", f"{img.name} → {destino}")
        if not x.dry_run:
            if not (destino / img.name).exists():
                x.fondos_puestos.append(str(destino / img.name))
            shutil.copy2(img, destino / img.name)
    c.ok(f"{len(imagenes)} fondo{'s' if len(imagenes) != 1 else ''} en su sitio")


def _remapear_monitor(contenido: str, monitor: str | None) -> tuple[str, str | None]:
    """dash-to-panel guarda tamaño y posicion del panel por monitor."""
    claves = "|".join(D2P_CLAVES_POR_MONITOR)
    origen = set(re.findall(r'"([A-Za-z0-9]+-[A-Za-z0-9]+)"\s*:', contenido))
    if not origen:
        return contenido, None
    if monitor:
        for viejo in origen:
            contenido = contenido.replace(f'"{viejo}"', f'"{monitor}"')
        return contenido, None
    contenido = re.sub(rf"^({claves})=.*\n", "", contenido, flags=re.M)
    return contenido, ("no se pudo identificar el monitor de esta maquina: el panel de "
                       "dash-to-panel quedara con tamaño y posicion por defecto "
                       "(ajustalo una vez en sus preferencias)")


def _filtrar_habilitadas(contenido: str, instaladas: set[str]) -> tuple[str, list[str]]:
    """Quita de enabled-extensions lo que no este realmente instalado.

    Sin esto, si dash-to-panel no se llega a instalar el load lo pide igual
    —GNOME lo ignora— pero de paso desactiva ubuntu-dock, que no aparece en
    la lista porque en el equipo de origen esta apagado a proposito. El
    resultado es una sesion sin barra y sin dock, peor que no haber tocado
    nada. Si falta el sustituto, se deja lo que hubiera."""
    ausentes: list[str] = []

    def _sub(m: re.Match) -> str:
        uuids = re.findall(r"'([^']+)'", m.group(1))
        vivas = [u for u in uuids if u in instaladas]
        ausentes.extend(u for u in uuids if u not in instaladas)
        if not ausentes:
            return m.group(0)
        return "enabled-extensions=[" + ", ".join(f"'{u}'" for u in vivas) + "]"

    return re.sub(r"enabled-extensions=\[(.*?)\]", _sub, contenido), ausentes


def paso_dconf(x: Ctx) -> None:
    c = x.c
    ramas = ramas_objetivo()
    if not ramas:
        raise Fallo("no hay ningun .ini en data/dconf/")

    monitor = monitor_actual()
    instaladas = extensiones_instaladas()
    for fichero, ruta in ramas:
        contenido = fichero.read_text().replace(MARCADOR_HOME, str(Path.home()))
        etiqueta = fichero.stem.split("-", 1)[1] if "-" in fichero.stem else fichero.stem

        if "enabled-extensions=" in contenido:
            contenido, ausentes = _filtrar_habilitadas(contenido, instaladas)
            if ausentes:
                c.aviso(f"no se habilitan (no instaladas): {', '.join(ausentes)}")
                x.notas.append(
                    f"{len(ausentes)} extensiones no se habilitaron porque no estan "
                    "instaladas. No se ha tocado el dock que ya tenias, asi que no "
                    "te quedas sin barra: arregla la instalacion y repite el paso "
                    "'ajustes'")

        if fichero.stem.endswith("dash-to-panel"):
            contenido, nota = _remapear_monitor(contenido, monitor)
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
            datos["fondos_puestos"] = sorted(
                set(datos.get("fondos_puestos", [])) | set(x.fondos_puestos))
            MANIFIESTO.write_text(json.dumps(datos, indent=2, ensure_ascii=False) + "\n")
        except json.JSONDecodeError:
            c.aviso("no se pudo actualizar el manifiesto de respaldo")


PASOS = [
    ("comprobaciones", "Comprobaciones previas", paso_comprobaciones),
    ("respaldo",       "Respaldo del estado actual", paso_respaldo),
    ("base",           "Paquetes base", paso_apt_base),
    ("sublime",        "Sublime Text", paso_sublime),
    ("extensiones",    "Extensiones de GNOME Shell", paso_extensiones),
    ("fondos",         "Fondos de pantalla", paso_fondos),
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
    fondos = datos.get("fondos_puestos", [])

    c.info(f"respaldo del {datos.get('fecha', '?')} (GNOME {datos.get('gnome_shell', '?')})")
    c.bruto()
    c.entrada("Restaura", f"{len(ramas)} ramas de dconf al estado anterior")
    c.entrada("Desinstala", f"{len(extras)} extensiones que puso el instalador"
                            + (f" ({', '.join(extras)})" if extras else ""))
    c.entrada("Borra", f"{len(fondos)} fondos de pantalla copiados")
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

    if fondos:
        c.paso("Borrando fondos copiados")
        for f in fondos:
            c.accion("Borrando", f)
            if not dry_run:
                Path(f).unlink(missing_ok=True)
        c.ok(f"{len(fondos)} fondos borrados")

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
    nombres = ", ".join(n for n, _, _ in PASOS)
    return argparse.ArgumentParser(
        prog="LGW_installer.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Replica en esta maquina el escritorio GNOME de Linux_GNOME_Workspace:\n"
            "extensiones con sus ajustes individuales, atajos de teclado, fondo,\n"
            "tema y Sublime Text. Pensado para una VM recien instalada.\n"
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

    x = Ctx(c, args.dry_run)
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
