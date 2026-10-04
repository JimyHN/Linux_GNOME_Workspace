#!/usr/bin/env python3
"""Captura la configuracion de GNOME de esta maquina en data/.

Es la contraparte de LGW_installer.py: se ejecuta en el ordenador de
referencia y vuelca lo que el instalador reproducira en la VM. Solo lee,
nunca modifica el escritorio.

    python3 LGW_export.py            # vuelca a data/
    python3 LGW_export.py --dry-run  # enseña lo que volcaria
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lgw.ui import Consola  # noqa: E402

RAIZ = Path(__file__).resolve().parent
DATA = RAIZ / "data"

# Ramas de dconf que se capturan, en el orden en que luego se aplican.
RAMAS = [
    ("00-interface",         "/org/gnome/desktop/interface/"),
    ("01-shell",             "/org/gnome/shell/"),
    ("10-wm-keybindings",    "/org/gnome/desktop/wm/keybindings/"),
    ("11-media-keys",        "/org/gnome/settings-daemon/plugins/media-keys/"),
    ("12-mutter-keybindings", "/org/gnome/mutter/keybindings/"),
    ("13-shell-keybindings", "/org/gnome/shell/keybindings/"),
]

# /org/gnome/shell/ completo arrastra basura (posiciones de iconos, estado
# del grid de apps, contadores). Solo estas claves viajan.
SHELL_CLAVES = {"favorite-apps", "enabled-extensions", "disable-user-extensions"}

# Algunos ajustes guardan rutas absolutas con el nombre de usuario dentro
# (burn-my-windows y su active-profile). Ni el repo es sitio para publicarlo
# ni la ruta valdria en otra maquina, asi que viaja como marcador y el
# instalador lo resuelve al cargar.
MARCADOR_HOME = "@LGW_HOME@"


def correr(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout


def anonimizar(volcado: str) -> str:
    return volcado.replace(str(Path.home()), MARCADOR_HOME)


def extensiones_usuario() -> list[dict]:
    """Las de ~/.local/share: hay que descargarlas. Las del sistema vienen
    con la distro y no se tocan."""
    base = Path.home() / ".local/share/gnome-shell/extensions"
    fuera = []
    if not base.is_dir():
        return fuera
    for d in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        meta = d / "metadata.json"
        if not meta.is_file():
            continue
        try:
            m = json.loads(meta.read_text())
        except json.JSONDecodeError:
            continue
        fuera.append({
            "uuid": m.get("uuid", d.name),
            "nombre": m.get("name", d.name),
            "version_origen": m.get("version"),
            "url": m.get("url", ""),
        })
    return fuera


def filtrar_shell(volcado: str) -> str:
    """Deja solo las claves de SHELL_CLAVES de la seccion raiz."""
    fuera, en_raiz = [], False
    for linea in volcado.splitlines():
        if linea.startswith("["):
            en_raiz = linea.strip() == "[/]"
            if en_raiz:
                fuera.append(linea)
            continue
        if not en_raiz or not linea.strip():
            continue
        clave = linea.split("=", 1)[0].strip()
        if clave in SHELL_CLAVES:
            fuera.append(linea)
    return "\n".join(fuera) + "\n"


def limpiar_enabled(volcado: str, instaladas: set[str]) -> tuple[str, list[str]]:
    """Quita de enabled-extensions las que no estan instaladas.

    Es facil acumular huerfanas: desinstalas una extension y su UUID se
    queda en la lista, asi que el instalador intentaria habilitar algo que
    no existe."""
    huerfanas = []

    def _sub(m: re.Match) -> str:
        uuids = re.findall(r"'([^']+)'", m.group(1))
        vivas = [u for u in uuids if u in instaladas]
        huerfanas.extend(u for u in uuids if u not in instaladas)
        return "enabled-extensions=[" + ", ".join(f"'{u}'" for u in vivas) + "]"

    return re.sub(r"enabled-extensions=\[(.*?)\]", _sub, volcado), huerfanas


def main() -> int:
    p = argparse.ArgumentParser(
        description="Captura la configuracion de GNOME de esta maquina en data/.")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="no escribe nada, solo informa")
    p.add_argument("--no-color", action="store_true", help="salida sin color")
    args = p.parse_args()

    c = Consola(color=False if args.no_color else None)
    c.titulo("LGW · exportar configuracion de esta maquina")

    if not shutil.which("dconf"):
        c.error("dconf no esta disponible; esto hay que ejecutarlo en el GNOME de origen")
        return 1

    destino_dconf = DATA / "dconf"
    if not args.dry_run:
        destino_dconf.mkdir(parents=True, exist_ok=True)

    instaladas = set(correr(["gnome-extensions", "list"]).split())
    usuario = extensiones_usuario()

    # --- extensiones ----------------------------------------------------
    c.paso("Extensiones")
    shell_ver = correr(["gnome-shell", "--version"]).strip().split()[-1]
    manifiesto = {
        "capturado_en": {
            "gnome_shell": shell_ver,
            "distro": correr(["lsb_release", "-ds"]).strip().strip('"'),
        },
        "extensiones_usuario": usuario,
    }
    for e in usuario:
        c.accion("Capturada", f"{e['nombre']} (v{e['version_origen']})")
    if not args.dry_run:
        (DATA / "extensions.json").write_text(
            json.dumps(manifiesto, indent=2, ensure_ascii=False) + "\n")
    c.ok(f"{len(usuario)} extensiones de usuario en extensions.json")

    # --- dconf ----------------------------------------------------------
    c.paso("Ramas de dconf")
    for nombre, ruta in RAMAS:
        volcado = correr(["dconf", "dump", ruta])
        if nombre == "01-shell":
            volcado = filtrar_shell(volcado)
            volcado, huerfanas = limpiar_enabled(volcado, instaladas)
            for h in huerfanas:
                c.aviso(f"'{h}' esta en enabled-extensions pero no instalada; se descarta")
        if not volcado.strip() or volcado.strip() == "[/]":
            c.saltado(f"{nombre} (vacia)")
            continue
        texto = f"# dconf-path: {ruta}\n# Generado por LGW_export.py — no editar a mano.\n{anonimizar(volcado)}"
        c.accion("Volcada", f"{nombre}  → {ruta}")
        if not args.dry_run:
            (destino_dconf / f"{nombre}.ini").write_text(texto)

    # --- extensiones: una rama por extension ----------------------------
    c.paso("Ajustes individuales de cada extension")
    vivas = {e["uuid"].split("@")[0].lower() for e in usuario} | {
        u.split("@")[0].lower() for u in instaladas}
    for rama in correr(["dconf", "list", "/org/gnome/shell/extensions/"]).split():
        nombre = rama.rstrip("/")
        ruta = f"/org/gnome/shell/extensions/{nombre}/"
        volcado = correr(["dconf", "dump", ruta])
        if not volcado.strip() or volcado.strip() == "[/]":
            continue
        if nombre.lower() not in vivas:
            c.aviso(f"{nombre}: hay ajustes guardados pero la extension no esta instalada; se descarta")
            continue
        texto = f"# dconf-path: {ruta}\n# Generado por LGW_export.py — no editar a mano.\n{anonimizar(volcado)}"
        c.accion("Volcados", f"{nombre} ({len(volcado.splitlines())} lineas)")
        if not args.dry_run:
            (destino_dconf / f"30-ext-{nombre}.ini").write_text(texto)

    # --- perfiles de burn-my-windows ------------------------------------
    c.paso("Perfiles de burn-my-windows")
    origen = Path.home() / ".config/burn-my-windows/profiles"
    if origen.is_dir() and any(origen.glob("*.conf")):
        destino = DATA / "burn-my-windows"
        if not args.dry_run:
            destino.mkdir(parents=True, exist_ok=True)
        for f in sorted(origen.glob("*.conf")):
            c.accion("Copiado", f.name)
            if not args.dry_run:
                shutil.copy2(f, destino / f.name)
        c.info("el instalador reescribira la ruta absoluta de active-profile")
    else:
        c.saltado("no hay perfiles")

    c.paso("Listo")
    if args.dry_run:
        c.aviso("dry-run: no se ha escrito nada")
    else:
        c.ok(f"configuracion capturada en {DATA.relative_to(RAIZ)}/")
    c.info("revisa el diff antes de commitear: los volcados pueden traer rutas o identificadores personales")
    return 0


if __name__ == "__main__":
    sys.exit(main())
