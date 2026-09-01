#!/usr/bin/env python3
"""Costruisce il campione su cui si misura il riconoscimento della lingua.

Un giudice solo non basta: langdetect etichetta 'italiano' con il 99% di
confidenza anche frasi che sono portoghese (il 4% delle volte), e misurare
contro quelle da' un numero peggiore del vero. Qui una frase entra nel
campione solo se langdetect e il correttore ortografico di macOS - che ha
entrambi i dizionari e non sa niente di langdetect - dicono la stessa cosa.

    .venv/bin/python build_goldset.py     riscrive goldset.json
"""
import glob
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
GOLD = os.path.join(HERE, "goldset.json")
SPELL_SRC = os.path.join(HERE, "tools", "spellcount.swift")
SPELL_BIN = os.path.join(HERE, "tools", "spellcount")


def build_speller() -> str:
    """Compila il contatore di parole valide, se serve."""
    if os.path.exists(SPELL_BIN) and \
            os.path.getmtime(SPELL_BIN) > os.path.getmtime(SPELL_SRC):
        return SPELL_BIN
    subprocess.run(["swiftc", "-O", SPELL_SRC, "-o", SPELL_BIN], check=True)
    return SPELL_BIN


def main() -> int:
    try:
        from langdetect import DetectorFactory, detect_langs
    except ImportError:
        print("manca langdetect: .venv/bin/pip install langdetect", file=sys.stderr)
        return 2
    DetectorFactory.seed = 0

    frasi, seen = [], set()
    for f in glob.glob("history/*.jsonl"):
        for line in open(f):
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if d.get("type") != "line":
                continue
            t = " ".join(d["src"].split())
            if len(t.split()) < 4 or t in seen:
                continue
            seen.add(t)
            frasi.append(t)
    print(f"frasi uniche in cronologia: {len(frasi)}")

    proposte = {}
    for t in frasi:
        try:
            b = detect_langs(t)[0]
        except Exception:  # noqa: BLE001
            continue
        if b.prob >= 0.99 and b.lang in ("it", "pt"):
            proposte[t] = b.lang
    print(f"proposte da langdetect (>=99%): {len(proposte)}")

    speller = build_speller()
    testi = list(proposte)
    r = subprocess.run([speller], input="\n".join(testi), capture_output=True, text=True)
    gold = {"it": [], "pt": []}
    scartate = 0
    for t, riga in zip(testi, r.stdout.strip().splitlines()):
        it, pt, _ = (int(x) for x in riga.split())
        # il correttore deve confermare: se dice l'altra lingua, o se non sa
        # decidere, la frase non entra nel campione
        conferma = "it" if it > pt else ("pt" if pt > it else "")
        if conferma and conferma == proposte[t]:
            gold[conferma].append(t)
        else:
            scartate += 1
    print(f"confermate dal correttore: it={len(gold['it'])} pt={len(gold['pt'])}"
          f"  (scartate {scartate} su cui i due giudici non concordano)")
    json.dump(gold, open(GOLD, "w"), ensure_ascii=False, indent=1)
    print(f"scritto {GOLD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
