// Disegna l'icona di Due Voci e scrive l'iconset.
//
// Due bolle che si sovrappongono: blu a sinistra, verde a destra — gli stessi
// due colori con cui l'app segna chi ha parlato. Niente lettere: le lingue si
// scelgono a runtime, un "IT/PT" stampato nell'icona sarebbe una bugia appena
// qualcuno cambia coppia.
//
// Le proporzioni sono quelle di macOS Big Sur in poi: corpo 824x824 dentro un
// quadro da 1024, raggio 185. Non e' vezzo — un'icona che riempie tutto il
// quadro sembra piu' grande delle vicine nel Dock.

import AppKit

let quadro: CGFloat = 1024
let margine: CGFloat = 100
let corpo = CGRect(x: margine, y: margine, width: quadro - margine * 2, height: quadro - margine * 2)
let raggio: CGFloat = 185

/// Una bolla: rettangolo stondato piu' la codina, in un pezzo solo.
func bolla(_ r: CGRect, raggio rr: CGFloat, codaX: CGFloat, verso: CGFloat) -> CGPath {
    let p = CGMutablePath()
    p.addRoundedRect(in: r, cornerWidth: rr, cornerHeight: rr)
    // la coda parte dal bordo basso e scende in diagonale
    let c = CGMutablePath()
    let base = r.minY + 2
    c.move(to: CGPoint(x: codaX, y: base))
    c.addLine(to: CGPoint(x: codaX + verso * 62, y: base))
    c.addLine(to: CGPoint(x: codaX + verso * 18, y: base - 96))
    c.closeSubpath()
    let insieme = CGMutablePath()
    insieme.addPath(p)
    insieme.addPath(c)
    return insieme
}

func disegna(_ ctx: CGContext) {
    let sp = CGColorSpace(name: CGColorSpace.sRGB)!

    // fondo scuro, come la finestra dell'app
    ctx.saveGState()
    let sagoma = CGPath(roundedRect: corpo, cornerWidth: raggio, cornerHeight: raggio, transform: nil)
    ctx.addPath(sagoma)
    ctx.clip()
    let fondo = CGGradient(colorsSpace: sp, colors: [
        CGColor(srgbRed: 0.16, green: 0.17, blue: 0.20, alpha: 1),
        CGColor(srgbRed: 0.07, green: 0.08, blue: 0.10, alpha: 1),
    ] as CFArray, locations: [0, 1])!
    ctx.drawLinearGradient(fondo, start: CGPoint(x: 0, y: quadro), end: CGPoint(x: 0, y: 0), options: [])
    ctx.restoreGState()

    // bolla verde: dietro, in basso a destra
    let verde = bolla(CGRect(x: 470, y: 300, width: 400, height: 292), raggio: 96,
                      codaX: 700, verso: 1)
    ctx.saveGState()
    ctx.addPath(verde)
    ctx.clip()
    let gv = CGGradient(colorsSpace: sp, colors: [
        CGColor(srgbRed: 0.30, green: 0.85, blue: 0.45, alpha: 1),
        CGColor(srgbRed: 0.10, green: 0.63, blue: 0.34, alpha: 1),
    ] as CFArray, locations: [0, 1])!
    ctx.drawLinearGradient(gv, start: CGPoint(x: 470, y: 592), end: CGPoint(x: 870, y: 300), options: [])
    ctx.restoreGState()

    // bolla blu: davanti, in alto a sinistra. Lo stacco scuro attorno serve a
    // far leggere le due forme anche a 16 px, dove si toccherebbero e basta.
    let blu = bolla(CGRect(x: 154, y: 430, width: 430, height: 314), raggio: 104,
                    codaX: 260, verso: -1)
    ctx.saveGState()
    ctx.addPath(blu)
    ctx.setLineWidth(46)
    ctx.setStrokeColor(CGColor(srgbRed: 0.07, green: 0.08, blue: 0.10, alpha: 1))
    ctx.strokePath()
    ctx.restoreGState()

    ctx.saveGState()
    ctx.addPath(blu)
    ctx.clip()
    let gb = CGGradient(colorsSpace: sp, colors: [
        CGColor(srgbRed: 0.36, green: 0.66, blue: 1.00, alpha: 1),
        CGColor(srgbRed: 0.11, green: 0.40, blue: 0.92, alpha: 1),
    ] as CFArray, locations: [0, 1])!
    ctx.drawLinearGradient(gb, start: CGPoint(x: 154, y: 744), end: CGPoint(x: 584, y: 430), options: [])
    ctx.restoreGState()

    // il velo di luce in alto, che e' quello che distingue un'icona macOS da un
    // rettangolo colorato
    ctx.saveGState()
    ctx.addPath(sagoma)
    ctx.clip()
    let velo = CGGradient(colorsSpace: sp, colors: [
        CGColor(srgbRed: 1, green: 1, blue: 1, alpha: 0.16),
        CGColor(srgbRed: 1, green: 1, blue: 1, alpha: 0.0),
    ] as CFArray, locations: [0, 1])!
    ctx.drawLinearGradient(velo, start: CGPoint(x: 0, y: quadro - margine),
                           end: CGPoint(x: 0, y: quadro * 0.52), options: [])
    ctx.restoreGState()
}

func scrivi(lato: Int, in url: URL) {
    let s = CGFloat(lato)
    guard let ctx = CGContext(data: nil, width: lato, height: lato, bitsPerComponent: 8,
                              bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
        FileHandle.standardError.write("contesto non creato a \(lato)\n".data(using: .utf8)!)
        exit(1)
    }
    ctx.scaleBy(x: s / quadro, y: s / quadro)
    ctx.setAllowsAntialiasing(true)
    ctx.interpolationQuality = .high
    disegna(ctx)
    guard let img = ctx.makeImage() else { exit(1) }
    let rep = NSBitmapImageRep(cgImage: img)
    guard let dati = rep.representation(using: .png, properties: [:]) else { exit(1) }
    try! dati.write(to: url)
}

let dove = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "DueVoci.iconset")
try? FileManager.default.createDirectory(at: dove, withIntermediateDirectories: true)

// i nomi li impone iconutil, non si scelgono
let pezzi: [(String, Int)] = [
    ("icon_16x16", 16), ("icon_16x16@2x", 32),
    ("icon_32x32", 32), ("icon_32x32@2x", 64),
    ("icon_128x128", 128), ("icon_128x128@2x", 256),
    ("icon_256x256", 256), ("icon_256x256@2x", 512),
    ("icon_512x512", 512), ("icon_512x512@2x", 1024),
]
for (nome, lato) in pezzi {
    scrivi(lato: lato, in: dove.appendingPathComponent("\(nome).png"))
}
print("iconset: \(dove.path) (\(pezzi.count) pezzi)")
