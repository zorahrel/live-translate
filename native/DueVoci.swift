// Due Voci — sottotitoli tradotti per una conversazione fra due persone che
// parlano lingue diverse, allo stesso tavolo e nello stesso microfono.
//
// Perche' esiste, in una riga: le app che fanno questo traducono in UNA
// direzione, perche' il caso normale e' "guardo un video straniero". Due
// persone che si alternano sono un caso diverso, e chi lo risolve lo fa
// mandando l'audio in cloud a pagamento.
//
// Chi ha parlato lo decide `Router.swift`, condiviso col banco di misura.
//
// Tutto in locale: i modelli sono quelli di macOS, stanno fuori dal processo e
// girano sul Neural Engine. Misurato: 16 MB di RAM contro i 547 MB del modello
// whisper che questo progetto usava prima.

import SwiftUI
import Speech
import AVFoundation
import Translation

// ---------------------------------------------------------------- il verdetto

/// Una frase riconosciuta, gia' attribuita a una delle due lingue.
struct Battuta: Identifiable, Equatable {
    let id = UUID()
    let lingua: String          // "it" | "pt"
    let originale: String
    var tradotta: String = ""
    let quando = Date()
    /// quanto e' stata netta la decisione: 0 = pareggio, alto = nessun dubbio
    let margine: Int
}

// ------------------------------------------------------------------ il motore

@MainActor
@Observable
final class Motore {
    var battute: [Battuta] = []
    var inAscolto = false
    var stato = "fermo"
    var daTradurre: [Battuta] = []

    /// Quello che i due trascrittori stanno capendo ADESSO, mentre si parla.
    /// E' la risposta a "sta funzionando?": si vede il testo crescere prima
    /// che la frase sia finita.
    var vive: [String: String] = [:]
    /// Chi dei due sta vincendo su quello che ha sentito finora.
    var capofila: String?
    /// Quanto forte entra il microfono, 0…1. E' l'unico segno di vita quando
    /// nessuno ha ancora detto una parola riconoscibile.
    var livello: Float = 0

    private let engine = AVAudioEngine()
    private var analyzers: [String: SpeechAnalyzer] = [:]
    private var transcribers: [String: SpeechTranscriber] = [:]
    private var conts: [String: AsyncStream<AnalyzerInput>.Continuation] = [:]
    private var tasks: [Task<Void, Never>] = []

    /// I due testi definitivi della frase in corso. Non arrivano insieme:
    /// misurati, fino a 2 secondi di scarto.
    private var finali: [String: String] = [:]
    private var attesaChiusura: Task<Void, Never>?

    static let lingue = ["it": "it-IT", "pt": "pt-BR"]

    nonisolated static func installaModelli(_ t: [SpeechTranscriber]) async throws {
        if let req = try await AssetInventory.assetInstallationRequest(supporting: t) {
            try await req.downloadAndInstall()
        }
    }

    func avvia() async {
        guard !inAscolto else { return }
        stato = "preparo i due trascrittori…"

        for (breve, locale) in Self.lingue {
            // `.fastResults` e' la differenza fra un'app che sembra rotta e
            // una che risponde: senza, il primo testo non arriva mai mentre
            // parli e il definitivo tarda 7 secondi. Con, 0,9 s e 3,8 s.
            // `.volatileResults` da solo non basta — misurato, non dedotto.
            let t = SpeechTranscriber(locale: Locale(identifier: locale),
                                      transcriptionOptions: [],
                                      reportingOptions: [.volatileResults, .fastResults],
                                      attributeOptions: [])
            transcribers[breve] = t
            analyzers[breve] = SpeechAnalyzer(modules: [t])

            let task = Task { [weak self] in
                do {
                    for try await r in t.results {
                        let s = String(r.text.characters)
                            .trimmingCharacters(in: .whitespacesAndNewlines)
                        guard !s.isEmpty else { continue }
                        if r.isFinal { await self?.definitivo(lingua: breve, testo: s) }
                        else { await self?.provvisorio(lingua: breve, testo: s) }
                    }
                } catch { }
            }
            tasks.append(task)
        }

        // I modelli vanno chiesti al sistema anche quando `installedLocales`
        // li elenca gia': senza, i trascrittori partono, non danno errore e
        // non producono niente. Un'ora buttata per scoprirlo.
        stato = "controllo i modelli di lingua…"
        do {
            // fuori dal thread principale: da dentro, `downloadAndInstall()`
            // non torna mai — l'app resta ferma su questa riga per sempre.
            // Nel binario da terminale, che non e' isolato sul main, la stessa
            // chiamata dura 0,2 s: e' un blocco, non una lentezza.
            try await Self.installaModelli(Array(transcribers.values))
        } catch {
            stato = "modelli non disponibili: \(error.localizedDescription)"; return
        }

        // il motore vuole 16 kHz interi con segno; il microfono da' 48 kHz in
        // virgola mobile, e senza conversione il framework abortisce
        guard let fmtASR = await SpeechAnalyzer.bestAvailableAudioFormat(
                compatibleWith: Array(transcribers.values)) else {
            stato = "nessun formato audio compatibile"; return
        }
        let input = engine.inputNode
        let fmtMic = input.outputFormat(forBus: 0)
        guard let conv = AVAudioConverter(from: fmtMic, to: fmtASR) else {
            stato = "conversione audio impossibile"; return
        }

        for (breve, _) in Self.lingue {
            let (st, cont) = AsyncStream<AnalyzerInput>.makeStream()
            conts[breve] = cont
            do { try await analyzers[breve]!.start(inputSequence: st) }
            catch { stato = "avvio fallito (\(breve)): \(error)"; return }
        }

        let conti = conts
        var contaBuffer = 0
        input.installTap(onBus: 0, bufferSize: 4096, format: fmtMic) { [weak self] buf, _ in
            let cap = AVAudioFrameCount(
                Double(buf.frameLength) * fmtASR.sampleRate / fmtMic.sampleRate) + 1024
            guard let out = AVAudioPCMBuffer(pcmFormat: fmtASR, frameCapacity: cap)
            else { return }
            var dato = false
            var err: NSError?
            conv.convert(to: out, error: &err) { _, status in
                if dato { status.pointee = .noDataNow; return nil }
                dato = true; status.pointee = .haveData; return buf
            }
            guard err == nil, out.frameLength > 0 else { return }
            for (_, c) in conti { c.yield(AnalyzerInput(buffer: out)) }

            // il VU meter, calcolato sul buffer gia' convertito
            contaBuffer += 1
            guard contaBuffer % 2 == 0,
                  let dati = out.int16ChannelData?[0] else { return }
            var somma: Double = 0
            let n = Int(out.frameLength)
            for i in stride(from: 0, to: n, by: 8) {
                let v = Double(dati[i]) / 32768.0
                somma += v * v
            }
            let rms = (somma / Double(max(1, n / 8))).squareRoot()
            Task { @MainActor [weak self] in
                guard let self else { return }
                // scala logaritmica: il parlato normale sta molto in basso
                let db = 20 * log10(max(rms, 1e-5))
                let v = Float(max(0, min(1, (db + 55) / 45)))
                self.livello = self.livello * 0.6 + v * 0.4
            }
        }

        // Prova senza parlare: `DV_PROVA=/percorso/frase.wav` da' in pasto un
        // file invece del microfono, alla velocita' con cui sarebbe stato
        // pronunciato. Serve a vedere se l'app funziona prima di sedersi a
        // tavola con qualcuno, e a me e' servita per fotografare la striscia
        // in diretta senza far uscire un suono dagli altoparlanti.
        if let prova = ProcessInfo.processInfo.environment["DV_PROVA"] {
            input.removeTap(onBus: 0)
            inAscolto = true
            stato = "prova da file — nessun microfono"
            Task { [weak self] in await self?.daFile(prova.split(separator: ":").map(String.init),
                                                     formato: fmtASR) }
            return
        }

        do { try engine.start() } catch {
            stato = "microfono non disponibile: \(error.localizedDescription)"; return
        }
        inAscolto = true
        stato = "in ascolto — parlate pure"
    }

    /// Riproduce i file verso i trascrittori al ritmo del parlato. Alimentare
    /// piu' in fretta falserebbe tutto: il motore ragiona sul tempo dell'audio.
    private func daFile(_ percorsi: [String], formato fmt: AVAudioFormat) async {
        let perPezzo = AVAudioFrameCount(fmt.sampleRate / 10)
        let bpf = Int(fmt.streamDescription.pointee.mBytesPerFrame)
        let tZero = Date(); var scadenza: TimeInterval = 0
        func manda(_ p: AVAudioPCMBuffer) async {
            for (_, c) in conts { c.yield(AnalyzerInput(buffer: p)) }
            livello = p.int16ChannelData.map { d in
                var s: Double = 0
                let n = Int(p.frameLength)
                for i in stride(from: 0, to: n, by: 8) { let v = Double(d[0][i]) / 32768; s += v*v }
                let db = 20 * log10(max((s / Double(max(1, n/8))).squareRoot(), 1e-5))
                return Float(max(0, min(1, (db + 55) / 45)))
            } ?? 0
            scadenza += 0.1
            let attesa = scadenza - Date().timeIntervalSince(tZero)
            if attesa > 0 { try? await Task.sleep(for: .seconds(attesa)) }
        }
        func fruscio(_ decimi: Int) async {
            for _ in 0..<decimi {
                guard let m = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: perPezzo) else { return }
                m.frameLength = perPezzo
                let p = m.audioBufferList.pointee.mBuffers.mData!
                    .bindMemory(to: Int16.self, capacity: Int(perPezzo))
                for i in 0..<Int(perPezzo) { p[i] = Int16.random(in: -24...24) }
                await manda(m)
            }
        }
        await fruscio(15)
        for percorso in percorsi {
            guard let f = try? AVAudioFile(forReading: URL(fileURLWithPath: percorso)),
                  let inBuf = AVAudioPCMBuffer(pcmFormat: f.processingFormat,
                                               frameCapacity: AVAudioFrameCount(f.length)),
                  (try? f.read(into: inBuf)) != nil,
                  let conv = AVAudioConverter(from: f.processingFormat, to: fmt),
                  let out = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity:
                        AVAudioFrameCount(Double(inBuf.frameLength) * fmt.sampleRate
                                          / f.processingFormat.sampleRate) + 4096)
            else { stato = "file illeggibile: \(percorso)"; return }
            var dato = false; var err: NSError?
            conv.convert(to: out, error: &err) { _, st in
                if dato { st.pointee = .noDataNow; return nil }
                dato = true; st.pointee = .haveData; return inBuf
            }
            var off: AVAudioFrameCount = 0
            while off < out.frameLength {
                let n = min(perPezzo, out.frameLength - off)
                guard let pezzo = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: n) else { break }
                pezzo.frameLength = n
                memcpy(pezzo.audioBufferList.pointee.mBuffers.mData!,
                       out.audioBufferList.pointee.mBuffers.mData!.advanced(by: Int(off) * bpf),
                       Int(n) * bpf)
                await manda(pezzo)
                off += n
            }
            await fruscio(45)
        }
        stato = "prova finita"
        livello = 0
    }

    func ferma() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        for (_, c) in conts { c.finish() }
        conts.removeAll()
        for t in tasks { t.cancel() }
        tasks.removeAll()
        analyzers.removeAll(); transcribers.removeAll()
        attesaChiusura?.cancel()
        vive.removeAll(); finali.removeAll(); capofila = nil
        livello = 0
        inAscolto = false
        stato = "fermo"
    }

    // ------------------------------------------------------------- il flusso

    /// Testo provvisorio: cresce parola per parola mentre si parla. Serve a
    /// due cose — farlo vedere, e sapere in anticipo chi dei due sta vincendo.
    private func provvisorio(lingua: String, testo: String) {
        vive[lingua] = testo
        let it = vive["it"] ?? "", pt = vive["pt"] ?? ""
        if !it.isEmpty || !pt.isEmpty {
            capofila = Router.decidi(it: it, pt: pt).lingua
        }
    }

    /// Testo definitivo di uno dei due. L'altro arriva dopo, da 30 ms a 2
    /// secondi piu' tardi: aspettarlo sempre costerebbe quei 2 secondi a ogni
    /// frase, non aspettarlo mai vorrebbe dire decidere con meta' delle prove.
    /// Se a chiudere e' il trascrittore che gia' guidava sui provvisori, la
    /// prova c'e' gia' e si pubblica subito.
    private func definitivo(lingua: String, testo: String) {
        finali[lingua] = testo
        attesaChiusura?.cancel()

        if finali.count == Self.lingue.count || lingua == capofila {
            chiudi(); return
        }
        attesaChiusura = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(1200))
            guard !Task.isCancelled else { return }
            await self?.chiudi()
        }
    }

    private func chiudi() {
        attesaChiusura?.cancel()
        // per chi non ha ancora chiuso vale l'ultimo provvisorio: e' quello
        // che ha capito, anche se non l'ha ancora dichiarato definitivo
        let it = finali["it"] ?? vive["it"] ?? ""
        let pt = finali["pt"] ?? vive["pt"] ?? ""
        finali.removeAll(); vive.removeAll(); capofila = nil
        guard !(it.isEmpty && pt.isEmpty) else { return }

        let v = Router.decidi(it: it, pt: pt)
        guard !v.testo.isEmpty else { return }
        let b = Battuta(lingua: v.lingua, originale: v.testo, margine: v.margine)
        battute.append(b)
        daTradurre.append(b)
        if battute.count > 200 { battute.removeFirst(battute.count - 200) }
    }

    func applicaTraduzione(id: UUID, testo: String) {
        if let i = battute.firstIndex(where: { $0.id == id }) {
            battute[i].tradotta = testo
        }
    }
}

// -------------------------------------------------------------------- la vista

struct Targhetta: View {
    let lingua: String
    var acceso = true
    var body: some View {
        Text(lingua == "it" ? "IT" : "PT")
            .font(.system(size: 10, weight: .bold, design: .rounded))
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background((lingua == "it" ? Color.blue : Color.green)
                .opacity(acceso ? 0.85 : 0.25))
            .foregroundStyle(acceso ? .white : .secondary)
            .clipShape(Capsule())
    }
}

struct Riga: View {
    let b: Battuta
    var body: some View {
        let daIt = b.lingua == "it"
        VStack(alignment: daIt ? .leading : .trailing, spacing: 3) {
            HStack(spacing: 6) {
                Targhetta(lingua: b.lingua)
                if b.margine == 0 {
                    // onesta' a schermo: qui la decisione e' stata in bilico
                    Image(systemName: "questionmark.circle")
                        .font(.system(size: 10)).foregroundStyle(.orange)
                        .help("lingua decisa per un soffio")
                }
            }
            Text(b.tradotta.isEmpty ? "…" : b.tradotta)
                .font(.system(size: 21, weight: .medium))
                .foregroundStyle(.primary)
            Text(b.originale)
                .font(.system(size: 13))
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: daIt ? .leading : .trailing)
        .padding(.vertical, 5)
    }
}

/// Il livello del microfono. E' il feedback che regge quando non e' ancora
/// stata detta nessuna parola riconoscibile: se queste barrette si muovono, il
/// microfono sente; se stanno ferme, il problema e' prima della lingua.
struct Vu: View {
    let livello: Float
    var body: some View {
        HStack(spacing: 2) {
            ForEach(0..<5, id: \.self) { i in
                let soglia = Float(i) / 5
                RoundedRectangle(cornerRadius: 1)
                    .fill(livello > soglia ? Color.accentColor : Color.secondary.opacity(0.22))
                    .frame(width: 3, height: 5 + CGFloat(i) * 2.5)
            }
        }
        .frame(height: 16, alignment: .bottom)
        .animation(.linear(duration: 0.1), value: livello)
    }
}

/// La striscia in diretta: quello che i DUE trascrittori stanno capendo in
/// questo momento, uno sopra l'altro. Chi guida e' acceso, l'altro spento.
/// Si vede chi sta parlando mentre parla, senza aspettare la frase finita.
struct InDiretta: View {
    let vive: [String: String]
    let capofila: String?
    let livello: Float

    var body: some View {
        VStack(spacing: 4) {
            ForEach(["it", "pt"], id: \.self) { lg in
                let testo = vive[lg] ?? ""
                let guida = capofila == lg && !testo.isEmpty
                HStack(alignment: .top, spacing: 7) {
                    Targhetta(lingua: lg, acceso: guida)
                    Text(testo.isEmpty ? "…" : testo)
                        .font(.system(size: guida ? 14 : 12,
                                      weight: guida ? .medium : .regular))
                        .foregroundStyle(guida ? .primary : .tertiary)
                        .lineLimit(2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .animation(nil, value: testo)
                    if guida { Vu(livello: livello) }
                }
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 8)
        .background(.quaternary.opacity(0.35))
    }
}

struct ContentView: View {
    @State private var motore = Motore()
    @State private var cfgItPt: TranslationSession.Configuration?
    @State private var cfgPtIt: TranslationSession.Configuration?

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Button(motore.inAscolto ? "Ferma" : "Ascolta") {
                    Task {
                        if motore.inAscolto { motore.ferma() } else { await motore.avvia() }
                    }
                }
                .keyboardShortcut(.space, modifiers: [])
                Text(motore.stato).font(.caption).foregroundStyle(.secondary)
                Spacer()
                Text("IT ⇄ PT").font(.caption.bold()).foregroundStyle(.secondary)
            }
            .padding(10)
            Divider()
            ScrollViewReader { p in
                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(motore.battute) { b in Riga(b: b).id(b.id) }
                    }.padding(.horizontal, 14).padding(.vertical, 8)
                }
                .onChange(of: motore.battute.count) {
                    if let u = motore.battute.last {
                        withAnimation { p.scrollTo(u.id, anchor: .bottom) }
                    }
                }
            }
            if motore.inAscolto {
                Divider()
                InDiretta(vive: motore.vive, capofila: motore.capofila,
                          livello: motore.livello)
            }
        }
        .frame(minWidth: 520, minHeight: 380)
        // le due sessioni si aprono all'avvio dell'app, non al primo "Ascolta":
        // la prima traduzione paga il caricamento del modello, e pagarlo
        // mentre nessuno ha ancora parlato non costa niente a nessuno
        .task {
            // in prova da file si parte da soli: non c'e' nessuno da aspettare
            if ProcessInfo.processInfo.environment["DV_PROVA"] != nil { await motore.avvia() }
            cfgItPt = .init(source: .init(identifier: "it"),
                            target: .init(identifier: "pt-BR"))
            cfgPtIt = .init(source: .init(identifier: "pt-BR"),
                            target: .init(identifier: "it"))
        }
        .translationTask(cfgItPt) { s in await svuota(s, da: "it") }
        .translationTask(cfgPtIt) { s in await svuota(s, da: "pt") }
    }

    private func svuota(_ s: TranslationSession, da lingua: String) async {
        try? await s.prepareTranslation()
        while !Task.isCancelled {
            let quelli = motore.daTradurre.filter { $0.lingua == lingua }
            if quelli.isEmpty {
                try? await Task.sleep(for: .milliseconds(120)); continue
            }
            motore.daTradurre.removeAll { b in quelli.contains { $0.id == b.id } }
            for b in quelli {
                if let r = try? await s.translate(b.originale) {
                    motore.applicaTraduzione(id: b.id, testo: r.targetText)
                }
            }
        }
    }
}

@main
struct DueVociApp: App {
    var body: some Scene {
        WindowGroup("Due Voci") { ContentView() }
            .defaultSize(width: 620, height: 460)
    }
}
