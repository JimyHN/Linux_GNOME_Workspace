# Linux_GNOME_Workspace

Monta de una tacada el escritorio GNOME que uso, en una máquina nueva o una VM
recién instalada: extensiones con su configuración individual, atajos de
teclado, tema y Sublime Text.

Funciona en **Ubuntu y en Kali** (y en cualquier Debian). Detecta la distro y
adapta los nombres de paquete, los UUID de las extensiones del sistema y los
favoritos del dash, que no son los mismos en una y otra.

```bash
git clone https://github.com/JimyHN/Linux_GNOME_Workspace.git
cd Linux_GNOME_Workspace
python3 LGW_installer.py
```

Te pregunta una vez y lo hace todo. Con `-y` ni pregunta.

Las extensiones se instalan pidiéndoselo a GNOME, así que verás un diálogo de
confirmación por cada una y quedarán activas al momento. Si prefieres que no
pregunte nada, `--zip` las baja directamente, pero entonces hay que cerrar
sesión para que GNOME las cargue.

> No lo ejecutes con `sudo`: la configuración es del usuario. El script pide
> `sudo` por su cuenta solo para instalar paquetes.

## Qué deja instalado

**Extensiones de usuario**, instaladas a través de GNOME Shell igual que si
pulsaras el interruptor en extensions.gnome.org — sale un diálogo de
confirmación por cada una y quedan activas al momento, sin cerrar sesión:

Blur my Shell · Burn My Windows · Caffeine · Clipboard Indicator ·
Dash to Panel · GSConnect · Show My IP · User Themes · Vitals

**Extensiones de la distro**: Desktop Icons NG y Tiling Assistant. En Ubuntu
vienen en `gnome-shell-ubuntu-extensions`; en Kali son paquetes sueltos y
Tiling Assistant tiene otro UUID, así que el instalador lo traduce solo.

**Cada una con su configuración**: los sensores que enseña Vitals, el blur y
los pipelines de Blur my Shell, los colores y el estilo del panel de
Dash to Panel, el efecto Glide de Burn My Windows…

**Atajos de teclado**:

| Atajo | Acción |
|---|---|
| `Super`+`Enter` | Terminal |
| `Super`+`C` | Configuración |
| `Super`+`W` | Cerrar ventana |
| `Shift`+`Super`+`F` / `V` / `D` / `T` / `K` | Firefox · VS Code · Discord · Thunderbird · Deskflow |
| `Shift`+`Super`+`B` | BurpSuite |
| `Ctrl`+`Super`+`.` | Ambas pantallas |

**Tema y escritorio**: Yaru-magenta-dark con acento rosa y modo oscuro,
teclado español, numlock encendido, sin bloqueo de sesión ni apagado de
pantalla, y `edge-tiling` desactivado para que mande Tiling Assistant.

**El fondo de pantalla no se toca.** Cada máquina se queda con el suyo.

**Sublime Text**, desde el repositorio oficial de sublimehq con la clave GPG
en `/etc/apt/keyrings` (nada de `apt-key`).

## Opciones

```
python3 LGW_installer.py -h                       # ayuda con ejemplos
python3 LGW_installer.py -y                       # sin preguntar
python3 LGW_installer.py -r                       # deshacer la instalación
python3 LGW_installer.py -n                       # simular, no toca nada
python3 LGW_installer.py --only extensiones,ajustes
python3 LGW_installer.py --skip sublime
python3 LGW_installer.py --zip                    # extensiones sin diálogos (requiere cerrar sesión)
python3 LGW_installer.py -d                       # diagnosticar extensiones
python3 LGW_installer.py -l                       # listar los pasos
```

## Deshacer

El instalador guarda un respaldo antes de tocar nada:

```bash
python3 LGW_installer.py --revert     # o -r
```

Pide confirmación y devuelve el escritorio al estado anterior: restaura las
ramas de dconf y desinstala las extensiones que puso. **No** desinstala paquetes de apt, así que Sublime Text se queda.

Si nunca has ejecutado el instalador en esa máquina, te lo dice y no hace
nada.

## Actualizar la configuración guardada

`data/` se mantiene a mano. Para cambiar un ajuste, vuelca la rama y pega el
resultado bajo la cabecera `# dconf-path:` del `.ini` correspondiente:

```bash
dconf dump /org/gnome/shell/extensions/vitals/
```

## Qué no viaja igual

Algunas cosas dependen del hardware y el instalador las adapta sola: el panel
de Dash to Panel está anclado al número de serie del monitor de origen, así
que lo remapea al de la VM o lo deja por defecto si no puede identificarlo.
Los atajos que abren programas que no estén instalados en la VM se avisan al
final en vez de fallar en silencio.

## Licencia

MIT
