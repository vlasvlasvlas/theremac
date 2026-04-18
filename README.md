# theremac

Theremin en tiempo real para Apple Silicon usando el acelerometro de la Mac como control de inclinacion.

## Que hace

- Lee el acelerometro del SPU en Macs Apple Silicon.
- Convierte inclinacion en pitch continuo o cuantizado.
- Permite controlar volumen con otro eje o dejarlo fijo.
- Muestra una UI fullscreen en terminal con nota, teclado ASCII y medidores en vivo.
- Incluye menu interactivo con presets, escalas, filtro por tapa y modo personalizado.

## Requisitos

- macOS en Apple Silicon.
- `mac-hardware-toys` instalado con soporte para `speaker` y `accelerometer`.
- Python 3.11 con `numpy` y `sounddevice`.
- Acceso `sudo`, porque la lectura del sensor requiere privilegios.
- Terminal con soporte `curses` para la UI fullscreen.

## Arranque rapido

La forma recomendada es usar el menu:

```bash
./run-theremac.sh
```

Menu principal:

- `1` Basico.
- `2` Theremin A.
- `3` Theremin B.
- `4` Presets.
- `5` Config.

## Controles dentro de la UI

- `q` vuelve al menu.
- `d` alterna vista simple/detalle.
- `c` recalibra el centro en vivo.

Durante la calibracion o recalibracion, deja la Mac quieta sobre la pose que quieras usar como neutra.

## Modos principales

### Basico

- Pitch con inclinacion frontal.
- Volumen fijo.

### Theremin A

- `IZQ/DER` controla frecuencia o nota.
- `ADELANTE/ATRAS` controla volumen.

### Theremin B

- `ADELANTE/ATRAS` controla frecuencia o nota.
- `IZQ/DER` controla volumen.

## Presets incluidos

- `Super reactivo`: menos smoothing, mas respuesta, mas jitter.
- `Super estable`: mas smoothing, menos nervioso.
- `Grave y amplio`: rango mas musical y menos chillido.
- `Agudo y sensible`: rango alto y recorrido corto.
- `Debug tecnico`: diagnostico en vivo de pitch, roll, frecuencia y amplitud.

## Configuracion disponible

Desde `Config` en el menu:

- Escala y nota central.
- Anti-sleep best effort.
- Filtro de tapa.
- Parametros personalizados.

### Escalas

Escalas soportadas:

- `continuous`
- `chromatic`
- `major`
- `minor`
- `major-pentatonic`
- `minor-pentatonic`
- `blues`
- `dorian`

Cuando la escala no es `continuous`, el pitch se cuantiza alrededor de la nota central configurada.

### Filtro de tapa

Opcionalmente la apertura/cierre de la tapa puede controlar el cutoff del filtro:

- perfil `acid`
- perfil `soft`
- perfil `custom`

## Uso directo sin menu

Tambien podes correr el motor directo:

```bash
python3 ./theremac.py
```

Ejemplos utiles:

```bash
python3 ./theremac.py --fixed-volume 0.18 --block-size 64 --glide-ms 18
python3 ./theremac.py --pitch-axis roll --volume-mode pitch --volume-direction positive --max-volume 0.24
python3 ./theremac.py --pitch-axis pitch --volume-mode roll --volume-direction positive --max-volume 0.24
python3 ./theremac.py --scale major --root-note A3 --scale-span-steps 10
python3 ./theremac.py --filter-source lid --filter-low-hz 140 --filter-high-hz 5200 --filter-resonance 14
```

## Notas de operacion

- `PITCHd` y `ROLLd` en la UI son deltas respecto del centro calibrado, no angulos absolutos.
- En detalles (`d`) tambien se muestran `Abs pitch` y `Abs roll`.
- El teclado ASCII queda fijo y centrado en la nota central; la tecla activa se resalta y el puntero marca la posicion musical actual.
- Si cambias mucho la postura base, usa `c` para recalibrar en vez de salir y entrar de nuevo.
- `anti-sleep best effort` intenta evitar sleep con `pmset` y `caffeinate`, pero con tapa cerrada macOS puede forzar sleep igual.

## Limitaciones

- Es un proyecto orientado a Apple Silicon y macOS; no es portable a otros equipos.
- Depende de runtime y sensores del ecosistema `mac-hardware-toys`.
- Ejecutar audio y sensor bajo `sudo` puede comportarse distinto segun la configuracion del sistema.

## Publicacion

Antes de subirlo a remoto, revisa esto:

- El launcher `run-theremac.sh` ya no usa rutas hardcodeadas del repo.
- El `README` ahora describe la UI y el menu actuales.
- En este workspace, `.git` no es un repositorio Git valido; hay que re-inicializar o recuperar el metadata real antes de hacer `git status`, commit o push.
