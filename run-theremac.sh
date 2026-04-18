#!/bin/zsh

set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
THEREMAC="$ROOT_DIR/theremac.py"
if [[ -x "/opt/homebrew/opt/python@3.11/bin/python3.11" ]]; then
  PYTHON_BIN="/opt/homebrew/opt/python@3.11/bin/python3.11"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

typeset -A POWER_ORIG=()
typeset -a POWER_SECTIONS=()

ANTI_SLEEP_ENABLED=0
POWER_OVERRIDE_ACTIVE=0
CAFFEINATE_PID=""
SCALE_MODE="continuous"
SCALE_ROOT="A3"
SCALE_SPAN_STEPS="10"
FILTER_ENABLED=0
FILTER_PROFILE="acid"
FILTER_LOW_HZ="140"
FILTER_HIGH_HZ="5200"
FILTER_RESONANCE="14"
FILTER_LID_MIN="8"
FILTER_LID_MAX="40"

if [[ -t 1 && "${TERM:-}" != "dumb" ]]; then
  C_RESET=$'\033[0m'
  C_BOLD=$'\033[1m'
  C_DIM=$'\033[2m'
  C_CYAN=$'\033[36m'
  C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'
else
  C_RESET=""
  C_BOLD=""
  C_DIM=""
  C_CYAN=""
  C_GREEN=""
  C_YELLOW=""
fi

if [[ ! -x "$THEREMAC" ]]; then
  echo "No encuentro $THEREMAC" >&2
  exit 1
fi

if [[ "$PYTHON_BIN" == */* ]]; then
  if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "No encuentro interprete Python en $PYTHON_BIN" >&2
    exit 1
  fi
elif ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "No encuentro interprete Python '$PYTHON_BIN' en PATH" >&2
  exit 1
fi

pmset_value() {
  local section="$1"
  local key="$2"
  pmset -g custom | awk -v target="$section" -v key="$key" '
    $0 == target ":" { in_section=1; next }
    /^[A-Za-z].*:$/ && $0 != target ":" { in_section=0 }
    in_section && $1 == key { print $2; exit }
  '
}

save_power_profile() {
  POWER_SECTIONS=()
  local custom_output
  custom_output="$(pmset -g custom 2>/dev/null || true)"

  if [[ "$custom_output" == *"AC Power:"* ]]; then
    POWER_SECTIONS+=("AC Power")
  fi
  if [[ "$custom_output" == *"Battery Power:"* ]]; then
    POWER_SECTIONS+=("Battery Power")
  fi

  local section key value
  for section in "${POWER_SECTIONS[@]}"; do
    for key in sleep displaysleep disksleep ttyskeepawake; do
      value="$(pmset_value "$section" "$key" || true)"
      if [[ -n "$value" ]]; then
        POWER_ORIG["$section:$key"]="$value"
      fi
    done
  done
}

apply_power_override() {
  if [[ "$ANTI_SLEEP_ENABLED" -ne 1 || "$POWER_OVERRIDE_ACTIVE" -eq 1 ]]; then
    return
  fi

  echo
  echo "Anti-sleep best effort: intentando evitar sleep mientras corre theremac."
  echo "Nota: con tapa cerrada en algunos MacBooks igual puede dormir si macOS/firmaware lo fuerza."
  sudo -v

  save_power_profile

  local section flag
  for section in "${POWER_SECTIONS[@]}"; do
    if [[ "$section" == "AC Power" ]]; then
      flag="-c"
    else
      flag="-b"
    fi
    sudo pmset "$flag" sleep 0 displaysleep 0 disksleep 0 ttyskeepawake 1 >/dev/null
  done

  if command -v caffeinate >/dev/null 2>&1; then
    caffeinate -dimsu &
    CAFFEINATE_PID="$!"
  fi

  POWER_OVERRIDE_ACTIVE=1
}

restore_power_override() {
  if [[ "$POWER_OVERRIDE_ACTIVE" -ne 1 ]]; then
    return
  fi

  local section flag args key value
  for section in "${POWER_SECTIONS[@]}"; do
    if [[ "$section" == "AC Power" ]]; then
      flag="-c"
    else
      flag="-b"
    fi

    args=()
    for key in sleep displaysleep disksleep ttyskeepawake; do
      value="${POWER_ORIG[$section:$key]-}"
      if [[ -n "$value" ]]; then
        args+=("$key" "$value")
      fi
    done

    if (( ${#args[@]} > 0 )); then
      sudo pmset "$flag" "${args[@]}" >/dev/null
    fi
  done

  if [[ -n "$CAFFEINATE_PID" ]]; then
    kill "$CAFFEINATE_PID" >/dev/null 2>&1 || true
    wait "$CAFFEINATE_PID" 2>/dev/null || true
  fi

  POWER_OVERRIDE_ACTIVE=0
  CAFFEINATE_PID=""
}

cleanup_and_exit() {
  restore_power_override
  exit 0
}

trap cleanup_and_exit INT TERM
trap restore_power_override EXIT

scale_args() {
  if [[ "$SCALE_MODE" == "continuous" ]]; then
    return 0
  fi

  print -- "--scale" "$SCALE_MODE" "--root-note" "$SCALE_ROOT" "--scale-span-steps" "$SCALE_SPAN_STEPS"
}

configure_scale() {
  echo
  echo "Modo de escala actual: $SCALE_MODE"
  cat <<'EOF'
1. continuous
2. chromatic
3. major
4. minor
5. major-pentatonic
6. minor-pentatonic
7. blues
8. dorian
EOF
  printf "Elegi escala [1-8]: "
  read -r scale_choice

  case "$scale_choice" in
    1) SCALE_MODE="continuous" ;;
    2) SCALE_MODE="chromatic" ;;
    3) SCALE_MODE="major" ;;
    4) SCALE_MODE="minor" ;;
    5) SCALE_MODE="major-pentatonic" ;;
    6) SCALE_MODE="minor-pentatonic" ;;
    7) SCALE_MODE="blues" ;;
    8) SCALE_MODE="dorian" ;;
    *) echo "Escala invalida."; return ;;
  esac

  if [[ "$SCALE_MODE" != "continuous" ]]; then
    printf "Nota central actual [%s] (ej: C4, A3 o solo C): " "$SCALE_ROOT"
    read -r root_input
    if [[ -n "${root_input:-}" ]]; then
      SCALE_ROOT="$root_input"
    fi

    printf "Cantidad de pasos hacia cada lado [%s]: " "$SCALE_SPAN_STEPS"
    read -r step_input
    if [[ -n "${step_input:-}" ]]; then
      SCALE_SPAN_STEPS="$step_input"
    fi
  fi
}

toggle_anti_sleep() {
  if [[ "$ANTI_SLEEP_ENABLED" -eq 1 ]]; then
    ANTI_SLEEP_ENABLED=0
    echo "Anti-sleep best effort desactivado."
  else
    ANTI_SLEEP_ENABLED=1
    echo "Anti-sleep best effort activado."
  fi
}

set_filter_profile() {
  local profile="$1"
  case "$profile" in
    soft)
      FILTER_PROFILE="soft"
      FILTER_LOW_HZ="220"
      FILTER_HIGH_HZ="3600"
      FILTER_RESONANCE="6"
      FILTER_LID_MIN="10"
      FILTER_LID_MAX="45"
      ;;
    acid)
      FILTER_PROFILE="acid"
      FILTER_LOW_HZ="140"
      FILTER_HIGH_HZ="5200"
      FILTER_RESONANCE="14"
      FILTER_LID_MIN="8"
      FILTER_LID_MAX="40"
      ;;
    custom)
      FILTER_PROFILE="custom"
      ;;
    *)
      echo "Perfil de filtro invalido."
      return 1
      ;;
  esac
}

configure_filter() {
  echo
  echo "Filtro de tapa actual: $([[ "$FILTER_ENABLED" -eq 1 ]] && echo ON || echo OFF)"
  echo "Perfil actual: $FILTER_PROFILE"
  echo "Cutoff: $FILTER_LOW_HZ -> $FILTER_HIGH_HZ Hz"
  echo "Resonancia: $FILTER_RESONANCE"
  echo "Rango tapa: $FILTER_LID_MIN -> $FILTER_LID_MAX deg"
  cat <<'EOF'
1. Apagado
2. Prendido - perfil acid
3. Prendido - perfil soft
4. Prendido - personalizado
EOF
  printf "Elegi opcion [1-4]: "
  read -r filter_choice

  case "$filter_choice" in
    1)
      FILTER_ENABLED=0
      ;;
    2)
      FILTER_ENABLED=1
      set_filter_profile acid
      ;;
    3)
      FILTER_ENABLED=1
      set_filter_profile soft
      ;;
    4)
      FILTER_ENABLED=1
      set_filter_profile custom
      printf "Cutoff minimo [%s]: " "$FILTER_LOW_HZ"
      read -r low_input
      if [[ -n "${low_input:-}" ]]; then
        FILTER_LOW_HZ="$low_input"
      fi
      printf "Cutoff maximo [%s]: " "$FILTER_HIGH_HZ"
      read -r high_input
      if [[ -n "${high_input:-}" ]]; then
        FILTER_HIGH_HZ="$high_input"
      fi
      printf "Resonancia [%s]: " "$FILTER_RESONANCE"
      read -r res_input
      if [[ -n "${res_input:-}" ]]; then
        FILTER_RESONANCE="$res_input"
      fi
      printf "Angulo minimo de tapa [%s]: " "$FILTER_LID_MIN"
      read -r min_input
      if [[ -n "${min_input:-}" ]]; then
        FILTER_LID_MIN="$min_input"
      fi
      printf "Angulo maximo de tapa [%s]: " "$FILTER_LID_MAX"
      read -r max_input
      if [[ -n "${max_input:-}" ]]; then
        FILTER_LID_MAX="$max_input"
      fi
      ;;
    *)
      echo "Opcion invalida."
      return
      ;;
  esac
}

run_mode() {
  local label="$1"
  shift

  local -a args=("$@")
  local -a extra_scale_args=()
  local -a extra_filter_args=()

  if [[ "$SCALE_MODE" != "continuous" ]]; then
    extra_scale_args=(--scale "$SCALE_MODE" --root-note "$SCALE_ROOT" --scale-span-steps "$SCALE_SPAN_STEPS")
  fi
  if [[ "$FILTER_ENABLED" -eq 1 ]]; then
    extra_filter_args=(
      --filter-source lid
      --filter-low-hz "$FILTER_LOW_HZ"
      --filter-high-hz "$FILTER_HIGH_HZ"
      --filter-resonance "$FILTER_RESONANCE"
      --lid-angle-min "$FILTER_LID_MIN"
      --lid-angle-max "$FILTER_LID_MAX"
    )
  fi

  echo
  echo "Modo: $label"
  echo "Comando:"
  printf '  %q ' "$PYTHON_BIN" "$THEREMAC" "${args[@]}" "${extra_scale_args[@]}" "${extra_filter_args[@]}"
  echo

  apply_power_override
  "$PYTHON_BIN" "$THEREMAC" "${args[@]}" "${extra_scale_args[@]}" "${extra_filter_args[@]}"
  local exit_code=$?
  restore_power_override
  return "$exit_code"
}

run_custom() {
  printf "Escribi parametros extra para theremac.py: "
  read -r extra_args
  local -a parsed_args=("${(@z)extra_args}")
  run_mode "Personalizado" "${parsed_args[@]}"
}

format_on_off() {
  if [[ "$1" -eq 1 ]]; then
    print -- "${C_GREEN}ON${C_RESET}"
  else
    print -- "${C_DIM}OFF${C_RESET}"
  fi
}

show_shell_header() {
  clear
  echo "${C_DIM}============================================================${C_RESET}"
  echo "${C_BOLD}${C_CYAN}THEREMAC${C_RESET}"
  echo "${C_DIM}============================================================${C_RESET}"
  echo "escala   : ${C_YELLOW}${SCALE_MODE}${C_RESET}   centro: ${SCALE_ROOT}   pasos: ${SCALE_SPAN_STEPS}"
  echo "anti-slp : $(format_on_off "$ANTI_SLEEP_ENABLED")   filtro: $([[ "$FILTER_ENABLED" -eq 1 ]] && printf '%sON%s (%s)' "$C_GREEN" "$C_RESET" "$FILTER_PROFILE" || printf '%sOFF%s' "$C_DIM" "$C_RESET")"
  echo
}

show_main_menu() {
  show_shell_header
  cat <<EOF
${C_BOLD}[ PRINCIPAL ]${C_RESET}

  1. Basico
     Pitch frontal con volumen fijo.

  2. Theremin A
     IZQ/DER = frecuencia o nota.
     ADELANTE/ATRAS = volumen.

  3. Theremin B
     ADELANTE/ATRAS = frecuencia o nota.
     IZQ/DER = volumen.

  4. Presets
     Modos alternativos y debug.

  5. Config
     Escala, anti-sleep, filtro y personalizado.

  0. Salir

Dentro de theremac: q = volver al menu.
EOF
}

show_presets_menu() {
  show_shell_header
  cat <<EOF
${C_BOLD}[ PRESETS ]${C_RESET}

  1. Super reactivo
     Menos smoothing, mas respuesta, mas jitter.

  2. Super estable
     Mas smoothing, menos nervioso.

  3. Grave y amplio
     Rango mas musical y menos chillido.

  4. Agudo y sensible
     Rango mas alto y control corto.

  5. Debug tecnico
     Pitch, roll, frecuencia y amplitud en vivo.

  0. Volver
EOF
}

show_config_menu() {
  show_shell_header
  cat <<EOF
${C_BOLD}[ CONFIG ]${C_RESET}

  1. Escala y nota central
     Activa cuantizacion y define el centro.

  2. Anti-sleep best effort
     Ajusta pmset + caffeinate mientras corre.

  3. Filtro de tapa
     ON/OFF y perfil recordado mientras el menu siga abierto.

  4. Personalizado
     Parametros extra sin recordar el comando base.

  0. Volver
EOF
}

show_presets_loop() {
  while true; do
    show_presets_menu
    printf "\nElegi un preset: "
    read -r choice

    case "$choice" in
      1)
        run_mode \
          "Super reactivo" \
          --fixed-volume 0.18 \
          --gravity-cutoff-hz 12 \
          --glide-ms 10 \
          --block-size 32 \
          --pitch-deadzone-deg 1.0
        ;;
      2)
        run_mode \
          "Super estable" \
          --fixed-volume 0.18 \
          --gravity-cutoff-hz 5 \
          --glide-ms 30 \
          --block-size 128 \
          --pitch-deadzone-deg 2.5
        ;;
      3)
        run_mode \
          "Grave y amplio" \
          --min-hz 110 \
          --max-hz 880 \
          --fixed-volume 0.20 \
          --pitch-range-deg 40 \
          --block-size 64
        ;;
      4)
        run_mode \
          "Agudo y sensible" \
          --min-hz 330 \
          --max-hz 2640 \
          --fixed-volume 0.14 \
          --pitch-range-deg 22 \
          --gravity-cutoff-hz 10 \
          --block-size 32
        ;;
      5)
        run_mode \
          "Debug tecnico" \
          --volume-mode roll \
          --max-volume 0.24 \
          --pitch-range-deg 30 \
          --volume-range-deg 18 \
          --block-size 64 \
          --glide-ms 18 \
          --debug
        ;;
      0)
        return
        ;;
      *)
        echo "Opcion invalida."
        sleep 1
        ;;
    esac
  done
}

show_config_loop() {
  while true; do
    show_config_menu
    printf "\nElegi una opcion: "
    read -r choice

    case "$choice" in
      1)
        configure_scale
        ;;
      2)
        toggle_anti_sleep
        sleep 1
        ;;
      3)
        configure_filter
        ;;
      4)
        run_custom
        ;;
      0)
        return
        ;;
      *)
        echo "Opcion invalida."
        sleep 1
        ;;
    esac
  done
}

while true; do
  show_main_menu
  printf "\nElegi una opcion: "
  read -r choice

  case "$choice" in
    1)
      run_mode \
        "Basico estable" \
        --fixed-volume 0.18 \
        --block-size 64 \
        --glide-ms 18 \
        --gravity-cutoff-hz 8
      ;;
    2)
      run_mode \
        "Theremin A: IZQ/DER nota, ADELANTE/ATRAS volumen" \
        --pitch-axis roll \
        --volume-mode pitch \
        --volume-direction positive \
        --volume-curve 1.8 \
        --max-volume 0.24 \
        --pitch-range-deg 30 \
        --volume-range-deg 14 \
        --volume-deadzone-deg 3.5 \
        --block-size 64 \
        --glide-ms 18
      ;;
    3)
      run_mode \
        "Theremin B: ADELANTE/ATRAS nota, IZQ/DER volumen" \
        --pitch-axis pitch \
        --volume-mode roll \
        --volume-direction positive \
        --volume-curve 1.8 \
        --max-volume 0.24 \
        --pitch-range-deg 30 \
        --volume-range-deg 14 \
        --volume-deadzone-deg 3.5 \
        --block-size 64 \
        --glide-ms 18
      ;;
    4)
      show_presets_loop
      ;;
    5)
      show_config_loop
      ;;
    0)
      exit 0
      ;;
    *)
      echo "Opcion invalida."
      sleep 1
      ;;
  esac
done
