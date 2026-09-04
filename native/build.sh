#!/bin/bash
# Compila Due Voci e la impacchetta. Router.swift lo compilano sia l'app sia
# il banco di misura: e' la stessa decisione, non due che si assomigliano.
set -e
cd "$(dirname "$0")"
swiftc -O -parse-as-library Router.swift DueVoci.swift -o DueVoci
swiftc -O -parse-as-library Router.swift bench.swift   -o bench
cp DueVoci DueVoci.app/Contents/MacOS/DueVoci
codesign --force --deep -s - DueVoci.app >/dev/null 2>&1
codesign -v DueVoci.app
echo "fatto: DueVoci.app ($(du -sh DueVoci.app | cut -f1)) e ./bench"
