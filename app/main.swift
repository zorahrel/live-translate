import ApplicationServices
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
        // performDrag non fa niente se la finestra non e' spostabile: e' il
        // default, ma da questo dipende tutto il trascinamento
        window.isMovable = true
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
        // misurata su root, non su `rect`: `rect` e' la dimensione di partenza,
        // ma assegnare root a contentView lo ha gia' ridimensionato alla
        // finestra vera, che setFrameAutosaveName ripristina com'era l'ultima
        // volta. Con `rect` la striscia nasceva a 26px dal bordo di una
        // finestra 1020x480 che non esiste piu': su una finestra 1193x738
        // restava a mezz'aria in mezzo alla pagina, e in alto non c'era niente
        // da afferrare. L'autoresizing la tiene su solo DOPO, non la rimette a
        // posto se parte sbagliata.
        let bar = DragBar(frame: NSRect(x: 0, y: root.bounds.height - dragHeight,
                                        width: root.bounds.width, height: dragHeight))
        bar.autoresizingMask = [.width, .minYMargin]
        root.addSubview(bar, positioned: .above, relativeTo: web)
        // autodiagnosi: LT_SELFTEST=1 stampa lo stato della striscia e esce.
        // Prova tutto quello che si puo' provare senza toccare il mouse: che la
        // striscia esista, che raccolga il click, che la finestra sia
        // spostabile. Il gesto vero - premere e trascinare - sta in
        // LT_DRAGTEST=1 piu' sotto, perche' chiede un permesso di sistema.
        if ProcessInfo.processInfo.environment["LT_SELFTEST"] == "1" {
            // sondato sulla finestra vera, non su `rect`: cercando la striscia
            // alle coordinate con cui l'avevamo messa, la si trova sempre -
            // anche quando e' finita in mezzo alla pagina e in alto non c'e'
            // niente. E' l'errore che ha lasciato passare la striscia orfana:
            // la prova e il difetto condividevano la costante.
            let hit = root.hitTest(NSPoint(x: root.bounds.midX,
                                           y: root.bounds.height - dragHeight / 2))
            let below = root.hitTest(NSPoint(x: root.bounds.midX, y: 50))
            // e larga quanto la finestra: agli angoli si trascina come al centro
            let piena = abs(bar.frame.width - root.bounds.width) < 1
                && abs(bar.frame.maxY - root.bounds.height) < 1
            var ok = root.subviews.contains(bar) && hit === bar && below !== bar
                && bar.mouseDownCanMoveWindow && piena
            print("striscia trovata nella gerarchia : \(root.subviews.contains(bar))")
            print("click in alto colpisce la striscia: \(hit === bar)")
            print("striscia larga quanto la finestra: \(piena)")
            print("click sul testo la evita         : \(below !== bar)")
            print("puo' muovere la finestra         : \(bar.mouseDownCanMoveWindow)")
            print("finestra spostabile dallo sfondo : \(window.isMovableByWindowBackground)")
            print("finestra spostabile              : \(window.isMovable)")
            if !window.isMovable { ok = false }
            // spostarla davvero: performDrag muove la finestra alla stessa
            // maniera, quindi se questo non attecchisce non attecchira'
            // nemmeno il trascinamento con il mouse
            let start = window.frame.origin
            window.setFrameOrigin(NSPoint(x: start.x + 120, y: start.y + 60))
            let moved = window.frame.origin
            let spostata = abs(moved.x - start.x - 120) < 1 && abs(moved.y - start.y - 60) < 1
            window.setFrameOrigin(start)
            let tornata = abs(window.frame.origin.x - start.x) < 1
            print("si sposta davvero                : \(spostata)")
            print("e torna dove stava               : \(tornata)")
            if !spostata || !tornata { ok = false }
            // i semafori stanno nel themeFrame, sopra il contentView: la
            // striscia si sovrappone a loro ma il click gli arriva comunque
            // prima. Va verificato, non dato per scontato.
            for (name, kind) in [("chiudi", NSWindow.ButtonType.closeButton),
                                 ("minimizza", .miniaturizeButton),
                                 ("ingrandisci", .zoomButton)] {
                guard let b = window.standardWindowButton(kind) else { ok = false; continue }
                let c = b.convert(NSPoint(x: b.bounds.midX, y: b.bounds.midY), to: nil)
                let got = window.contentView?.superview?.hitTest(c) === b
                print("semaforo '\(name)' resta cliccabile : \(got)")
                if !got { ok = false }
            }
            // il badge deve dire la modalita': si provano i due versi
            setBadge(bidi: false, src: "pt", dst: "it")
            let uni = NSApp.dockTile.badgeLabel ?? "nil"
            setBadge(bidi: true, src: "pt", dst: "it")
            let bi = NSApp.dockTile.badgeLabel ?? "nil"
            print("badge in un verso                : \(uni)")
            print("badge in conversazione           : \(bi)")
            if uni != "pt→it" || bi != "pt⇄it" { ok = false }
            // la finestra deve finire sullo schermo che stai guardando: con
            // due monitor era finita su quello sbagliato e non si trovava
            placeOnActiveScreen()
            let main = NSScreen.main ?? NSScreen.screens[0]
            let onMain = main.visibleFrame.intersects(window.frame)
            let visible = NSScreen.screens.contains { $0.visibleFrame.intersects(window.frame) }
            print("finestra sullo schermo principale: \(onMain)")
            print("finestra su uno schermo visibile : \(visible)")
            if !onMain || !visible { ok = false }
            exit(ok ? 0 : 1)
        }

        // niente controlli nella titlebar: si accavallano con i semafori e con
        // i comandi di gestione finestre di macOS. Pin e opacita' stanno nella
        // pagina, che li richiama via postMessage.
        applyPin()
        placeOnActiveScreen()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        // il gesto vero, non il suo prerequisito: vedi dragTest()
        if ProcessInfo.processInfo.environment["LT_DRAGTEST"] == "1" {
            dragTest()
            return
        }
        load()
        buildMenu()
    }

    /// Preme sulla striscia, trascina, rilascia - con eventi veri, e guarda se
    /// la finestra ha seguito il puntatore.
    ///
    /// L'autodiagnosi qui sopra prova tutto tranne l'ultimo anello: sposta la
    /// finestra con `setFrameOrigin`, che e' proprio la strada che `performDrag`
    /// NON percorre. Se il trascinamento si rompesse dentro `performDrag`
    /// resterebbe verde. L'unico modo di provarlo e' postare eventi di mouse
    /// veri e vedere il frame cambiare da solo.
    ///
    /// Il prezzo e' un permesso: da macOS Mojave, postare eventi sintetici
    /// richiede che il processo sia in Impostazioni di Sistema > Privacy e
    /// sicurezza > Accessibilita'. Senza, `CGEvent.post` viene ingoiato in
    /// silenzio e il test passerebbe... fallendo. Quindi si controlla prima, e
    /// se il permesso manca il test si dichiara SALTATO con codice 2: saltato
    /// non e' verde, e verify.py lo stampa diverso da un successo.
    ///
    /// Gira su un thread di sfondo perche' `performDrag` si prende il thread
    /// principale in un suo ciclo di eventi finche' non arriva il rilascio: se
    /// postassimo da li', gli eventi non li leggerebbe nessuno.
    func dragTest() {
        // lanciata con `open` l'app non ha uno stdout da leggere: l'esito va
        // anche su file, e chi ha lanciato lo raccoglie di li'
        func esito(_ code: Int32, _ righe: [String]) -> Never {
            for r in righe { print(r) }
            if let p = ProcessInfo.processInfo.environment["LT_DRAGTEST_OUT"] {
                let stato = code == 0 ? "ok" : (code == 2 ? "saltato" : "rotto")
                let j = try? JSONSerialization.data(
                    withJSONObject: ["stato": stato, "righe": righe])
                try? j?.write(to: URL(fileURLWithPath: p))
            }
            exit(code)
        }
        guard AXIsProcessTrusted() else {
            esito(2, ["SALTATO: manca il permesso di Accessibilita'",
                      "  Impostazioni di Sistema > Privacy e sicurezza > "
                      + "Accessibilita' > aggiungi LiveTranslate.app"])
        }
        // al centro dello schermo: vicino a un bordo il window server limita lo
        // spostamento e il test fallirebbe per la geometria, non per il drag
        let vis = (NSScreen.main ?? NSScreen.screens[0]).visibleFrame
        window.setFrameOrigin(NSPoint(x: vis.midX - window.frame.width / 2,
                                      y: vis.midY - window.frame.height / 2))

        DispatchQueue.global().asyncAfter(deadline: .now() + 1.2) {
            var start = NSPoint.zero
            var bar = NSPoint.zero
            DispatchQueue.main.sync {
                start = self.window.frame.origin
                // il punto da premere: meta' della striscia, lontano dai
                // semafori a sinistra
                bar = NSPoint(x: self.window.frame.midX,
                              y: self.window.frame.maxY - dragHeight / 2)
            }
            // Cocoa conta la y dal basso, CoreGraphics dall'alto dello schermo
            // principale: senza questa conversione si preme fuori dalla finestra
            let flip = NSScreen.screens[0].frame.height
            let from = CGPoint(x: bar.x, y: flip - bar.y)
            let dx: CGFloat = 90, dy: CGFloat = 45   // dy in giu' sullo schermo

            func post(_ type: CGEventType, _ p: CGPoint) {
                let e = CGEvent(mouseEventSource: nil, mouseType: type,
                                mouseCursorPosition: p, mouseButton: .left)
                e?.post(tap: .cghidEventTap)
            }
            post(.mouseMoved, from)
            usleep(120_000)
            post(.leftMouseDown, from)
            usleep(120_000)
            // a passi: un solo salto puo' essere scartato come rumore, e il
            // ciclo di trascinamento vuole vedere il movimento continuo
            for i in 1...6 {
                let f = CGFloat(i) / 6
                post(.leftMouseDragged, CGPoint(x: from.x + dx * f, y: from.y + dy * f))
                usleep(40_000)
            }
            // l'ultimo spostamento va ribadito e lasciato assestare: postato a
            // ridosso del rilascio veniva accorpato, e la finestra si fermava
            // un passo indietro. Non e' il trascinamento che perde colpi, e'
            // questa prova che parla troppo in fretta - un dito vero si ferma
            // prima di alzarsi.
            let fine = CGPoint(x: from.x + dx, y: from.y + dy)
            post(.leftMouseDragged, fine)
            usleep(250_000)
            post(.leftMouseUp, fine)
            usleep(300_000)

            var end = NSPoint.zero
            DispatchQueue.main.sync { end = self.window.frame.origin }
            let gotX = end.x - start.x
            let gotY = end.y - start.y          // in Cocoa scende = negativo
            // tolleranza larga: il window server aggancia di qualche pixel e
            // qui interessa che la finestra abbia seguito, non l'aritmetica
            let ok = abs(gotX - dx) <= 4 && abs(gotY + dy) <= 4
            DispatchQueue.main.sync { self.window.setFrameOrigin(start) }
            esito(ok ? 0 : 1,
                  ["trascinata dalla striscia        : \(ok)",
                   "  chiesto dx=\(Int(dx)) dy=\(Int(-dy)), "
                   + "ottenuto dx=\(Int(gotX)) dy=\(Int(gotY))"])
        }
    }

    /// All'avvio la finestra va sullo schermo che ha il menu, cioe' quello che
    /// stai guardando. Prima seguiva il puntatore: con due monitor bastava
    /// avere il mouse sull'altro schermo per non vederla comparire.
    func placeOnActiveScreen() {
        place(on: NSScreen.main ?? NSScreen.screens[0])
    }

    /// Riporta la finestra sullo schermo che ha il menu, cioe' quello che stai
    /// guardando. Con due monitor la finestra finiva dove stava il puntatore e
    /// da li' non si trovava piu': serve un modo di richiamarla senza cercarla.
    @objc func recallWindow() {
        place(on: NSScreen.main ?? NSScreen.screens[0])
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func place(on screen: NSScreen) {
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
        appMenu.addItem(withTitle: "Portala qui",
                        action: #selector(recallWindow), keyEquivalent: "j")
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

    /// Click sull'icona nel Dock quando non ci sono finestre visibili: con due
    /// monitor la finestra puo' essere finita su quello che non stai
    /// guardando, e cliccare l'icona e' il gesto con cui la si cerca.
    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows f: Bool) -> Bool {
        recallWindow()
        return true
    }

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
