// Il router: quale delle due lingue ha parlato.
//
// Sta in un file suo perche' lo compilano DUE binari — l'app e il banco di
// misura. Se il banco ne avesse una copia, misurerebbe la copia: la lezione
// piu' cara di questo progetto e' che la prova e la cosa provata non devono
// mai essere due oggetti diversi che si assomigliano.
//
// L'idea, in una riga: non c'e' un rilevatore di lingua. Ci sono due
// trascrittori che ascoltano lo stesso microfono, e quello sbagliato non
// produce parole sbagliate — produce FRAMMENTI, perche' non riconosce e butta
// via quasi tutto ("a noi con" dove l'altro sente "Ontem à noite eu falei com
// a minha mãe no telefone"). Quindi vince chi ha trascritto piu' parole che il
// correttore ortografico di sistema riconosce come proprie. A parita', vince
// chi ne ha di piu' ESCLUSIVE della sua lingua.

import Foundation
import AppKit

// ------------------------------------------------------------------ le lingue

/// Una lingua utilizzabile in Due Voci. Servono TRE cose che la conoscano, e
/// solo due si vedono: il trascrittore (`asr`) e il traduttore. La terza e' il
/// correttore ortografico (`corr`), che qui non fa da correttore ma da giudice
/// — e senza di lui il router non ha nessuno a cui chiedere.
struct Lingua: Identifiable, Hashable {
    /// identificatore del `SpeechTranscriber`, es. "pt-BR"
    let asr: String
    /// codice del correttore ortografico, es. "pt_BR". Non e' lo stesso:
    /// il correttore usa l'underscore, e per certe lingue ha solo il codice
    /// base ("it" per it-CH, che l'ASR distingue e lui no).
    let corr: String

    var id: String { asr }
    /// "pt-BR" → "pt"
    var codice: String { String(asr.prefix(while: { $0 != "-" })) }
    /// il nome nella lingua dell'utente, es. "portoghese (Brasile)"
    var nome: String {
        Locale.current.localizedString(forIdentifier: asr) ?? asr
    }
}

/// Quali lingue si possono davvero scegliere.
///
/// Il filtro che conta e' il correttore, ed e' il motivo per cui questo
/// catalogo esiste invece di offrire tutto quello che sa trascrivere l'ASR:
/// `checkSpelling` con un codice che non conosce **non fallisce, dice si' a
/// tutto**. Misurato — la stessa frase italiana da' 7 parole valide chiesta in
/// "it", e 7 chiesta in "ja" o in "zz". Un router costruito su un giudice cosi'
/// non sbaglia: smette proprio di decidere, e nessuno se ne accorge.
/// Su questo Mac restano fuori giapponese e cinese, che l'ASR trascrive e il
/// correttore non conosce.
enum Catalogo {
    /// Il codice con cui chiamare il correttore per una lingua dell'ASR, o
    /// `nil` se il correttore non la conosce affatto.
    static func correttorePer(_ asr: String, disponibili: Set<String>) -> String? {
        let piatto = asr.replacingOccurrences(of: "-", with: "_")
        let base = String(asr.prefix(while: { $0 != "-" }))
        if disponibili.contains(piatto) { return piatto }
        if disponibili.contains(base) { return base }
        // it-CH non ha "it_CH"; pt-BR sceglie pt_BR sopra; se resta solo una
        // variante regionale della stessa lingua, vale come giudice
        return disponibili.first { $0.hasPrefix(base + "_") }
    }

    @MainActor
    static func costruisci(_ identificatoriASR: [String]) -> [Lingua] {
        let disponibili = Set(NSSpellChecker.shared.availableLanguages)
        return identificatoriASR.compactMap { id in
            correttorePer(id, disponibili: disponibili).map { Lingua(asr: id, corr: $0) }
        }
        .sorted { $0.nome.localizedCompare($1.nome) == .orderedAscending }
    }

    /// La coppia di partenza, quando non c'e' niente di salvato.
    static let predefinite = ("it-IT", "pt-BR")
}

/// Le due targhette. Normalmente il codice della lingua — IT, PT, ES — ma se
/// la coppia e' fatta di due varianti della stessa (pt-BR e pt-PT, en-US e
/// en-GB) due targhette uguali non direbbero niente, e si passa alla regione.
func sigle(_ a: Lingua, _ b: Lingua) -> (String, String) {
    a.codice == b.codice
        ? (a.asr.uppercased(), b.asr.uppercased())
        : (a.codice.uppercased(), b.codice.uppercased())
}

// ------------------------------------------------------------------ il giudice

/// Quante parole di un testo sono valide in una lingua, secondo il correttore
/// ortografico di macOS: ha i dizionari di tutte, e non sa niente di quale
/// trascrittore ha prodotto il testo che sta giudicando.
@MainActor
enum Lessico {
    private static var cache: [String: Int] = [:]

    static func valide(_ testo: String, lingua: String) -> Int {
        let chiave = lingua + "\u{1}" + testo
        if let v = cache[chiave] { return v }
        let ck = NSSpellChecker.shared
        let parole = testo.split(whereSeparator: { !$0.isLetter && $0 != "'" })
            .map(String.init).filter { $0.count > 2 }
        var n = 0
        for w in parole {
            let r = ck.checkSpelling(of: w, startingAt: 0, language: lingua,
                                     wrap: false, inSpellDocumentWithTag: 0,
                                     wordCount: nil)
            if r.location == NSNotFound { n += 1 }
        }
        // i parziali ricrescono parola per parola e ripassano dagli stessi
        // prefissi decine di volte a frase: senza cache si ripaga tutto ogni
        // volta, con la tastiera del correttore in mezzo
        if cache.count > 400 { cache.removeAll(keepingCapacity: true) }
        cache[chiave] = n
        return n
    }
}

/// Quanto si concede all'altro trascrittore quando a chiudere per primo e'
/// quello che NON guidava sui provvisori.
///
/// Sta qui per la stessa ragione del router, e per una ragione in piu': era
/// cablato in due posti, `1200` nell'app e `1200` nel banco, mentre il banco
/// STAMPAVA `700` perche' aveva una sua variabile che nessuno leggeva. Un
/// numero scritto due volte diverge; una manopola che stampa un valore che non
/// applica e' peggio, perche' fa attribuire alla manopola la varianza del caso.
@MainActor
enum Politica {
    static var attesaAltroMs: Int = 1200
}

/// L'esito del confronto fra i due testi.
struct Verdetto {
    let lingua: Lingua
    let testo: String
    /// quanto e' stata netta: 0 = pareggio deciso all'ultimo criterio
    let margine: Int
}

@MainActor
enum Router {
    /// Confronta quello che i due trascrittori hanno capito della stessa voce.
    /// Un testo vuoto significa "quel trascrittore non ha prodotto niente",
    /// che di per se' e' gia' una risposta.
    static func decidi(_ a: Lingua, _ testoA: String,
                       _ b: Lingua, _ testoB: String) -> Verdetto {
        if testoA.isEmpty && testoB.isEmpty { return Verdetto(lingua: a, testo: "", margine: 0) }
        if testoA.isEmpty { return Verdetto(lingua: b, testo: testoB, margine: 99) }
        if testoB.isEmpty { return Verdetto(lingua: a, testo: testoA, margine: 99) }

        let nA = Lessico.valide(testoA, lingua: a.corr)
        let nB = Lessico.valide(testoB, lingua: b.corr)
        if nA != nB {
            let vinceA = nA > nB
            return Verdetto(lingua: vinceA ? a : b,
                            testo: vinceA ? testoA : testoB,
                            margine: abs(nA - nB))
        }
        // pareggio: vince chi ha piu' parole ESCLUSIVE della sua lingua
        let eA = nA - Lessico.valide(testoA, lingua: b.corr)
        let eB = nB - Lessico.valide(testoB, lingua: a.corr)
        let vinceA = eA >= eB
        return Verdetto(lingua: vinceA ? a : b,
                        testo: vinceA ? testoA : testoB,
                        margine: 0)
    }
}
