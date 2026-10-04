"""Salida de terminal en color.

Paleta derivada del escritorio que replica este repo (Yaru-magenta sobre
fondo oscuro). Regla del proyecto: no se usa blanco en ningun momento, ni
siquiera para el texto corriente, que va en lavanda clara.
"""

import os
import shutil
import sys

# Truecolor. Ningun valor es blanco ni gris neutro: el texto "normal" es
# lavanda y los apagados son malva, los dos con tinte magenta.
_C = {
    "texto":    (198, 184, 214),
    "tenue":    (138, 120, 158),
    "acento":   (220, 138, 221),
    "titulo":   (168, 112, 214),
    "ok":       (87,  227, 137),
    "aviso":    (245, 194, 86),
    "error":    (240, 97,  109),
    "info":     (109, 198, 232),
    "paso":     (152, 151, 240),
}

_RESET = "\033[0m"
_BOLD = "\033[1m"


def _soporta_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("LGW_FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


class Consola:
    def __init__(self, color: bool | None = None, verboso: bool = False):
        self.color = _soporta_color() if color is None else color
        self.verboso = verboso
        self._paso = 0
        self._total = 0
        self.saltados = 0

    # -- primitivas ------------------------------------------------------
    def _t(self, texto: str, clave: str, negrita: bool = False) -> str:
        if not self.color:
            return texto
        r, g, b = _C[clave]
        return f"{_BOLD if negrita else ''}\033[38;2;{r};{g};{b}m{texto}{_RESET}"

    def bruto(self, texto: str = "") -> None:
        print(texto)

    # -- bloques ---------------------------------------------------------
    def titulo(self, texto: str) -> None:
        ancho = min(shutil.get_terminal_size((80, 24)).columns, 72)
        print()
        print(self._t("┏" + "━" * (ancho - 2) + "┓", "titulo"))
        relleno = ancho - 4 - len(texto)
        print(
            self._t("┃ ", "titulo")
            + self._t(texto, "acento", negrita=True)
            + " " * max(relleno, 0)
            + self._t(" ┃", "titulo")
        )
        print(self._t("┗" + "━" * (ancho - 2) + "┛", "titulo"))

    def plan(self, total: int) -> None:
        self._total = total
        self._paso = 0

    def paso(self, texto: str) -> None:
        """Encabeza una fase: '[2/7] Instalando extensiones'."""
        self._paso += 1
        marca = f"[{self._paso}/{self._total}]" if self._total else "[*]"
        print()
        print(self._t(marca, "paso", negrita=True) + " " + self._t(texto, "acento", negrita=True))

    # -- lineas ----------------------------------------------------------
    def accion(self, verbo: str, objeto: str) -> None:
        """'  Instalando   blur-my-shell' — el verbo en curso, el objeto destacado."""
        print(
            "  " + self._t(f"{verbo:<13}", "info") + self._t(objeto, "texto")
        )

    def ok(self, texto: str) -> None:
        print("  " + self._t("✔ ", "ok") + self._t(texto, "texto"))

    def aviso(self, texto: str) -> None:
        print("  " + self._t("▲ ", "aviso") + self._t(texto, "aviso"))

    def error(self, texto: str) -> None:
        print("  " + self._t("✘ ", "error") + self._t(texto, "error"), file=sys.stderr)

    def info(self, texto: str) -> None:
        print("  " + self._t("· ", "tenue") + self._t(texto, "tenue"))

    def detalle(self, texto: str) -> None:
        """Solo con --verbose; para la salida de los comandos externos."""
        if self.verboso:
            for linea in texto.rstrip().splitlines():
                print("    " + self._t("│ ", "tenue") + self._t(linea, "tenue"))

    def entrada(self, clave: str, valor: str) -> None:
        """Fila de un listado: la clave en acento, la descripcion al lado."""
        print("  " + self._t(f"{clave:<22}", "acento") + self._t(valor, "texto"))

    def saltado(self, texto: str) -> None:
        self.saltados += 1
        print("  " + self._t("– ", "tenue") + self._t(texto, "tenue"))

    # -- interaccion -----------------------------------------------------
    def preguntar(self, texto: str, por_defecto: bool = True) -> bool:
        sufijo = "[S/n]" if por_defecto else "[s/N]"
        pregunta = (
            "\n" + self._t("? ", "acento", negrita=True)
            + self._t(texto, "texto") + " " + self._t(sufijo, "tenue") + " "
        )
        try:
            r = input(pregunta).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if not r:
            return por_defecto
        return r in ("s", "si", "sí", "y", "yes")

    def resumen(self, hechos: int, saltados: int, fallos: int) -> None:
        print()
        partes = [
            self._t(f"{hechos} aplicados", "ok"),
            self._t(f"{saltados} omitidos", "tenue"),
            self._t(f"{fallos} con error", "error" if fallos else "tenue"),
        ]
        print("  " + self._t("Resumen: ", "acento", negrita=True) + self._t(" · ", "tenue").join(partes))
