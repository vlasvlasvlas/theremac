# theremac

theremac es un theremin en tiempo real para Mac Apple Silicon. Usa el acelerometro de la Mac como control de inclinacion para generar tono, volumen y, opcionalmente, filtro y vibrato.

## Que es

- Toca un sintetizador simple en tiempo real a partir de la inclinacion de la Mac.
- Puede trabajar con pitch continuo o con escalas cuantizadas.
- Permite volumen fijo o volumen controlado por inclinacion.
- Incluye una interfaz de terminal a pantalla completa con medidores, nota actual y teclado ASCII.
- Se maneja desde un launcher con menu para elegir modos, presets y configuracion.

## Como funciona

1. El sensor de aceleracion se lee desde el runtime de `mac-hardware-toys`.
2. Al iniciar, el programa calibra una pose neutra durante unos segundos.
3. Luego convierte la inclinacion en pitch, volumen y, si se activa, filtro.
4. El audio se genera en tiempo real con `sounddevice`.
5. La UI de `curses` muestra el estado actual y permite recalibrar mientras corre.

En la practica, el eje de pitch y el eje de volumen se pueden elegir de forma independiente. Tambien hay suavizado, zonas muertas y cuantizacion para hacer la respuesta mas tocable.

## Requisitos

- macOS en Apple Silicon.
- Python 3.11.
- `mac-hardware-toys` con soporte para `speaker` y `accelerometer`.
- Paquetes de Python como `numpy` y `sounddevice`.
- `sudo`, porque la lectura del sensor requiere privilegios.
- Una terminal compatible con `curses`.

## Arranque rapido

La forma recomendada es usar el launcher:

```bash
./run-theremac.sh
```

El menu principal ofrece estas opciones:

- `1` Basico.
- `2` Theremin A.
- `3` Theremin B.
- `4` Presets.
- `5` Config.
- `0` Salir.

## Modos del menu

### Basico

Pitch con inclinacion frontal y volumen fijo.

### Theremin A

- `IZQ/DER` controla la frecuencia o la nota.
- `ADELANTE/ATRAS` controla el volumen.

### Theremin B

- `ADELANTE/ATRAS` controla la frecuencia o la nota.
- `IZQ/DER` controla el volumen.

### Presets

- `Super reactivo`: menos smoothing, mas respuesta, mas jitter.
- `Super estable`: mas smoothing, menos nervioso.
- `Grave y amplio`: rango mas musical y menos chillido.
- `Agudo y sensible`: rango alto y recorrido corto.
- `Debug tecnico`: pitch, roll, frecuencia y amplitud en vivo.

### Config

- Escala y nota central.
- Anti-sleep best effort.
- Filtro de tapa.
- Parametros personalizados.

## Controles durante la ejecucion

- `q` vuelve al menu.
- `d` alterna vista simple y detalle.
- `c` recalibra el centro en vivo.

Cuando recalibres, conviene dejar la Mac quieta en la pose que quieras tomar como neutra.

## Escalas

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

## Filtro de tapa

Se puede usar la apertura de la tapa como control del cutoff del filtro de sintetizador. Los perfiles disponibles son:

- `acid`
- `soft`
- `custom`

## Uso directo sin menu

Tambien podes correr el motor directo:

```bash
python3 ./theremac.py
```

Opciones utiles del binario:

- `--sample-rate` define la tasa de salida de audio.
- `--block-size` define el tamano del bloque de audio.
- `--min-hz` y `--max-hz` fijan el rango del theremin.
- `--scale` activa `continuous` o cuantizacion por escala.
- `--root-note` define la nota central cuando la escala no es continua.
- `--scale-span-steps` define cuanto recorrido de escala cubre cada extremo.
- `--pitch-axis` elige `pitch` o `roll` para la nota.
- `--volume-mode` permite `fixed`, `roll` o `pitch`.
- `--volume-direction` limita la respuesta a `both`, `positive` o `negative`.
- `--volume-curve` ajusta la curva de respuesta del volumen.
- `--fixed-volume` fija la amplitud cuando el volumen es constante.
- `--max-volume` define el volumen maximo.
- `--pitch-range-deg` y `--volume-range-deg` controlan cuanta inclinacion cubre cada mapeo.
- `--pitch-deadzone-deg` y `--volume-deadzone-deg` agregan zonas muertas cerca del centro.
- `--center-seconds` define el tiempo de calibracion inicial.
- `--gravity-cutoff-hz` controla el filtrado para estimar gravedad.
- `--glide-ms` ajusta el suavizado de pitch y amplitud.
- `--filter-source` habilita o deshabilita el control de filtro.
- `--filter-low-hz`, `--filter-high-hz` y `--filter-resonance` definen el filtro.
- `--lid-angle-min` y `--lid-angle-max` mapean la tapa al filtro.
- `--vibrato-rate-hz` y `--vibrato-depth-cents` agregan vibrato opcional.

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
- En la vista de detalle tambien se muestran `Abs pitch` y `Abs roll`.
- El teclado ASCII queda fijo y centrado en la nota central; la tecla activa se resalta y el puntero marca la posicion musical actual.
- Si cambias mucho la postura base, usa `c` para recalibrar en vez de salir y volver a entrar.
- `anti-sleep best effort` intenta evitar sleep con `pmset` y `caffeinate`, pero con tapa cerrada macOS puede forzar sleep igual.

## Limitaciones

- Es un proyecto orientado a Apple Silicon y macOS; no es portable a otros equipos.
- Depende del runtime y los sensores del ecosistema `mac-hardware-toys`.
- Ejecutar audio y sensor bajo `sudo` puede comportarse distinto segun la configuracion del sistema.
