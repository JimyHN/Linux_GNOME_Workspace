# Linux_GNOME_Workspace

Monta de una tacada el escritorio GNOME que uso, en una máquina nueva o una VM
recién instalada: extensiones con su configuración individual, atajos de
teclado, tema y Sublime Text.

```bash
git clone https://github.com/JimyHN/Linux_GNOME_Workspace.git
cd Linux_GNOME_Workspace
python3 LGW_installer.py
```

Te pregunta una vez y lo hace todo. Con `-y` ni pregunta. Al terminar hay que
cerrar sesión y volver a entrar para que GNOME cargue las extensiones.

> No lo ejecutes con `sudo`: la configuración es del usuario. El script pide
> `sudo` por su cuenta solo para instalar paquetes.

## Qué deja instalado

**Extensiones de usuario**, descargadas de extensions.gnome.org en la versión
que corresponda a tu GNOME:

Blur my Shell · Burn My Windows · Caffeine · Clipboard Indicator ·
Dash to Panel · GSConnect · Show My IP · User Themes · Vitals

**Extensiones de la distro** (`gnome-shell-ubuntu-extensions`): Desktop Icons
NG, Tiling Assistant, AppIndicators.

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
| `Ctrl`+`Super`+`-` / `,` / `.` | Pantalla derecha · izquierda · ambas |

**Tema**: Yaru-magenta-dark con acento rosa y modo oscuro.

**Sublime Text**, desde el repositorio oficial de sublimehq con la clave GPG
en `/etc/apt/keyrings` (nada de `apt-key`).

## Opciones

```
python3 LGW_installer.py -h                       # ayuda con ejemplos
python3 LGW_installer.py -y                       # sin preguntar
python3 LGW_installer.py -n                       # simular, no toca nada
python3 LGW_installer.py --only extensiones,ajustes
python3 LGW_installer.py --skip sublime
python3 LGW_installer.py -l                       # listar los pasos
```

## Actualizar la configuración guardada

Cuando cambies algo en el ordenador de referencia:

```bash
python3 LGW_export.py          # regenera data/
git diff                       # revísalo: puede traer rutas personales
git commit -am "export: ..."
```

## Qué no viaja igual

Algunas cosas dependen del hardware y el instalador las adapta sola: el panel
de Dash to Panel está anclado al número de serie del monitor de origen, así
que lo remapea al de la VM o lo deja por defecto si no puede identificarlo.
Los atajos que abren programas que no estén instalados en la VM se avisan al
final en vez de fallar en silencio.

## Licencia

MIT
