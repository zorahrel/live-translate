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
// girano sul Neural Engine. Misurato mentre trascrive in due lingue: 37 MB di
// footprint per l'app, piu' 34 dei servizi di sistema — contro i ~700 MB che
// whisper teneva residenti nella versione precedente di questo progetto.

import SwiftUI
import Speech
import AVFoundation
import Translation

// ---------------------------------------------------------------- il verdetto

/// Una frase riconosciuta, gia' attribuita a una delle due lingue.
struct Battuta: Identifiable, Equatable {
    let id = UUID()
    let lingua: Lingua
    let originale: String
    var tradotta: String = ""
    let quando = Date()
    /// quanto e' stata netta la decisione: 0 = pareggio, alto = nessun dubbio
    let margine: Int
}

// ------------------------------------------------------------ la dimensione

/// Quanto grande il testo a schermo. Quattro passi, non uno slider: a tavola
/// non si regola un cursore, si preme una volta. L'ultimo passo serve a usarla
/// come sottotitolo da lontano.
enum Dimensione: Int, CaseIterable, Identifiable {
    case piccolo = 0, medio = 1, grande = 2, enorme = 3
    var id: Int { rawValue }
    var nome: String {
        switch self {
        case .piccolo: "Piccolo"
        case .medio:   "Medio"
        case .grande:  "Grande"
        case .enorme:  "Enorme"
        }
    }
    var scala: CGFloat {
        switch self {
        case .piccolo: 0.8
        case .medio:   1.0
        case .grande:  1.35
        case .enorme:  1.8
        }
    }
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
    /// che la frase sia finita. Chiave: l'id ASR della lingua.
    var vive: [String: String] = [:]
    /// Chi dei due sta vincendo su quello che ha sentito finora.
    var capofila: String?
    /// Quanto forte entra il microfono, 0…1. E' l'unico segno di vita quando
    /// nessuno ha ancora detto una parola riconoscibile.
    var livello: Float = 0

    /// Le due lingue con cui il motore e' partito. Cambiarle vuol dire
    /// spegnere e riaccendere: i trascrittori nascono con la loro lingua.
    private(set) var a = Lingua(asr: "it-IT", corr: "it")
    private(set) var b = Lingua(asr: "pt-BR", corr: "pt_BR")

    // `nonisolated(unsafe)`: il microfono si accende FUORI dal main apposta —
    // `engine.inputNode` fa un dispatch_sync interno, e se il thread principale
    // e' fermo dentro una catena async (un cambio di lingua parte da un
    // aggiornamento di vista) quel sync non torna piu'. Misurato: l'app
    // congelata su "controllo i modelli", stack in `__DISPATCH_WAIT_FOR_QUEUE__`.
    private nonisolated(unsafe) let engine = AVAudioEngine()
    /// il microfono e' acceso? in prova da file non si tocca proprio
    private var microfonoAcceso = false
    private var analyzers: [String: SpeechAnalyzer] = [:]
    private var transcribers: [String: SpeechTranscriber] = [:]
    private var conts: [String: AsyncStream<AnalyzerInput>.Continuation] = [:]
    private var tasks: [Task<Void, Never>] = []

    /// I due testi definitivi della frase in corso. Non arrivano insieme:
    /// misurati, fino a 2 secondi di scarto.
    private var finali: [String: String] = [:]
    private var attesaChiusura: Task<Void, Never>?
    /// I trascrittori che devono ancora chiudere una frase GIA' pubblicata.
    /// Il loro definitivo arriva in ritardo — misurato, fino a 2 secondi — e
    /// senza questo apre una riga nuova con quello che il perdente aveva
    /// capito: due righe per una frase sola, la seconda spazzatura nella
    /// lingua sbagliata. Il banco non poteva vederlo, perche' taglia la frase
    /// fra un file e l'altro; si e' visto in una schermata dell'app piena.
    private var attesiInRitardo: Set<String> = []

    nonisolated static func installaModelli(_ t: [SpeechTranscriber]) async throws {
        if let req = try await AssetInventory.assetInstallationRequest(supporting: t) {
            try await req.downloadAndInstall()
        }
    }

    func avvia(_ primaLingua: Lingua, _ secondaLingua: Lingua) async {
        guard !inAscolto else { return }
        a = primaLingua; b = secondaLingua
        stato = "preparo i due trascrittori…"

        for lg in [a, b] {
            // `.fastResults` e' la differenza fra un'app che sembra rotta e
            // una che risponde: senza, il primo testo non arriva mai mentre
            // parli e il definitivo tarda 7 secondi. Con, 0,9 s e 3,8 s.
            // `.volatileResults` da solo non basta — misurato, non dedotto.
            let t = SpeechTranscriber(locale: Locale(identifier: lg.asr),
                                      transcriptionOptions: [],
                                      reportingOptions: [.volatileResults, .fastResults],
                                      attributeOptions: [])
            transcribers[lg.id] = t
            analyzers[lg.id] = SpeechAnalyzer(modules: [t])

            let task = Task { [weak self] in
                do {
                    for try await r in t.results {
                        let s = String(r.text.characters)
                            .trimmingCharacters(in: .whitespacesAndNewlines)
                        guard !s.isEmpty else { continue }
                        if r.isFinal { await self?.definitivo(lingua: lg.id, testo: s) }
                        else { await self?.provvisorio(lingua: lg.id, testo: s) }
                    }
                } catch {
                    // un catch vuoto qui fa sembrare SILENZIOSO un motore che sta
                    // invece fallendo: e' il modo piu' veloce di passare un'ora a
                    // cercare un guasto nell'audio che sta nello stream
                    FileHandle.standardError.write(
                        Data("trascrittore \(lg.id) interrotto: \(error)\n".utf8))
                }
            }
            tasks.append(task)
        }

        // I modelli vanno chiesti al sistema anche quando `installedLocales`
        // li elenca gia': senza, i trascrittori partono, non danno errore e
        // non producono niente. Un'ora buttata per scoprirlo.
        let installate = await SpeechTranscriber.installedLocales.map { $0.identifier(.bcp47) }
        let daScaricare = [a, b].filter { !installate.contains($0.asr) }
        stato = daScaricare.isEmpty
            ? "controllo i modelli di lingua…"
            : "scarico \(daScaricare.map(\.nome).joined(separator: " e "))… (una volta sola)"
        do {
            // fuori dal thread principale: da dentro, `downloadAndInstall()`
            // non torna mai — l'app resta ferma su questa riga per sempre.
            // Nel binario da terminale, che non e' isolato sul main, la stessa
            // chiamata dura 0,2 s: e' un blocco, non una lentezza.
            try await Self.installaModelli(Array(transcribers.values))
        } catch {
            stato = "modelli non disponibili: \(error.localizedDescription)"
            svuotaTutto(); return
        }

        // il motore vuole 16 kHz interi con segno; il microfono da' 48 kHz in
        // virgola mobile, e senza conversione il framework abortisce
        guard let fmtASR = await SpeechAnalyzer.bestAvailableAudioFormat(
                compatibleWith: Array(transcribers.values)) else {
            stato = "nessun formato audio compatibile"; svuotaTutto(); return
        }

        for lg in [a, b] {
            let (st, cont) = AsyncStream<AnalyzerInput>.makeStream()
            conts[lg.id] = cont
            do { try await analyzers[lg.id]!.start(inputSequence: st) }
            catch { stato = "avvio fallito (\(lg.id)): \(error)"; svuotaTutto(); return }
        }

        // Prova senza parlare: `DV_PROVA=/percorso/frase.wav` da' in pasto un
        // file invece del microfono, alla velocita' con cui sarebbe stato
        // pronunciato. Serve a vedere se l'app funziona prima di sedersi a
        // tavola con qualcuno, e a fotografare la striscia in diretta senza
        // far uscire un suono dagli altoparlanti. Il microfono qui non si
        // accende affatto: accenderlo per spegnerlo subito era il modo di
        // pagare un deadlock per un'apparecchiatura che non serve.
        if let prova = ProcessInfo.processInfo.environment["DV_PROVA"] {
            inAscolto = true
            stato = "prova da file — nessun microfono"
            Task { [weak self] in await self?.daFile(prova.split(separator: ":").map(String.init),
                                                     formato: fmtASR) }
            return
        }

        if let guasto = await accendiMicrofono(fmtASR, conts) {
            stato = guasto; svuotaTutto(); return
        }
        microfonoAcceso = true
        inAscolto = true
        stato = "in ascolto — parlate pure"
    }

    /// Accende il microfono e ci attacca la presa. Gira FUORI dal main actor:
    /// e' l'unico punto che puo' bloccarsi, e bloccarsi sul main significa
    /// un'app congelata invece di un errore. Torna `nil` se e' andata bene,
    /// altrimenti il messaggio da mostrare.
    private nonisolated func accendiMicrofono(
        _ fmtASR: AVAudioFormat,
        _ conti: [String: AsyncStream<AnalyzerInput>.Continuation]
    ) async -> String? {
        let input = engine.inputNode
        let fmtMic = input.outputFormat(forBus: 0)
        guard let conv = AVAudioConverter(from: fmtMic, to: fmtASR) else {
            return "conversione audio impossibile"
        }
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
        do { try engine.start() } catch {
            input.removeTap(onBus: 0)
            return "microfono non disponibile: \(error.localizedDescription)"
        }
        return nil
    }

    private nonisolated func spegniMicrofono() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
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

    /// Smonta tutto quello che `avvia` ha costruito. Serve anche a meta'
    /// avvio: se l'avvio fallisce e i trascrittori restano appesi, il tentativo
    /// successivo ne crea altri due e l'audio finisce a quattro.
    private func svuotaTutto() {
        for (_, c) in conts { c.finish() }
        conts.removeAll()
        for t in tasks { t.cancel() }
        tasks.removeAll()
        analyzers.removeAll(); transcribers.removeAll()
        attesaChiusura?.cancel()
        vive.removeAll(); finali.removeAll(); capofila = nil
        attesiInRitardo.removeAll()
        livello = 0
    }

    func ferma() async {
        if microfonoAcceso {
            microfonoAcceso = false
            await Task.detached { [self] in spegniMicrofono() }.value
        }
        svuotaTutto()
        inAscolto = false
        stato = "fermo"
    }

    // ------------------------------------------------------------- il flusso

    /// Testo provvisorio: cresce parola per parola mentre si parla. Serve a
    /// due cose — farlo vedere, e sapere in anticipo chi dei due sta vincendo.
    private func provvisorio(lingua: String, testo: String) {
        // un parziale nuovo vuol dire una frase nuova: quello che deve ancora
        // arrivare da qui in poi non e' piu' il residuo di quella pubblicata
        attesiInRitardo.remove(lingua)
        vive[lingua] = testo
        let tA = vive[a.id] ?? "", tB = vive[b.id] ?? ""
        if !tA.isEmpty || !tB.isEmpty {
            capofila = Router.decidi(a, tA, b, tB).lingua.id
        }
    }

    /// Testo definitivo di uno dei due. L'altro arriva dopo, da 30 ms a 2
    /// secondi piu' tardi: aspettarlo sempre costerebbe quei 2 secondi a ogni
    /// frase, non aspettarlo mai vorrebbe dire decidere con meta' delle prove.
    /// Se a chiudere e' il trascrittore che gia' guidava sui provvisori, la
    /// prova c'e' gia' e si pubblica subito.
    private func definitivo(lingua: String, testo: String) {
        if attesiInRitardo.remove(lingua) != nil { return }
        finali[lingua] = testo
        attesaChiusura?.cancel()

        if finali.count == 2 || lingua == capofila {
            chiudi(); return
        }
        attesaChiusura = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(Politica.attesaAltroMs))
            guard !Task.isCancelled else { return }
            await self?.chiudi()
        }
    }

    private func chiudi() {
        attesaChiusura?.cancel()
        // per chi non ha ancora chiuso vale l'ultimo provvisorio: e' quello
        // che ha capito, anche se non l'ha ancora dichiarato definitivo
        let tA = finali[a.id] ?? vive[a.id] ?? ""
        let tB = finali[b.id] ?? vive[b.id] ?? ""
        attesiInRitardo = Set([a.id, b.id]).subtracting(finali.keys)
        finali.removeAll(); vive.removeAll(); capofila = nil
        guard !(tA.isEmpty && tB.isEmpty) else { return }

        let v = Router.decidi(a, tA, b, tB)
        guard !v.testo.isEmpty else { return }
        let btt = Battuta(lingua: v.lingua, originale: v.testo, margine: v.margine)
        battute.append(btt)
        daTradurre.append(btt)
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
    let sigla: String
    /// la prima lingua e' blu, la seconda verde: due voci, due colori
    let prima: Bool
    var acceso = true
    var scala: CGFloat = 1
    var body: some View {
        Text(sigla)
            .font(.system(size: 10 * min(scala, 1.4), weight: .bold, design: .rounded))
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background((prima ? Color.blue : Color.green).opacity(acceso ? 0.85 : 0.25))
            .foregroundStyle(acceso ? .white : .secondary)
            .clipShape(Capsule())
    }
}

struct Riga: View {
    let b: Battuta
    let sigla: String
    let prima: Bool
    let scala: CGFloat

    var body: some View {
        VStack(alignment: prima ? .leading : .trailing, spacing: 3 * scala) {
            HStack(spacing: 6) {
                Targhetta(sigla: sigla, prima: prima, scala: scala)
                if b.margine == 0 {
                    // onesta' a schermo: qui la decisione e' stata in bilico
                    Image(systemName: "questionmark.circle")
                        .font(.system(size: 10 * scala)).foregroundStyle(.orange)
                        .help("lingua decisa per un soffio")
                }
            }
            Text(b.tradotta.isEmpty ? "…" : b.tradotta)
                .font(.system(size: 21 * scala, weight: .medium))
                .foregroundStyle(.primary)
                .multilineTextAlignment(prima ? .leading : .trailing)
            Text(b.originale)
                .font(.system(size: 13 * scala))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(prima ? .leading : .trailing)
        }
        .frame(maxWidth: .infinity, alignment: prima ? .leading : .trailing)
        .padding(.vertical, 5 * scala)
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
    let lingue: [Lingua]
    let sigle: [String]
    let vive: [String: String]
    let capofila: String?
    let livello: Float
    let scala: CGFloat

    var body: some View {
        VStack(spacing: 4) {
            ForEach(Array(lingue.enumerated()), id: \.element.id) { i, lg in
                let testo = vive[lg.id] ?? ""
                let guida = capofila == lg.id && !testo.isEmpty
                HStack(alignment: .top, spacing: 7) {
                    Targhetta(sigla: sigle[i], prima: i == 0, acceso: guida, scala: scala)
                    Text(testo.isEmpty ? "…" : testo)
                        .font(.system(size: (guida ? 14 : 12) * scala,
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

/// Il menu di una delle due lingue. Le lingue il cui modello non e' ancora sul
/// Mac sono marcate: sceglierle costa un download, e saperlo prima e' meglio
/// che vedere l'app ferma su "scarico…" senza averlo chiesto.
struct SceltaLingua: View {
    @Binding var scelta: String
    let catalogo: [Lingua]
    let installate: Set<String>
    let altra: String

    var body: some View {
        Picker("", selection: $scelta) {
            ForEach(catalogo) { lg in
                Text(lg.nome + (installate.contains(lg.asr) ? "" : " ↓")
                     + (lg.asr == altra ? "  •" : ""))
                    .tag(lg.asr)
            }
        }
        .labelsHidden()
        .pickerStyle(.menu)
        .frame(maxWidth: 190)
    }
}

struct ContentView: View {
    @State private var motore = Motore()
    @State private var cfgAB: TranslationSession.Configuration?
    @State private var cfgBA: TranslationSession.Configuration?

    @State private var catalogo: [Lingua] = []
    @State private var installate: Set<String> = []
    @State private var avviso: String?

    @AppStorage("linguaA") private var idA = Catalogo.predefinite.0
    @AppStorage("linguaB") private var idB = Catalogo.predefinite.1
    @AppStorage("dimensione") private var dimGrezza = Dimensione.medio.rawValue

    private var dim: Dimensione { Dimensione(rawValue: dimGrezza) ?? .medio }
    private var a: Lingua { risolvi(idA, difetto: Catalogo.predefinite.0) }
    private var b: Lingua { risolvi(idB, difetto: Catalogo.predefinite.1) }

    /// Prima che il catalogo sia caricato la coppia va costruita lo stesso:
    /// il correttore risponde subito, e' l'elenco dell'ASR che e' asincrono.
    private func risolvi(_ id: String, difetto: String) -> Lingua {
        if let l = catalogo.first(where: { $0.id == id }) { return l }
        let disp = Set(NSSpellChecker.shared.availableLanguages)
        if let c = Catalogo.correttorePer(id, disponibili: disp) {
            return Lingua(asr: id, corr: c)
        }
        return Lingua(asr: difetto,
                      corr: Catalogo.correttorePer(difetto, disponibili: disp) ?? "en")
    }

    var body: some View {
        let (sA, sB) = sigle(a, b)
        VStack(spacing: 0) {
            barra(sA, sB)
            Divider()
            ScrollViewReader { p in
                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(motore.battute) { btt in
                            Riga(b: btt,
                                 sigla: btt.lingua.id == a.id ? sA : sB,
                                 prima: btt.lingua.id == a.id,
                                 scala: dim.scala)
                                .id(btt.id)
                        }
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
                InDiretta(lingue: [motore.a, motore.b], sigle: [sA, sB],
                          vive: motore.vive, capofila: motore.capofila,
                          livello: motore.livello, scala: dim.scala)
            }
        }
        .frame(minWidth: 620, minHeight: 380)
        .task { await preparaLingue() }
        // cambiare lingua vuol dire rifare i trascrittori: nascono con la loro
        .onChange(of: idA) { Task { await cambiaCoppia() } }
        .onChange(of: idB) { Task { await cambiaCoppia() } }
        .translationTask(cfgAB) { s in await svuota(s, da: a.id) }
        .translationTask(cfgBA) { s in await svuota(s, da: b.id) }
    }

    @ViewBuilder
    private func barra(_ sA: String, _ sB: String) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Button(motore.inAscolto ? "Ferma" : "Ascolta") {
                    Task {
                        if motore.inAscolto { await motore.ferma() }
                        else { await motore.avvia(a, b) }
                    }
                }
                .keyboardShortcut(.space, modifiers: [])

                Text(motore.stato).font(.caption).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.tail)
                Spacer(minLength: 8)

                SceltaLingua(scelta: $idA, catalogo: catalogo,
                             installate: installate, altra: idB)
                Button {
                    let x = idA; idA = idB; idB = x
                } label: { Image(systemName: "arrow.left.arrow.right") }
                    .help("scambia le due lingue")
                SceltaLingua(scelta: $idB, catalogo: catalogo,
                             installate: installate, altra: idA)

                Menu {
                    Picker("Dimensione", selection: $dimGrezza) {
                        ForEach(Dimensione.allCases) { d in
                            Text(d.nome).tag(d.rawValue)
                        }
                    }.pickerStyle(.inline)
                } label: {
                    Image(systemName: "textformat.size")
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
                .help("dimensione del testo (⌘+ / ⌘−)")
            }
            .padding(.horizontal, 10).padding(.vertical, 8)

            if let avviso {
                HStack(spacing: 6) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                    Text(avviso).font(.caption)
                    Spacer()
                }
                .padding(.horizontal, 12).padding(.bottom, 7)
            }
        }
    }

    // ------------------------------------------------------------ le lingue

    private func preparaLingue() async {
        let supportate = await SpeechTranscriber.supportedLocales.map { $0.identifier(.bcp47) }
        installate = Set(await SpeechTranscriber.installedLocales.map { $0.identifier(.bcp47) })
        catalogo = Catalogo.costruisci(supportate)
        // una scelta salvata che non e' piu' valida (o che non lo e' mai stata)
        // torna alla coppia di partenza invece di far giudicare il router da un
        // dizionario che non esiste
        if !catalogo.contains(where: { $0.id == idA }) { idA = Catalogo.predefinite.0 }
        if !catalogo.contains(where: { $0.id == idB }) { idB = Catalogo.predefinite.1 }
        await cambiaCoppia(riavvia: false)
        // in prova da file si parte da soli: non c'e' nessuno da aspettare
        if ProcessInfo.processInfo.environment["DV_PROVA"] != nil {
            await motore.avvia(a, b)
        }
    }

    /// Rifa' le due sessioni di traduzione e, se il motore era acceso, i
    /// trascrittori. Le sessioni si aprono qui e non al primo "Ascolta": la
    /// prima traduzione paga il caricamento del modello, e pagarlo mentre
    /// nessuno ha ancora parlato non costa niente a nessuno.
    private func cambiaCoppia(riavvia: Bool = true) async {
        avviso = await diagnosi()
        cfgAB = .init(source: .init(identifier: a.asr), target: .init(identifier: b.asr))
        cfgBA = .init(source: .init(identifier: b.asr), target: .init(identifier: a.asr))
        if riavvia && motore.inAscolto {
            await motore.ferma()
            await motore.avvia(a, b)
        }
    }

    /// Le due cose che possono rendere inutile una coppia, dette prima invece
    /// che scoperte a tavola.
    private func diagnosi() async -> String? {
        if a.id == b.id {
            return "le due lingue sono la stessa: scegline un'altra da un lato."
        }
        if a.corr == b.corr {
            // pt-BR contro pt-PT: il correttore ha un dizionario solo, quindi
            // i due testi valgono sempre uguale e il router non decide piu'
            return "\(a.nome) e \(b.nome) condividono il dizionario del correttore: il router non puo' distinguerle."
        }
        let st = await LanguageAvailability().status(
            from: .init(identifier: a.asr), to: .init(identifier: b.asr))
        if case .unsupported = st {
            return "macOS non traduce \(a.nome) → \(b.nome): resta la trascrizione, senza traduzione."
        }
        return nil
    }

    // --------------------------------------------------------- la traduzione

    private func svuota(_ s: TranslationSession, da lingua: String) async {
        try? await s.prepareTranslation()
        while !Task.isCancelled {
            let quelli = motore.daTradurre.filter { $0.lingua.id == lingua }
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
    @AppStorage("dimensione") private var dimGrezza = Dimensione.medio.rawValue

    var body: some Scene {
        WindowGroup("Due Voci") { ContentView() }
            .defaultSize(width: 720, height: 480)
            .commands {
                CommandGroup(after: .toolbar) {
                    Button("Testo più grande") {
                        dimGrezza = min(dimGrezza + 1, Dimensione.enorme.rawValue)
                    }.keyboardShortcut("+", modifiers: .command)
                    Button("Testo più piccolo") {
                        dimGrezza = max(dimGrezza - 1, Dimensione.piccolo.rawValue)
                    }.keyboardShortcut("-", modifiers: .command)
                }
            }
    }
}
