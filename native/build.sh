#!/bin/bash
# Compila Due Voci e la impacchetta. Router.swift lo compilano sia l'app sia
# il banco di misura: e' la stessa decisione, non due che si assomigliano.
set -e
cd "$(dirname "$0")"

RES=DueVoci.app/Contents/Resources
ICNS=$RES/DueVoci.icns

# L'icona si rifa' solo se il disegno e' cambiato: costa qualche secondo e non
# c'e' motivo di pagarli a ogni compilazione. Il confronto e' sul sorgente,
# cosi' non puo' restare indietro.
if [ ! -f "$ICNS" ] || [ icona.swift -nt "$ICNS" ]; then
  echo "disegno l'icona…"
  mkdir -p "$RES"
  swiftc -O icona.swift -o /tmp/dv-icona
  /tmp/dv-icona /tmp/DueVoci.iconset >/dev/null
  iconutil -c icns /tmp/DueVoci.iconset -o "$ICNS"
  rm -rf /tmp/DueVoci.iconset /tmp/dv-icona
fi

swiftc -O -parse-as-library Router.swift DueVoci.swift -o DueVoci
swiftc -O -parse-as-library Router.swift bench.swift   -o bench
cp DueVoci DueVoci.app/Contents/MacOS/DueVoci
codesign --force --deep -s - DueVoci.app >/dev/null 2>&1
codesign -v DueVoci.app
echo "fatto: DueVoci.app ($(du -sh DueVoci.app | cut -f1)) e ./bench"
echo "installala con ./native/installa.sh"
