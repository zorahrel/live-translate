// Il router: chi ha parlato, italiano o portoghese.
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

/// Il giudice lessicale: quante parole di un testo sono valide in una lingua.
/// E' il correttore ortografico di macOS, che ha entrambi i dizionari e non sa
/// niente di quale trascrittore ha prodotto il testo.
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

/// L'esito del confronto fra i due testi.
struct Verdetto {
    let lingua: String      // "it" | "pt"
    let testo: String
    /// quanto e' stata netta: 0 = pareggio deciso all'ultimo criterio
    let margine: Int
}

@MainActor
enum Router {
    /// Confronta quello che i due trascrittori hanno capito della stessa voce.
    /// `it` o `pt` vuoto significa "quel trascrittore non ha prodotto niente",
    /// che di per se' e' gia' una risposta.
    static func decidi(it: String, pt: String) -> Verdetto {
        if it.isEmpty && pt.isEmpty { return Verdetto(lingua: "it", testo: "", margine: 0) }
        if it.isEmpty { return Verdetto(lingua: "pt", testo: pt, margine: 99) }
        if pt.isEmpty { return Verdetto(lingua: "it", testo: it, margine: 99) }

        let nIt = valide(it, "it"), nPt = valide(pt, "pt")
        if nIt != nPt {
            let vinceIt = nIt > nPt
            return Verdetto(lingua: vinceIt ? "it" : "pt",
                            testo: vinceIt ? it : pt,
                            margine: abs(nIt - nPt))
        }
        // pareggio: vince chi ha piu' parole ESCLUSIVE della sua lingua
        let eIt = nIt - valide(it, "pt")
        let ePt = nPt - valide(pt, "it")
        let vinceIt = eIt >= ePt
        return Verdetto(lingua: vinceIt ? "it" : "pt",
                        testo: vinceIt ? it : pt,
                        margine: 0)
    }

    private static func valide(_ t: String, _ l: String) -> Int {
        Lessico.valide(t, lingua: l == "it" ? "it" : "pt")
    }
}
