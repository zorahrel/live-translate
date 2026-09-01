import Foundation
import AppKit
// Quante parole sono valide in italiano e quante in portoghese: se il
// portoghese vince, langdetect ha sbagliato etichetta e l'errore non e' mio.
let ck = NSSpellChecker.shared
while let line = readLine() {
    let words = line.split(whereSeparator: { !$0.isLetter && $0 != "'" })
        .map(String.init).filter { $0.count > 2 }
    guard !words.isEmpty else { print("0 0 0"); continue }
    var score: [String: Int] = ["it": 0, "pt": 0]
    for lang in ["it", "pt"] {
        for w in words {
            let r = ck.checkSpelling(of: w, startingAt: 0, language: lang,
                                     wrap: false, inSpellDocumentWithTag: 0, wordCount: nil)
            if r.location == NSNotFound { score[lang]! += 1 }
        }
    }
    print("\(score["it"]!) \(score["pt"]!) \(words.count)")
}
