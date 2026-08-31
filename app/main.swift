import Cocoa
import WebKit

// Finestra nativa che ospita l'overlay servito da live_translate.py.
// Esiste per tre cose che un wrapper --app di Chrome non da':
// icona propria nel Dock, "sempre in primo piano", e sfondo trasparente.

let PORT = ProcessInfo.processInfo.environment["LT_PORT"] ?? "8777"

final class Delegate: NSObject, NSApplicationDelegate, WKNavigationDelegate,
                      WKScriptMessageHandler {
    var window: NSWindow!
    var web: WKWebView!
    var pinned = true

    func applicationDidFinishLaunching(_ n: Notification) {
        let cfg = WKWebViewConfiguration()
        let ctl = WKUserContentController()
        ctl.add(self, name: "host")
        cfg.userContentController = ctl
        cfg.preferences.setValue(true, forKey: "developerExtrasEnabled")
        // il VU meter usa getUserMedia: senza questo la richiesta viene negata in silenzio
        cfg.preferences.setValue(true, forKey: "mediaDevicesEnabled")
        cfg.preferences.setValue(true, forKey: "mediaStreamEnabled")

        let rect = NSRect(x: 0, y: 0, width: 1020, height: 480)
        window = NSWindow(contentRect: rect,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable,
                                      .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "Live Translate"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.styleMask.insert(.fullSizeContentView)
        window.backgroundColor = NSColor(red: 0.027, green: 0.027, blue: 0.043, alpha: 1)
        window.isMovableByWindowBackground = true
        window.minSize = NSSize(width: 460, height: 220)
        window.setFrameAutosaveName("LiveTranslateWindow")

        web = WKWebView(frame: rect, configuration: cfg)
        web.navigationDelegate = self
        web.setValue(false, forKey: "drawsBackground")
        web.autoresizingMask = [.width, .height]
        window.contentView = web

        // niente controlli nella titlebar: si accavallano con i semafori e con
        // i comandi di gestione finestre di macOS. Pin e opacita' stanno nella
        // pagina, che li richiama via postMessage.
        applyPin()
        placeOnActiveScreen()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        load()
        buildMenu()
    }

    /// L'autosave puo' riportare la finestra su un monitor scollegato: con due
    /// display capitava a Y=-962, invisibile. Si riporta sempre in basso al
    /// centro dello schermo dove sta il puntatore.
    func placeOnActiveScreen() {
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) }
            ?? NSScreen.main ?? NSScreen.screens[0]
        let vf = screen.visibleFrame
        var f = window.frame
        f.size.width = min(f.width, vf.width - 60)
        f.origin.x = vf.minX + (vf.width - f.width) / 2
        f.origin.y = vf.minY + 70
        window.setFrame(f, display: true)
    }

    func load() {
        web.load(URLRequest(url: URL(string: "http://127.0.0.1:\(PORT)/")!))
    }

    func webView(_ w: WKWebView, didFinish n: WKNavigation!) {
        w.evaluateJavaScript("window.__native=true;window.__setPin&&window.__setPin(\(pinned))",
                             completionHandler: nil)
    }

    @objc func reload() { load() }

    @objc func togglePin() { pinned.toggle(); applyPin() }

    func userContentController(_ c: WKUserContentController,
                               didReceive m: WKScriptMessage) {
        guard let body = m.body as? [String: Any],
              let cmd = body["cmd"] as? String else { return }
        switch cmd {
        case "pin":
            pinned = (body["on"] as? Bool) ?? !pinned
            applyPin()
        case "alpha":
            if let v = body["v"] as? Double { window.alphaValue = CGFloat(v) }
        case "reload":
            load()
        case "quit":
            NSApp.terminate(nil)
        default: break
        }
    }

    func applyPin() {
        window.level = pinned ? .floating : .normal
        window.collectionBehavior = pinned
            ? [.canJoinAllSpaces, .fullScreenAuxiliary]
            : [.fullScreenPrimary]
        // la pagina disegna lo stato del pin: qui si notifica soltanto
        web?.evaluateJavaScript("window.__setPin&&window.__setPin(\(pinned))",
                                completionHandler: nil)
    }

    @objc func setAlpha(_ s: NSSlider) { window.alphaValue = CGFloat(s.doubleValue) }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Sempre in primo piano",
                        action: #selector(togglePin), keyEquivalent: "p")
        appMenu.addItem(withTitle: "Ricarica", action: #selector(reload), keyEquivalent: "r")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Nascondi", action: #selector(NSApp.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Esci", action: #selector(NSApp.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        NSApp.mainMenu = main
    }

    // il server puo' non essere ancora su all'avvio: si riprova
    func webView(_ w: WKWebView, didFail n: WKNavigation!, withError e: Error) { retry() }
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) {
        retry()
    }
    func retry() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.7) { [weak self] in self?.load() }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }
}

let app = NSApplication.shared
let d = Delegate()
app.delegate = d
app.setActivationPolicy(.regular)   // icona nel Dock
app.run()
