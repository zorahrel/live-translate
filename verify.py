#!/usr/bin/env python3
"""Passa in rassegna tutto quello che l'app deve fare, e lo misura.

Esiste perche' i controlli erano sparsi in comandi usa e getta: ognuno provava
la cosa appena toccata, nessuno rileggeva l'insieme. Qui ogni requisito ha una
riga, un numero e un esito, e si rilancia tutto insieme dopo ogni modifica.

    ./verify.py            esce 0 se passa tutto
"""
import glob
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

# langdetect vive nel venv del progetto: e' il giudice esterno del
# riconoscimento lingua, e senza di lui quel controllo non si puo' fare
_venv = os.path.join(HERE, ".venv", "lib")
for _d in glob.glob(os.path.join(_venv, "python3*", "site-packages")):
    if _d not in sys.path:
        sys.path.append(_d)

spec = importlib.util.spec_from_file_location("lt", "live_translate.py")
lt = importlib.util.module_from_spec(spec)
_argv, sys.argv = sys.argv, ["verify"]
spec.loader.exec_module(lt)
sys.argv = _argv

FAILED = []


def check(req: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok ' if ok else 'ROTTO'}] {req}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILED.append(req)


def head(t: str) -> None:
    print(f"\n== {t}")


def history_rows() -> list:
    rows = []
    for f in glob.glob("history/*.jsonl"):
        for line in open(f):
            try:
                d = json.loads(line)
                if d.get("type") == "line":
                    rows.append(d)
            except Exception:  # noqa: BLE001
                pass
    return rows


# ---------------------------------------------------------------- allucinazioni
head("whisper firma i sottotitoli quando sente silenzio")
CREDITI = ["Legenda por Sônia Ruberti", "Legendas por João Silva",
           "Legendas pela comunidade Amara.org", "Sottotitoli a cura di Marco",
           "Subtitles by the Amara.org community", "Legendas: Caption.pt",
           "Sottotitoli e revisione a cura di QTSS", "Amara.org"]
VERE = ["A legenda do mapa é confusa", "Legenda por favor explica melhor",
        "Sottotitoli grandi per favore", "La traduzione di questo libro è ottima",
        "Vamos ver a legenda do gráfico", "Legendas por favor",
        "Sottotitoli di questo film sono brutti e lenti", "Bom dia, tudo bem?"]
scartati = sum(1 for t in CREDITI if lt._is_hallucination(t))
salvate = sum(1 for t in VERE if not lt._is_hallucination(t))
check("i crediti dei sottotitoli vengono scartati", scartati == len(CREDITI),
      f"{scartati}/{len(CREDITI)}")
check("le frasi vere che iniziano uguale restano", salvate == len(VERE),
      f"{salvate}/{len(VERE)}")

rows = history_rows()
cred_reali = [r for r in rows if lt._is_hallucination(r["src"]) == "credito sottotitoli"]
degeneri = [r for r in rows if r.get("dst", "").strip().rstrip(":") == "Traduzione"
            and not lt._is_hallucination(r["src"])]
check("sulla cronologia scarta solo i crediti veri", len(cred_reali) == 22,
      f"{len(cred_reali)} righe su {len(rows)}")
check("nessuna traduzione degenere 'Traduzione:' sopravvive", not degeneri,
      f"{len(degeneri)} rimaste")

# ------------------------------------------------------------------- direzione
head("in conversazione capisce chi sta parlando")
lt.CFG.update(bidi=True, langA="pt", langB="it")
NOTI = [("Oi, tudo bem?", "pt"), ("Ciao, tutto bene grazie", "it"),
        ("Você mora aqui?", "pt"), ("Sì, da tre anni", "it"), ("Que legal", "pt"),
        ("Ti piace la città?", "it"), ("Muito, adoro", "pt"),
        ("Andiamo a mangiare?", "it"), ("Vamos", "pt"), ("Perfetto", "it"),
        ("Obrigado", "pt"), ("Figurati", "it"), ("Muito bom", "pt"),
        ("Como vai você", "pt"), ("Sempre aqui", "pt"), ("Mais uma coisa", "pt"),
        ("Onde você está", "pt"), ("Que bonito", "pt"), ("Eu sou de São Paulo", "pt"),
        ("Sim", "pt"), ("Sì", "it"), ("Grazie", "it"), ("Tudo bem", "pt"),
        ("Va bene", "it"), ("Cadê o meu celular", "pt"), ("Tô com fome demais", "pt"),
        ("Ho fame da morire", "it"), ("Dov'è il mio telefono", "it"),
        ("Non ho ancora finito il lavoro", "it"), ("Ci sentiamo dopo", "it"),
        ("A gente se fala depois", "pt"), ("Devo uscire più tardi", "it")]
giusti = sum(1 for t, a in NOTI if lt.route(t)[0] == a)
check("i casi noti restano giusti", giusti == len(NOTI), f"{giusti}/{len(NOTI)}")

IT = re.compile(r"(?i)\b(che|non|però|perché|sono|siamo|questo|questa|quello|gli|"
                r"della|nella|già|più|adesso|allora|cosa|dove|quando|ecco|magari|"
                r"davvero|niente|sempre|anche|come|molto)\b")
PT = re.compile(r"(?i)\b(você|não|obrigad|tudo bem|pra|isso|aqui|está|são|muito|nós|eles)\b")
EN = re.compile(r"(?i)\b(the|and|i'm|going|you|that's|what's|don't|it's|we're)\b")
en_veri = [r["src"] for r in rows if len(EN.findall(r["src"])) >= 2
           and not PT.search(r["src"]) and not IT.search(r["src"])
           and len(r["src"].split()) >= 3]
inv = [t for t in en_veri if lt.route(t)[0] != "pt"]
pct_en = 100 * len(inv) / max(1, len(en_veri))
check("l'inglese in mezzo non ribalta la direzione", pct_en <= 2.0,
      f"{len(inv)}/{len(en_veri)} = {pct_en:.2f}%")

# Il giudice deve essere esterno: selezionare le frasi italiane con le stesse
# parole che il riconoscitore usa per riconoscerle da' un 1,41% che non vuol
# dire niente (la sovrapposizione era del 100%). langdetect e' addestrato
# altrove e non sa niente delle liste qui dentro: giudica lui.
try:
    from langdetect import DetectorFactory, detect_langs
    DetectorFactory.seed = 0
    gold = {"it": [], "pt": []}
    for r in rows:
        t = r["src"]
        if len(t.split()) < 4:
            continue
        try:
            b = detect_langs(t)[0]
        except Exception:  # noqa: BLE001
            continue
        if b.prob >= 0.99 and b.lang in gold:
            gold[b.lang].append(t)
    for lang, soglia in (("it", 14.0), ("pt", 2.0)):
        corpus = gold[lang]
        sb = [t for t in corpus if lt.route(t)[0] != lang]
        pct = 100 * len(sb) / max(1, len(corpus))
        check(f"{lang} giudicato da langdetect sotto il {soglia}%", pct <= soglia,
              f"{len(sb)}/{len(corpus)} = {pct:.2f}%")
except ImportError:
    check("langdetect disponibile come giudice terzo", False,
          "manca: .venv/bin/pip install langdetect")

lt.CFG.update(bidi=False)
dev = sum(1 for r in rows if lt.route(r["src"])[0] != "pt")
check("con il bidirezionale spento non si inverte mai", dev == 0,
      f"{dev} deviazioni su {len(rows)}")
lt.CFG.update(bidi=True)

# ---------------------------------------------------------------- preferenze
head("le scelte sopravvivono alla chiusura")
check("il file delle preferenze e' previsto", hasattr(lt, "PREFS_FILE"))
saved = {}
if os.path.exists(lt.PREFS_FILE):
    saved = json.load(open(lt.PREFS_FILE))
check("salva la modalita' e le due lingue",
      all(k in lt.PREFS_KEYS for k in ("bidi", "src", "dst")),
      ", ".join(lt.PREFS_KEYS[:5]) + "...")
if saved:
    check("le preferenze su disco sono leggibili", isinstance(saved.get("bidi"), bool),
          f"bidi={saved.get('bidi')} {saved.get('src')}->{saved.get('dst')}")

# --------------------------------------------------------------------- app
head("l'app nativa")
appbin = "LiveTranslate.app/Contents/MacOS/LiveTranslate"
if os.path.exists(appbin):
    r = subprocess.run([appbin], env={**os.environ, "LT_SELFTEST": "1"},
                       capture_output=True, text=True, timeout=60)
    for line in r.stdout.strip().splitlines():
        if ":" in line:
            k, _, v = line.rpartition(":")
            check(k.strip(), v.strip() == "true" or "→" in v or "⇄" in v, v.strip())
    check("l'autodiagnosi dell'app passa", r.returncode == 0, f"exit={r.returncode}")
else:
    check("l'app e' stata costruita", False, "manca LiveTranslate.app")

# ------------------------------------------------------------------- motore
head("il motore acceso adesso")
try:
    with urllib.request.urlopen("http://127.0.0.1:8777/langs", timeout=3) as fh:
        cfg = json.load(fh)
    check("l'overlay risponde", True, f"{cfg['src']}->{cfg['dst']} bidi={cfg['bidi']}")
    ps = subprocess.run(["ps", "ax", "-o", "command="], capture_output=True, text=True).stdout
    wl = [l for l in ps.splitlines() if "whisper-stream" in l]
    lang = re.search(r"-l (\w+)", wl[0]).group(1) if wl else "?"
    check("whisper gira", bool(wl), f"-l {lang}")
    check("in conversazione whisper riconosce le lingue da solo",
          (lang == "auto") == bool(cfg["bidi"]),
          f"bidi={cfg['bidi']} -l {lang}")
except Exception as exc:  # noqa: BLE001
    check("l'overlay risponde", False, str(exc)[:50])


def vivi() -> int:
    ps = subprocess.run(["ps", "ax", "-o", "command="], capture_output=True, text=True).stdout
    return sum(1 for l in ps.splitlines()
               if ("whisper-stream" in l or "argos_translate.py" in l
                   or "live_translate.py" in l) and "grep" not in l)


if "--ciclo" in sys.argv:
    # Il difetto che ha lasciato whisper acceso 26 ore: chiudendo la finestra
    # il motore restava a trascrivere per nessuno. Si prova spegnendo davvero,
    # quindi solo su richiesta: chiude l'app che stai usando.
    head("chiudere la finestra spegne tutto")
    prima = vivi()
    subprocess.run(["osascript", "-e", 'tell application "LiveTranslate" to quit'],
                   capture_output=True)
    for _ in range(20):
        time.sleep(1)
        if vivi() == 0:
            break
    dopo = vivi()
    check("alla chiusura non resta niente acceso", dopo == 0,
          f"{prima} processi -> {dopo}")
    subprocess.run(["open", "LiveTranslate.app"], capture_output=True)
    for _ in range(25):
        time.sleep(1)
        if vivi() >= 2:
            break
    check("riaprendola il motore torna su", vivi() >= 2, f"{vivi()} processi")
    try:
        with urllib.request.urlopen("http://127.0.0.1:8777/langs", timeout=5) as fh:
            c2 = json.load(fh)
        check("ritrova le impostazioni di prima", c2["bidi"] == cfg["bidi"],
              f"bidi={c2['bidi']} {c2['src']}->{c2['dst']}")
    except Exception as exc:  # noqa: BLE001
        check("ritrova le impostazioni di prima", False, str(exc)[:40])

print()
if FAILED:
    print(f"ROTTI {len(FAILED)}:")
    for f in FAILED:
        print(f"   - {f}")
    sys.exit(1)
print("tutto a posto")
