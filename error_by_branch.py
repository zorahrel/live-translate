#!/usr/bin/env python3
"""Spacca l'errore di direzione per ramo del router, con i conteggi.

Il titolo "it sbagliato il 5,39%" e' una media su rami che non si assomigliano:
`parole` decide guardando il testo, `whisper` si fida della lingua rilevata sul
chunk, `default` non decide niente - tiene la direzione principale, quindi su
una frase italiana e' sbagliato per costruzione, sempre. Mediarli insieme
nasconde sia dove il router e' gia' bravo sia dove non guarda proprio.

Non ricalcola le decisioni: le legge. Ogni riga di cronologia porta gia' il
ramo che l'ha decisa (`how`) e la direzione scelta (`srcLang`), scritti quando
la frase e' passata davvero, con whisper vivo. L'etichetta vera arriva da
goldset.json (langdetect + correttore di macOS, vedi build_goldset.py).

    .venv/bin/python error_by_branch.py            tabella e titolo
    .venv/bin/python error_by_branch.py --csv      una riga per frase sbagliata

Due avvertenze che il numero da solo non dice, e che lo strumento stampa:
la copertura (quante righe di quel ramo hanno un'etichetta: dove e' bassa il
suo errore e' una voce, non una misura) e l'intervallo di Wilson (su 15 righe
il 100% e' compatibile con il 78%).
"""
import collections
import glob
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

# I rami che route() sa produrre, nell'ordine in cui li prova. `whisper N%` e
# `scartato:xx` si raggruppano: la percentuale e la lingua terza sono dettagli
# della stessa decisione.
ORDINE = ["parole", "whisper", "terza lingua", "misto", "scartato", "default",
          "(assente)", "fisso"]


def famiglia(how) -> str:
    if not how:
        return "(assente)"
    for p in ("whisper", "terza lingua", "scartato"):
        if how.startswith(p):
            return p
    return how


def wilson(k: int, n: int) -> tuple:
    """Intervallo di Wilson al 95%: su pochi casi dice quanto poco sappiamo."""
    if n == 0:
        return (0.0, 100.0)
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * max(0.0, c - s), 100 * min(1.0, c + s))


def carica() -> tuple:
    gold = json.load(open("goldset.json"))
    lab = {t: L for L in ("it", "pt") for t in gold[L]}
    righe = []
    for f in sorted(glob.glob("history/*.jsonl")):
        for line in open(f):
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if d.get("type") != "line":
                continue
            d["_testo"] = " ".join(d.get("src", "").split())
            d["_ramo"] = famiglia(d.get("how"))
            d["_vero"] = lab.get(d["_testo"])
            d["_file"] = os.path.basename(f)
            righe.append(d)
    return righe, lab


def tabella(righe: list) -> dict:
    st = collections.defaultdict(
        lambda: {"righe": 0, "et": 0, "err": 0, "corte": 0,
                 "per_lingua": collections.defaultdict(lambda: [0, 0])})
    for d in righe:
        s = st[d["_ramo"]]
        s["righe"] += 1
        if d["_vero"]:
            s["et"] += 1
            s["per_lingua"][d["_vero"]][0] += 1
            if d.get("srcLang") != d["_vero"]:
                s["err"] += 1
                s["per_lingua"][d["_vero"]][1] += 1
        elif len(d["_testo"].split()) < 4:
            s["corte"] += 1
    return st


def stampa(st: dict, righe: list) -> None:
    def riga(nome, s):
        cop = 100 * s["et"] / max(1, s["righe"])
        pct = 100 * s["err"] / max(1, s["et"])
        lo, hi = wilson(s["err"], s["et"])
        it = s["per_lingua"].get("it", [0, 0])
        pt = s["per_lingua"].get("pt", [0, 0])
        ci = f"{lo:.1f}-{hi:.1f}%" if s["et"] else "-"
        print(f"  {nome:13} {s['righe']:6} {s['et']:6} {cop:5.0f}%  "
              f"{it[1]:>3}/{it[0]:<5} {pt[1]:>3}/{pt[0]:<5} "
              f"{pct:6.2f}%  {ci:>12}")

    print("\n== errore di direzione per ramo, in conversazione (bidi)")
    print(f"  {'ramo':13} {'righe':>6} {'etich':>6} {'cop':>6}  "
          f"{'it err/tot':<9} {'pt err/tot':<9} {'errore':>7}  {'Wilson 95%':>12}")
    tot = {"righe": 0, "et": 0, "err": 0}
    for nome in ORDINE:
        if nome == "fisso" or nome not in st:
            continue
        s = st[nome]
        riga(nome, s)
        for k in tot:
            tot[k] += s[k]
    print(f"  {'':13} {'-' * 6} {'-' * 6}")
    print(f"  {'tutti':13} {tot['righe']:6} {tot['et']:6} "
          f"{100 * tot['et'] / max(1, tot['righe']):5.0f}%  "
          f"{'':<9} {'':<9} {100 * tot['err'] / max(1, tot['et']):6.2f}%")
    if "fisso" in st:
        s = st["fisso"]
        print(f"\n  fuori misura: {s['righe']} righe in modalita' fissa "
              f"(lingua dichiarata dall'utente, il router non decide)")


def decomposizione(st: dict, lingua: str) -> None:
    """Quanto pesa ogni ramo sull'errore di quella lingua."""
    tot = sum(s["per_lingua"].get(lingua, [0, 0])[0] for n, s in st.items() if n != "fisso")
    err = sum(s["per_lingua"].get(lingua, [0, 0])[1] for n, s in st.items() if n != "fisso")
    if not tot:
        return
    print(f"\n== da dove viene l'errore {lingua} ({err} su {tot} = "
          f"{100 * err / tot:.2f}%)")
    print(f"  {'ramo':13} {'quota traffico':>15} {'errore ramo':>12} {'contributo':>12}")
    voci = []
    for nome in ORDINE:
        if nome == "fisso" or nome not in st:
            continue
        n, e = st[nome]["per_lingua"].get(lingua, [0, 0])
        if not n:
            continue
        voci.append((100 * e / tot, nome, n, e))
    for contrib, nome, n, e in sorted(voci, reverse=True):
        print(f"  {nome:13} {100 * n / tot:14.1f}% {100 * e / n:11.1f}% "
              f"{contrib:11.2f}p")


def buchi(righe: list, st: dict) -> None:
    print("\n== quello che questa misura non vede")
    tot_non = 0
    for nome in ORDINE:
        if nome == "fisso" or nome not in st:
            continue
        s = st[nome]
        non = s["righe"] - s["et"]
        tot_non += non
        if non:
            print(f"  {nome:13} {non:5} righe senza etichetta "
                  f"({100 * non / s['righe']:.0f}% del ramo), "
                  f"di cui {s['corte']} sotto le 4 parole")
    print(f"  in totale {tot_non} righe di conversazione non sono misurate: il "
          f"campione\n  a doppia conferma le esclude per costruzione (<4 parole, "
          f"o giudici in disaccordo)")


def confronto_offline(righe: list) -> None:
    """Il ricalcolo che fa verify.py non e' il router che gira davvero.

    route() legge la lingua che whisper ha rilevato sull'ultimo chunk, e quella
    esiste solo mentre l'app ascolta. Rilanciando route() su un file di testo
    quel canale e' vuoto: il ramo `whisper` non puo' scattare, e le frasi che a
    quel ramo devono la decisione giusta cadono su `default`, che sull'italiano
    sbaglia sempre. Le conta, cosi' si sa quanta parte del titolo e' il router
    e quanta e' l'assenza di whisper nella misura.
    """
    import importlib.util
    for d in glob.glob(os.path.join(".venv", "lib", "python3*", "site-packages")):
        if d not in sys.path:
            sys.path.append(d)
    spec = importlib.util.spec_from_file_location("lt", "live_translate.py")
    lt = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, ["verify"]
    spec.loader.exec_module(lt)
    sys.argv = argv
    lt.CFG.update(bidi=True, langA="pt", langB="it")

    reale = {}
    for d in righe:
        if d["_ramo"] not in ("fisso", "(assente)"):
            reale.setdefault(d["_testo"], (d["_ramo"], d.get("srcLang")))

    gold = json.load(open("goldset.json"))
    print("\n== il ricalcolo offline non e' il router vivo")
    for lingua in ("it", "pt"):
        rami, err, salvate, mai = collections.Counter(), 0, 0, 0
        for t in gold[lingua]:
            src, _, how = lt.route(t)
            ramo = famiglia(how)
            rami[ramo] += 1
            if src != lingua:
                err += 1
                r = reale.get(t)
                if r is None:
                    mai += 1
                elif r[1] == lingua:
                    salvate += 1
        n = len(gold[lingua])
        print(f"  {lingua}: {err}/{n} = {100 * err / n:.2f}% rilanciando route() "
              f"sul campione")
        if err:
            print(f"     di cui {salvate} frasi che dal vivo erano state decise "
                  f"bene (ramo whisper spento nel ricalcolo)")
            print(f"     e {mai} mai passate in conversazione: nessun ramo reale "
                  f"da confrontare")
        print(f"     rami nel ricalcolo: "
              + ", ".join(f"{k} {v}" for k, v in rami.most_common()))


def csv(righe: list) -> None:
    print("file,ramo,vero,scelto,testo")
    for d in righe:
        if d["_vero"] and d.get("srcLang") != d["_vero"] and d["_ramo"] != "fisso":
            t = d["_testo"].replace('"', "'")
            print(f'{d["_file"]},{d["_ramo"]},{d["_vero"]},{d.get("srcLang")},"{t}"')


def main() -> int:
    if not os.path.exists("goldset.json"):
        print("manca goldset.json: .venv/bin/python build_goldset.py", file=sys.stderr)
        return 2
    righe, lab = carica()
    st = tabella(righe)
    if "--csv" in sys.argv:
        csv(righe)
        return 0
    print(f"cronologia: {len(righe)} righe, campione a doppia conferma: "
          f"{len(lab)} frasi etichettate")
    stampa(st, righe)
    for lingua in ("it", "pt"):
        decomposizione(st, lingua)
    if "--veloce" not in sys.argv:
        confronto_offline(righe)
    buchi(righe, st)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
