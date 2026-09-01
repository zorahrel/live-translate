import Cocoa
import WebKit

/// La WKWebView copre tutta la finestra e si prende ogni click, quindi
/// `isMovableByWindowBackground` non basta: il CSS `-webkit-app-region: drag`
/// che funziona in Electron qui viene ignorato, e la finestra restava
/// inchiodata. Questa striscia sta sopra la barra dei titoli, dove la pagina
/// non mette controlli, e trascina la finestra a mano.
final class DragBar: NSView {
    override func mouseDown(with e: NSEvent) { window?.performDrag(with: e) }
    override var mouseDownCanMoveWindow: Bool { true }
}

// Finestra nativa che ospita l'overlay servito da live_translate.py.
// Esiste per tre cose che un wrapper --app di Chrome non da':
// icona propria nel Dock, "sempre in primo piano", e sfondo trasparente.

let PORT = ProcessInfo.processInfo.environment["LT_PORT"] ?? "8777"
/// altezza della striscia trascinabile: la stessa di #drag nel CSS della pagina
let dragHeight: CGFloat = 26

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

        // WKWebView risponde ai click per conto suo e una sottovista non li
        // vedrebbe mai: la striscia deve essere sua sorella dentro un
        // contenitore, non sua figlia.
        let root = NSView(frame: rect)
        root.autoresizingMask = [.width, .height]
        root.addSubview(web)
        window.contentView = root

        // la striscia di trascinamento sta sopra il web: la pagina lascia
        // vuoti i primi 26px proprio per questo (vedi #drag nel CSS)
        let bar = DragBar(frame: NSRect(x: 0, y: rect.height - dragHeight,
                                        width: rect.width, height: dragHeight))
        bar.autoresizingMask = [.width, .minYMargin]
        root.addSubview(bar, positioned: .above, relativeTo: web)
        // autodiagnosi: LT_SELFTEST=1 stampa lo stato della striscia e esce,
        // perche' simulare un trascinamento vero richiede i permessi di
        // accessibilita' che una shell non ha
        if ProcessInfo.processInfo.environment["LT_SELFTEST"] == "1" {
            let hit = root.hitTest(NSPoint(x: rect.width / 2,
                                           y: rect.height - dragHeight / 2))
            let below = root.hitTest(NSPoint(x: rect.width / 2, y: 50))
            print("striscia trovata nella gerarchia : \(root.subviews.contains(bar))")
            print("click in alto colpisce la striscia: \(hit === bar)")
            print("click sul testo la evita         : \(below !== bar)")
            print("puo' muovere la finestra         : \(bar.mouseDownCanMoveWindow)")
            print("finestra spostabile dallo sfondo : \(window.isMovableByWindowBackground)")
            exit((root.subviews.contains(bar) && hit === bar && below !== bar
                  && bar.mouseDownCanMoveWindow) ? 0 : 1)
        }

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
        case "mode":
            // la pagina dice in che modalita' e' e con che lingue: il Dock lo
            // mostra, cosi' si sa cosa sta facendo senza aprire la finestra
            setBadge(bidi: (body["bidi"] as? Bool) ?? false,
                     src: (body["src"] as? String) ?? "",
                     dst: (body["dst"] as? String) ?? "")
        case "quit":
            NSApp.terminate(nil)
        default: break
        }
    }

    /// Badge sull'icona del Dock: 'pt→it' in una direzione, 'pt⇄it' in
    /// conversazione. Senza, l'icona e' identica nelle due modalita' e non si
    /// capisce se il bidirezionale e' acceso.
    func setBadge(bidi: Bool, src: String, dst: String) {
        let label: String
        if src.isEmpty || dst.isEmpty {
            label = ""
        } else {
            label = bidi ? "\(src)⇄\(dst)" : "\(src)→\(dst)"
        }
        NSApp.dockTile.badgeLabel = label.isEmpty ? nil : label
        NSApp.dockTile.display()
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

    /// Chiudere la finestra deve spegnere anche il motore. Senza questo il
    /// backend lanciato con nohup dal launcher restava orfano: una volta e'
    /// rimasto 26 ore a trascrivere il microfono a vuoto, 720 MB e la ventola.
    /// La WKWebView muore con l'app, quindi la SSE cade e anche il watchdog
    /// lato server chiuderebbe: questo e' solo la strada rapida e pulita.
    func applicationWillTerminate(_ n: Notification) {
        guard let url = URL(string: "http://127.0.0.1:\(PORT)/quit") else { return }
        var r = URLRequest(url: url)
        r.httpMethod = "POST"
        r.timeoutInterval = 2
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: r) { _, _, _ in sem.signal() }.resume()
        _ = sem.wait(timeout: .now() + 2)
    }
}

let app = NSApplication.shared
let d = Delegate()
app.delegate = d
app.setActivationPolicy(.regular)   // icona nel Dock
app.run()
