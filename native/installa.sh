#!/bin/bash
# Mette Due Voci in /Applications e la aggancia al Dock. Idempotente: rilanciarlo
# aggiorna la copia installata senza sdoppiare l'icona nel Dock.
set -e
cd "$(dirname "$0")"

SORGENTE=DueVoci.app
DESTINAZIONE="/Applications/Due Voci.app"

[ -d "$SORGENTE" ] || { echo "manca $SORGENTE: lancia prima ./build.sh"; exit 1; }

# Se sta girando va chiusa, o si copia sopra un binario in uso.
if pgrep -x DueVoci >/dev/null; then
  echo "chiudo la copia in esecuzione…"
  pkill -x DueVoci || true
  sleep 1
fi

# ditto, non cp -R: cp da certi filesystem semina file AppleDouble (._*) dentro
# il bundle, e quelli invalidano il sigillo della firma. Costato una volta, su
# un'altra app: l'app risultava "danneggiata" e la firma era intatta nel
# sorgente. ditto non li produce.
rm -rf "$DESTINAZIONE"
ditto "$SORGENTE" "$DESTINAZIONE"

codesign -v "$DESTINAZIONE" || { echo "firma non valida dopo la copia"; exit 1; }
echo "installata: $DESTINAZIONE"

# --- Dock ---------------------------------------------------------------
# Il Dock legge la sua lista da un plist e la rilegge solo quando lo si
# riavvia. Aggiungere due volte la stessa app da' due icone, quindi prima si
# guarda se c'e' gia'.
if defaults read com.apple.dock persistent-apps 2>/dev/null | grep -q "Due Voci.app"; then
  echo "gia' nel Dock, non la aggiungo di nuovo"
else
  defaults write com.apple.dock persistent-apps -array-add "<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>$DESTINAZIONE</string><key>_CFURLStringType</key><integer>0</integer></dict></dict></dict>"
  killall Dock
  echo "aggiunta al Dock"
fi

# Il Finder tiene in cache l'icona per percorso: senza questo la voce nuova puo'
# restare col rettangolo bianco finche' non si fa logout.
touch "$DESTINAZIONE"
