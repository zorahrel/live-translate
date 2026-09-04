// Banco di misura della latenza. Rimpiazza il microfono con file audio
// riprodotti A VELOCITA' REALE: se la frase dura 3,4 secondi, il banco impiega
// 3,4 secondi a darla in pasto, un pezzetto da 100 ms alla volta come farebbe
// il tap dell'AVAudioEngine. Senza questo si misurerebbe la velocita' del
// disco, non quella del parlato.
//
// Tre cose imparate costruendolo, tutte e tre necessarie perche' misuri
// qualcosa invece di tacere:
//
//  1. La cadenza va tenuta su una scadenza assoluta. Con `sleep(100ms)` dentro
//     un ciclo che fa anche altro si arriva a dare 3,5 s di audio in 5,4 s: il
//     motore resta a bocca asciutta e i suoi tempi diventano i tempi del banco.
//  2. Il silenzio fra una frase e l'altra dev'essere fruscio, non zero
//     digitale: un microfono vero non produce mai zeri, e il rilevatore di
//     fine frase si comporta diversamente.
//  3. I modelli vanno chiesti con `AssetInventory` anche quando il sistema li
//     elenca gia' come installati. Senza, i trascrittori partono, non danno
//     errore, e non producono niente.
//
// `--veloce` accende `.volatileResults` + `.fastResults`; senza, si misura il
// comportamento di prima. Stesso harness per il prima e il dopo: confrontare
// due misure prese con due strumenti diversi non e' un confronto.

import Foundation
import Speech
import AVFoundation
import AppKit

let veloce = CommandLine.arguments.contains("--veloce")
/// nil = si tiene quella dell'app, che e' il punto di un banco
let attesaCLI: Int? = {
    if let i = CommandLine.arguments.firstIndex(of: "--attesa"),
       i + 1 < CommandLine.arguments.count { return Int(CommandLine.arguments[i+1]) }
    return nil
}()
let files = CommandLine.arguments.dropFirst().filter { $0.hasSuffix(".wav") }.sorted()

func ms(_ t: TimeInterval) -> String { String(format: "%5.0f", t * 1000) }

@MainActor
final class Banco {
    let lingue = ["it": "it-IT", "pt": "pt-BR"]
    var analyzers: [String: SpeechAnalyzer] = [:]
    var transcribers: [String: SpeechTranscriber] = [:]
    var conts: [String: AsyncStream<AnalyzerInput>.Continuation] = [:]
    var tasks: [Task<Void, Never>] = []

    var tInizio = Date()
    var tFine = Date()
    var primoParziale: [String: TimeInterval] = [:]
    var vive: [String: String] = [:]
    var capofila: String?
    var tChiusura: TimeInterval?
    var verdetto: Verdetto?
    var finale: [String: TimeInterval] = [:]
    var testoFinale: [String: String] = [:]
    var fmtASR: AVAudioFormat!

    // cadenza: una scadenza assoluta, non una somma di attese
    private let tZero = Date()
    private var scadenza: TimeInterval = 0

    func prepara() async throws {
        for (breve, loc) in lingue {
            let t = SpeechTranscriber(
                locale: Locale(identifier: loc),
                transcriptionOptions: [],
                reportingOptions: veloce ? [.volatileResults, .fastResults] : [],
                attributeOptions: [])
            transcribers[breve] = t
            analyzers[breve] = SpeechAnalyzer(modules: [t])
            let task = Task { [weak self] in
                do {
                    for try await r in t.results {
                        let s = String(r.text.characters)
                            .trimmingCharacters(in: .whitespacesAndNewlines)
                        guard !s.isEmpty else { continue }
                        await MainActor.run {
                            guard let self else { return }
                            if r.isFinal {
                                if self.finale[breve] == nil {
                                    self.finale[breve] = Date().timeIntervalSince(self.tFine)
                                }
                                self.testoFinale[breve] = s
                                self.definitivo(lingua: breve, testo: s)
                            } else {
                                if self.primoParziale[breve] == nil {
                                    self.primoParziale[breve] = Date().timeIntervalSince(self.tInizio)
                                }
                                self.vive[breve] = s
                                let it = self.vive["it"] ?? "", pt = self.vive["pt"] ?? ""
                                if !it.isEmpty || !pt.isEmpty {
                                    self.capofila = Router.decidi(it: it, pt: pt).lingua
                                }
                            }
                        }
                    }
                } catch {
                    FileHandle.standardError.write(
                        Data("trascrittore \(breve) interrotto: \(error)\n".utf8))
                }
            }
            tasks.append(task)
        }
        if let req = try await AssetInventory.assetInstallationRequest(
                supporting: Array(transcribers.values)) {
            try await req.downloadAndInstall()
        }
        fmtASR = await SpeechAnalyzer.bestAvailableAudioFormat(
            compatibleWith: Array(transcribers.values))
        for (breve, _) in lingue {
            let (st, cont) = AsyncStream<AnalyzerInput>.makeStream()
            conts[breve] = cont
            try await analyzers[breve]!.start(inputSequence: st)
        }
    }

    private func manda(_ p: AVAudioPCMBuffer) async {
        for (_, c) in conts { c.yield(AnalyzerInput(buffer: p)) }
        scadenza += 0.1
        let attesa = scadenza - Date().timeIntervalSince(tZero)
        if attesa > 0 { try? await Task.sleep(for: .seconds(attesa)) }
    }

    private var perPezzo: AVAudioFrameCount { AVAudioFrameCount(fmtASR.sampleRate / 10) }

    /// La stanza vuota fra una frase e l'altra.
    func fruscio(_ decimi: Int, finoAllaChiusura: Bool = false) async {
        for _ in 0..<decimi {
            guard let m = AVAudioPCMBuffer(pcmFormat: fmtASR, frameCapacity: perPezzo)
            else { return }
            m.frameLength = perPezzo
            let pt = m.audioBufferList.pointee.mBuffers.mData!
                .bindMemory(to: Int16.self, capacity: Int(perPezzo))
            for i in 0..<Int(perPezzo) { pt[i] = Int16.random(in: -24...24) }
            await manda(m)
            if finoAllaChiusura && verdetto != nil { return }
        }
    }

    func riproduci(_ path: String) async throws {
        let f = try AVAudioFile(forReading: URL(fileURLWithPath: path))
        let src = f.processingFormat
        guard let conv = AVAudioConverter(from: src, to: fmtASR),
              let inBuf = AVAudioPCMBuffer(pcmFormat: src,
                                           frameCapacity: AVAudioFrameCount(f.length))
        else { return }
        try f.read(into: inBuf)
        let cap = AVAudioFrameCount(
            Double(inBuf.frameLength) * fmtASR.sampleRate / src.sampleRate) + 4096
        guard let out = AVAudioPCMBuffer(pcmFormat: fmtASR, frameCapacity: cap) else { return }
        var dato = false
        var err: NSError?
        conv.convert(to: out, error: &err) { _, status in
            if dato { status.pointee = .noDataNow; return nil }
            dato = true; status.pointee = .haveData; return inBuf
        }
        guard err == nil else { throw err! }

        let bpf = Int(fmtASR.streamDescription.pointee.mBytesPerFrame)
        var off: AVAudioFrameCount = 0
        tInizio = Date()
        while off < out.frameLength {
            let n = min(perPezzo, out.frameLength - off)
            guard let pezzo = AVAudioPCMBuffer(pcmFormat: fmtASR, frameCapacity: n) else { break }
            pezzo.frameLength = n
            memcpy(pezzo.audioBufferList.pointee.mBuffers.mData!,
                   out.audioBufferList.pointee.mBuffers.mData!.advanced(by: Int(off) * bpf),
                   Int(n) * bpf)
            await manda(pezzo)
            off += n
        }
        tFine = Date()
    }

    /// Taglia la frase su ENTRAMBI i trascrittori prima di passare al file
    /// successivo. Senza, `azzera()` ripulisce solo la contabilita' del banco e
    /// i trascrittori tirano avanti con la loro segmentazione: uno che non ha
    /// ancora chiuso si porta dietro l'audio dei file precedenti e li scarica
    /// tutti insieme piu' tardi. Misurato: una riga conteneva tre frasi di tre
    /// file diversi, e la sbagliava per colpa delle altre due.
    /// Non si aspetta il ritorno di `finalize`: misurato, senza altro audio in
    /// arrivo puo' non tornare MAI — il banco si e' impiantato al sesto file su
    /// ventiquattro, a CPU zero, dopo cinque andati lisci. Si lancia e gli si
    /// da' mezzo secondo di stanza vuota, che e' l'input con cui raggiunge il
    /// confine. Quello che arriva tardi lo butta l'`azzera()` del giro dopo.
    func tagliaFrase() async {
        for (_, a) in analyzers { Task { try? await a.finalize(through: nil) } }
        await fruscio(5)
    }

    func azzera() {
        primoParziale.removeAll(); finale.removeAll(); testoFinale.removeAll()
        vive.removeAll(); capofila = nil; tChiusura = nil; verdetto = nil
        vistoIt = ""; vistoPt = ""; daParzialeIt = false; daParzialePt = false
        attesa?.cancel(); attesa = nil
    }

    private var attesa: Task<Void, Never>?

    /// La politica dell'app: se chiude il trascrittore che gia' guidava sui
    /// provvisori, si pubblica subito; altrimenti si concede all'altro 1,2 s.
    func definitivo(lingua: String, testo: String) {
        guard verdetto == nil else { return }
        attesa?.cancel()
        if testoFinale.count == lingue.count || lingua == capofila { chiudi(); return }
        attesa = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(Politica.attesaAltroMs))
            guard !Task.isCancelled else { return }
            await self?.chiudi()
        }
    }

    /// cosa aveva davanti il router quando ha deciso: senza, una riga "NO" dice
    /// che ha sbagliato e non cosa ha visto, che e' l'unica meta' azionabile
    var vistoIt = "", vistoPt = "", daParzialeIt = false, daParzialePt = false

    func chiudi() {
        guard verdetto == nil else { return }
        attesa?.cancel()
        let it = testoFinale["it"] ?? vive["it"] ?? ""
        let pt = testoFinale["pt"] ?? vive["pt"] ?? ""
        guard !(it.isEmpty && pt.isEmpty) else { return }
        vistoIt = it; vistoPt = pt
        daParzialeIt = testoFinale["it"] == nil; daParzialePt = testoFinale["pt"] == nil
        verdetto = Router.decidi(it: it, pt: pt)
        tChiusura = Date().timeIntervalSince(tFine)
    }

    func spegni() {
        for (_, c) in conts { c.finish() }
        for t in tasks { t.cancel() }
    }
}

@main
struct Main {
    static func main() async {
        guard !files.isEmpty else {
            print("uso: bench [--veloce] [--attesa ms] file1.wav …"); exit(2)
        }
        let b = await Banco()
        do { try await b.prepara() } catch {
            print("preparazione fallita: \(error)"); exit(1)
        }
        // due secondi di stanza vuota prima di cominciare, come all'accensione
        await b.fruscio(20)

        if let a = attesaCLI { await MainActor.run { Politica.attesaAltroMs = a } }
        let attesaVera = await MainActor.run { Politica.attesaAltroMs }
        print("modo: \(veloce ? "volatili + rapidi" : "solo finali (com'era)") · attesa altro trascrittore: \(attesaVera) ms\(attesaCLI == nil ? " (quella dell'app)" : " (forzata)")")
        print(String(repeating: "-", count: 76))
        print("file        atteso deciso   1º testo it  1º testo pt   finale it  finale pt")

        var parz: [TimeInterval] = [], fin: [TimeInterval] = []
        var chiusure: [TimeInterval] = []
        var giusti = 0

        for f in files {
            let nome = (f as NSString).lastPathComponent
            let atteso = nome.hasPrefix("it") ? "it" : "pt"
            await b.azzera()
            do { try await b.riproduci(f) } catch { print("\(nome): \(error)"); continue }
            await b.fruscio(70, finoAllaChiusura: true)
            await b.chiudi()
            await b.tagliaFrase()

            let vinto = await b.verdetto?.lingua ?? "–"
            if vinto == atteso { giusti += 1 }
            if let t = await b.tChiusura { chiusure.append(t) }
            let pIt = await b.primoParziale["it"], pPt = await b.primoParziale["pt"]
            let fIt = await b.finale["it"], fPt = await b.finale["pt"]
            // il primo testo utile e' quello del trascrittore che poi vince
            if let v = (atteso == "it" ? pIt : pPt) { parz.append(v) }
            if let v = (atteso == "it" ? fIt : fPt) { fin.append(v) }

            func opt(_ v: TimeInterval?) -> String { v.map { ms($0) } ?? "    –" }
            print("\(nome.padding(toLength: 10, withPad: " ", startingAt: 0))  \(atteso)     \(vinto) \(vinto == atteso ? "ok " : "NO ")   \(opt(pIt))        \(opt(pPt))       \(opt(fIt))      \(opt(fPt))")
            if vinto != atteso {
                let vIt = await b.vistoIt, vPt = await b.vistoPt
                let sIt = await b.daParzialeIt ? "parz" : "fin ", sPt = await b.daParzialePt ? "parz" : "fin "
                print("            it \(sIt) «\(vIt)»")
                print("            pt \(sPt) «\(vPt)»")
            }
        }

        print(String(repeating: "-", count: 76))
        func media(_ a: [TimeInterval]) -> String {
            a.isEmpty ? "mai" : ms(a.reduce(0,+)/Double(a.count)) + " ms"
        }
        print("primo testo a schermo, da quando si comincia a parlare: \(media(parz))")
        print("testo definitivo, da quando si smette di parlare:       \(media(fin))")
        print("riga pubblicata, da quando si smette di parlare:        \(media(chiusure))")
        print("lingua indovinata: \(giusti)/\(files.count)")
        await b.spegni()
        exit(0)
    }
}
