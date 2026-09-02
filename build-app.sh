#!/bin/bash
# Costruisce LiveTranslate.app: finestra nativa, icona nel Dock, sempre in primo piano.
set -euo pipefail
cd "$(dirname "$0")"

APP="LiveTranslate.app"
command -v swiftc >/dev/null 2>&1 || { echo "serve swiftc (strumenti da riga di comando di Xcode)"; exit 1; }

echo "==> compilo"
swiftc -O app/main.swift -o app/LiveTranslate

echo "==> bundle"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp app/LiveTranslate "$APP/Contents/MacOS/LiveTranslate"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Live Translate</string>
  <key>CFBundleDisplayName</key><string>Live Translate</string>
  <key>CFBundleIdentifier</key><string>com.livetranslate.app</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Live Translate trascrive e traduce quello che sente il microfono.</string>
  <key>NSAppTransportSecurity</key><dict>
    <key>NSAllowsLocalNetworking</key><true/>
    <key>NSExceptionDomains</key><dict><key>127.0.0.1</key><dict>
      <key>NSExceptionAllowsInsecureHTTPLoads</key><true/></dict></dict>
  </dict>
</dict></plist>
PLIST

# il launcher riusa il server se e' gia' su, altrimenti lo avvia. Chiudendo
# l'app il server si spegne da solo: prima restava orfano a trascrivere per ore.
cat > "$APP/Contents/MacOS/launcher" <<'SH'
#!/bin/bash
DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
PORT="${LT_PORT:-8777}"
# la prova del trascinamento apre solo la finestra e esce: il motore non
# c'entra, e accenderlo lascerebbe whisper acceso per niente
if [ "${LT_DRAGTEST:-}" = "1" ]; then exec "$(dirname "$0")/LiveTranslate"; fi
if ! curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/"; then
  cd "$DIR" && nohup /usr/bin/python3 live_translate.py --no-open --port "$PORT" \
    > /tmp/live-translate.log 2>&1 &
  for _ in $(seq 1 40); do
    curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/" && break
    sleep 0.25
  done
fi
exec "$(dirname "$0")/LiveTranslate"
SH
chmod +x "$APP/Contents/MacOS/launcher"

echo "==> icona"
if [ -f icon/AppIcon.icns ]; then
  cp icon/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
elif [ -f icon/icon.png ] && command -v iconutil >/dev/null 2>&1; then
  rm -rf /tmp/lt.iconset && mkdir -p /tmp/lt.iconset
  for sz in 16 32 64 128 256 512; do
    sips -z $sz $sz icon/icon.png --out "/tmp/lt.iconset/icon_${sz}x${sz}.png" >/dev/null
    sips -z $((sz*2)) $((sz*2)) icon/icon.png --out "/tmp/lt.iconset/icon_${sz}x${sz}@2x.png" >/dev/null
  done
  iconutil -c icns /tmp/lt.iconset -o "$APP/Contents/Resources/AppIcon.icns"
else
  echo "  (nessuna icona: si usa quella generica)"
fi

# firma ad-hoc: senza, macOS blocca l'accesso al microfono
codesign --force --deep --sign - "$APP" 2>/dev/null || true

echo "fatto: $APP"
