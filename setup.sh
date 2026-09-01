#!/bin/bash
# Installa tutto il necessario per live-translate su macOS.
# Idempotente: si puo' rilanciare, salta cio' che c'e' gia'.
set -euo pipefail
cd "$(dirname "$0")"

BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'
say() { echo "${BOLD}==>${OFF} $*"; }
ok()  { echo "  ${GREEN}ok${OFF} $*"; }
warn(){ echo "  ${YELLOW}!${OFF}  $*"; }

MODEL_DIR="models"
MODELS_BASE="https://huggingface.co/ggerganov/whisper.cpp/resolve/main"

# --- whisper.cpp -----------------------------------------------------------
say "whisper.cpp"
if command -v whisper-stream >/dev/null 2>&1 || [ -x /opt/homebrew/bin/whisper-stream ]; then
  ok "gia' installato"
else
  if command -v brew >/dev/null 2>&1; then
    brew install whisper-cpp
    ok "installato via Homebrew"
  else
    warn "Homebrew non trovato: installa whisper-cpp a mano, poi rilancia"
    exit 1
  fi
fi

# --- modello di trascrizione ----------------------------------------------
say "modello di trascrizione"
mkdir -p "$MODEL_DIR"
WANT="${LT_SETUP_MODEL:-turbo}"
case "$WANT" in
  turbo) FILE="ggml-large-v3-turbo-q5_0.bin" ;;
  small) FILE="ggml-small.bin" ;;
  base)  FILE="ggml-base.bin" ;;
  *)     FILE="ggml-large-v3-turbo-q5_0.bin" ;;
esac
if [ -s "$MODEL_DIR/$FILE" ]; then
  ok "$FILE gia' presente"
else
  echo "  scarico $FILE (qualche centinaio di MB)…"
  curl -fL --progress-bar -o "$MODEL_DIR/$FILE" "$MODELS_BASE/$FILE"
  ok "scaricato"
fi

# --- traduttore locale -----------------------------------------------------
say "traduttore locale (Argos)"
# argostranslate non supporta il python 3.9 di sistema di macOS, e un
# python3.12 installato con Homebrew puo' non essere nel PATH di questa shell
PY=""
CANDIDATES="python3.12 python3.11 python3.13 python3.10 python3"
for pfx in /opt/homebrew/bin /usr/local/bin /opt/homebrew/opt/python@3.12/bin; do
  for c in $CANDIDATES; do
    [ -x "$pfx/$c" ] && CANDIDATES="$CANDIDATES $pfx/$c"
  done
done
for c in $CANDIDATES; do
  command -v "$c" >/dev/null 2>&1 || [ -x "$c" ] || continue
  v=$("$c" -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null || echo 0)
  if [ "$v" -ge 310 ]; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  warn "serve python >= 3.10 per il traduttore locale (brew install python@3.12)"
  warn "senza, restano solo i traduttori online e le loro quote"
else
  [ -d .venv ] || "$PY" -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q argostranslate
  # giudice esterno per verify.py: senza, il riconoscimento lingua si
  # misurerebbe con le stesse liste che deve verificare
  ./.venv/bin/pip install -q langdetect
  SRC="${LT_SETUP_SRC:-pt}"; DST="${LT_SETUP_DST:-it}"
  ./.venv/bin/python - "$SRC" "$DST" <<'PY'
import sys
import argostranslate.package as pkg

src, dst = sys.argv[1], sys.argv[2]
have = {(p.from_code, p.to_code) for p in pkg.get_installed_packages()}
pkg.update_package_index()
avail = pkg.get_available_packages()


def install(f, t):
    if (f, t) in have:
        print(f"  ok  {f}->{t}")
        return True
    m = [p for p in avail if p.from_code == f and p.to_code == t]
    if not m:
        return False
    print(f"  scarico {f}->{t}...", flush=True)
    pkg.install_from_path(m[0].download())
    have.add((f, t))
    return True


for f, t in ((src, dst), (dst, src)):
    if install(f, t):
        continue
    # Argos non ha tutte le coppie dirette (pt<->it per esempio non esiste):
    # con i modelli verso l'inglese fa il pivot da solo, in modo trasparente
    if install(f, "en") and install("en", t):
        print(f"  ok  {f}->{t} via inglese")
    else:
        print(f"  !   {f}->{t} non disponibile")
PY
  ok "traduttore locale pronto"
fi

# --- app nativa ------------------------------------------------------------
say "app per il Dock"
if command -v swiftc >/dev/null 2>&1; then
  ./build-app.sh >/dev/null
  ok "LiveTranslate.app costruita"
else
  warn "swiftc non trovato (installa gli strumenti da riga di comando di Xcode)"
  warn "si puo' comunque usare ./live-translate, che apre il browser"
fi

echo
echo "${BOLD}pronto.${OFF}"
echo "  ${DIM}open LiveTranslate.app${OFF}    finestra nel Dock"
echo "  ${DIM}./live-translate${OFF}          da terminale, apre il browser"
