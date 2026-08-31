#!/usr/bin/env python3
"""Traduzione live dal microfono, con sottotitoli in un overlay del browser.

whisper.cpp (locale, Metal) -> traduzione -> pagina web via SSE.
Lingue, modello e volume del microfono si cambiano a caldo dall'overlay.
La cronologia e' persistente su disco (JSONL) e si riapre a ogni avvio.

Uso:  live-translate [--src pt] [--dst it] [--model turbo] [--port 8777]
"""
import argparse
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
def _find_whisper() -> str:
    """whisper-stream, ovunque sia: Homebrew arm/intel, PATH, build locale."""
    env = os.environ.get("LT_WHISPER")
    if env and os.path.exists(env):
        return env
    found = shutil.which("whisper-stream")
    if found:
        return found
    for c in ("/opt/homebrew/bin/whisper-stream", "/usr/local/bin/whisper-stream",
              os.path.join(HERE, "whisper.cpp", "build", "bin", "whisper-stream")):
        if os.path.exists(c):
            return c
    return "whisper-stream"


WHISPER = _find_whisper()
HIST_DIR = os.path.join(HERE, "history")
os.makedirs(HIST_DIR, exist_ok=True)

# La chiave e' opzionale: senza, si usa il traduttore locale. Si legge
# dall'ambiente o da un .env accanto allo script.
API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
for _env in (os.path.join(HERE, ".env"), os.path.expanduser("~/.config/live-translate/.env")):
    if API_KEY:
        break
    if os.path.exists(_env):
        try:
            with open(_env) as fh:
                for line in fh:
                    if line.startswith("CEREBRAS_API_KEY="):
                        API_KEY = line.split("=", 1)[1].strip()
                        break
        except Exception:  # noqa: BLE001
            pass

TS_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\.\d{3} --> \d{2}:\d{2}:\d{2}\.\d{3}\]\s*(.*)$")
NOISE_RE = re.compile(r"^[\s\-\.\*\(\[\]\)_]*$")
TAG_RE = re.compile(r"[\[\(\*][^\]\)\*]{0,40}[\]\)\*]")
HALLUC = {
    "obrigado", "obrigada", "muito obrigado", "tchau", "amara.org",
    "legendas pela comunidade amara.org", "legendas: caption.pt",
    "subtitles by the amara.org community", "thanks for watching",
    "thank you", "grazie", "sottotitoli e revisione a cura di qtss",
    "sottotitoli creati dalla comunità amara.org", "легенды", "字幕",
}

LANGS = [
    ("auto", "Rileva lingua"),
    ("pt", "Portoghese"), ("it", "Italiano"), ("en", "Inglese"), ("es", "Spagnolo"),
    ("fr", "Francese"), ("de", "Tedesco"), ("nl", "Olandese"), ("ro", "Rumeno"),
    ("pl", "Polacco"), ("ru", "Russo"), ("uk", "Ucraino"), ("tr", "Turco"),
    ("ar", "Arabo"), ("zh", "Cinese"), ("ja", "Giapponese"), ("ko", "Coreano"),
    ("hi", "Hindi"), ("el", "Greco"), ("sv", "Svedese"), ("ca", "Catalano"),
    ("cs", "Ceco"), ("da", "Danese"), ("fi", "Finlandese"), ("he", "Ebraico"),
    ("hu", "Ungherese"), ("id", "Indonesiano"), ("no", "Norvegese"), ("th", "Thai"),
    ("vi", "Vietnamita"),
]
LANG_NAME = dict(LANGS)
DST_LANGS = [(c, n) for c, n in LANGS if c != "auto"]

# M2 Max, 32 GB: 'turbo' e' il default sensato, gli altri restano per audio difficile o batteria
MODELS = [
    ("turbo", "Turbo (migliore)", "ggml-large-v3-turbo-q5_0.bin"),
    ("small", "Small (bilanciato)", "ggml-small.bin"),
    ("base", "Base (piu' rapido)", "ggml-base.bin"),
]
MODEL_FILE = {k: f for k, _, f in MODELS}

CFG = {"src": "pt", "dst": "it", "model": "turbo", "capture": "1", "mic": 30,
       "bidi": False, "langA": "pt", "langB": "it", "stream": True,
       "tts": False, "rate": 210, "preview": True}

# in sliding window whisper riscrive la riga con l'escape ANSI di clear-line
ANSI_CLR = re.compile(r"\x1b\[2K")
ANSI_ANY = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

DET_RE = re.compile(r"auto-detected language: ([a-z]{2,3}) \(p = ([0-9.]+)\)")
det_lock = threading.Lock()
det_lang = {"code": "", "p": 0.0, "at": 0.0}

# parole funzione: whisper confonde spesso pt/es/it fra loro sulle frasi corte
STOPWORDS = {
    "pt": {"que", "nao", "não", "para", "com", "uma", "meu", "minha", "voce", "você", "isso",
           "muito", "esta", "está", "eu", "ele", "ela", "tem", "mais", "pra", "aqui", "entao",
           "então", "ja", "já", "tudo", "bem", "cara", "gente", "ser", "fazer", "coisa"},
    "it": {"che", "non", "per", "con", "una", "mio", "mia", "tu", "questo", "molto", "sono",
           "lui", "lei", "ha", "piu", "più", "qui", "allora", "gia", "già", "tutto", "bene",
           "cosa", "fare", "essere", "anche", "come", "quando", "perche", "perché", "cosi"},
    "es": {"que", "no", "para", "con", "una", "mi", "tu", "esto", "muy", "estoy", "él", "ella",
           "tiene", "mas", "más", "aqui", "aquí", "entonces", "ya", "todo", "bien", "cosa",
           "hacer", "ser", "tambien", "también", "como", "cuando", "porque", "pero"},
    "en": {"the", "and", "that", "for", "with", "you", "this", "very", "have", "here", "then",
           "all", "good", "thing", "make", "just", "what", "when", "because", "but", "about"},
    "fr": {"que", "pas", "pour", "avec", "une", "mon", "vous", "cela", "tres", "très", "suis",
           "il", "elle", "plus", "ici", "alors", "deja", "déjà", "tout", "bien", "chose"},
}


# marcatori esclusivi: se compaiono, la lingua e' praticamente certa
EXCLUSIVE = {
    "pt": {"você", "voce", "não", "então", "muito", "tá", "está", "são", "coisas", "duas",
           "gente", "pra", "nós", "eles", "isso", "aqui", "mais", "já", "ção", "ões", "nh",
           "oi", "obrigado", "obrigada", "tudo", "bom", "legal", "cara", "amigo", "amiga",
           "vamos", "quero", "fazer", "agora", "sempre", "nunca", "porque", "quando"},
    "it": {"perché", "però", "così", "più", "gli", "della", "delle", "nella", "sono", "anche",
           "quello", "questa", "cosa", "essere", "doppia", "traduzione", "che", "sia",
           "ciao", "stai", "sei", "siamo", "vado", "faccio", "voglio", "adesso", "bene",
           "grazie", "prego", "certo", "vero", "niente", "sempre", "ancora", "dopo"},
    "es": {"pero", "porque", "también", "está", "esto", "muy", "hacer", "ellos", "señor"},
    "en": {"the", "this", "with", "have", "what", "because", "would", "should"},
    "fr": {"c'est", "pour", "avec", "être", "cette", "aussi", "parce"},
}
# sequenze di lettere che una lingua ha e l'altra no
NGRAMS = {
    "pt": ("ão", "õe", "ç", "nh", "lh", "ê", "â"),
    "it": ("gli", "gn", "cchi", "zione", "à ", "ù"),
    "es": ("ñ", "¿", "¡", "ll"),
}


def score_lang(text: str, candidates) -> dict:
    """Punteggio grezzo per lingua: stopword + marcatori esclusivi + n-grammi."""
    low = text.lower()
    words = re.findall(r"[a-zà-ÿ']+", low)
    scores = {}
    for c in candidates:
        sc = sum(1 for w in words if w in STOPWORDS.get(c, ()))
        sc += 2 * sum(1 for w in words if w in EXCLUSIVE.get(c, ()))
        sc += 2 * sum(1 for g in NGRAMS.get(c, ()) if g in low)
        scores[c] = sc
    return scores


def guess_lang(text: str, candidates, min_words: int = 4, margin: int = 2) -> str:
    """Rilevamento a stopword fra le sole lingue candidate. '' se e' indeciso."""
    words = re.findall(r"[a-zà-ÿ']+", text.lower())
    if len(words) < min_words:
        return ""
    scores = score_lang(text, candidates)
    if not scores:
        return ""
    best = max(scores, key=scores.get)
    ranked = sorted(scores.values(), reverse=True)
    if ranked[0] == 0 or (len(ranked) > 1 and ranked[0] - ranked[1] < margin):
        return ""
    return best


# taglia su punteggiatura forte, ma anche dove il parlato cambia turno
SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\s*[–—]\s+|\s{3,}")


def segment(text: str) -> list:
    """Spezza in unita' abbastanza lunghe da poter essere riconosciute."""
    parts = [p.strip() for p in SPLIT_RE.split(text) if p and p.strip()]
    if len(parts) <= 1:
        return [text.strip()]
    # riattacca i frammenti troppo corti al precedente: da soli non si riconoscono
    out: list = []
    for p in parts:
        if out and len(re.findall(r"[a-zà-ÿ']+", p)) < 3:
            out[-1] = out[-1] + " " + p
        else:
            out.append(p)
    return out


def split_by_language(text: str, a: str, b: str) -> list:
    """Divide una frase mista in blocchi [(testo, lingua)], accorpando i vicini uguali.

    Serve quando due voci si accavallano e whisper le fonde in un chunk solo.
    Se non trova almeno due lingue diverse con certezza, ritorna un blocco solo:
    spezzare a caso una frase monolingue la peggiora.
    """
    segs = segment(text)
    if len(segs) < 2:
        return [(text.strip(), "")]
    tagged = []
    for sg in segs:
        # soglia piu' bassa: qui i pezzi sono corti per costruzione
        tagged.append((sg, guess_lang(sg, (a, b), min_words=2, margin=2)))
    found = {l for _, l in tagged if l}
    if len(found) < 2:
        return [(text.strip(), "")]
    # i segmenti indecisi ereditano la lingua del vicino gia' etichettato
    for i, (sg, l) in enumerate(tagged):
        if l:
            continue
        prev = next((tagged[j][1] for j in range(i - 1, -1, -1) if tagged[j][1]), "")
        nxt = next((tagged[j][1] for j in range(i + 1, len(tagged)) if tagged[j][1]), "")
        tagged[i] = (sg, prev or nxt)
    merged: list = []
    for sg, l in tagged:
        if merged and merged[-1][1] == l:
            merged[-1] = (merged[-1][0] + " " + sg, l)
        else:
            merged.append((sg, l))
    return merged
src_q: "queue.Queue" = queue.Queue()
subscribers: "list" = []
sub_lock = threading.Lock()
stop_flag = threading.Event()
history: "list" = []
proc_lock = threading.Lock()
whisper_proc = None
gen = 0
# prefisso unico per processo: senza, gli id delle righe nuove collidono con
# quelli gia' in cronologia e la vista sovrascrive le righe vecchie
RUN_ID = format(int(time.time()) % 100000, "05d")

def _session_file(resume_within: int = 1800) -> str:
    """Riusa l'ultimo file se e' stato scritto da poco, altrimenti ne apre uno.

    Un riavvio dell'app in mezzo a una conversazione non e' una sessione nuova:
    spezzare li' la cronologia significa perdere di vista quello che si stava
    appena trascrivendo.
    """
    try:
        files = [f for f in os.listdir(HIST_DIR) if f.endswith(".jsonl")]
        if files:
            newest = max(files, key=lambda f: os.path.getmtime(os.path.join(HIST_DIR, f)))
            path = os.path.join(HIST_DIR, newest)
            if time.time() - os.path.getmtime(path) < resume_within:
                return path
    except Exception:  # noqa: BLE001
        pass
    return os.path.join(HIST_DIR, time.strftime("%Y-%m-%d_%H%M%S") + ".jsonl")


SESSION_FILE = _session_file()
hist_lock = threading.Lock()


def persist(evt: dict) -> None:
    with hist_lock:
        with open(SESSION_FILE, "a") as fh:
            fh.write(json.dumps(evt, ensure_ascii=False) + "\n")


def load_recent(limit: int = 400) -> list:
    """Righe gia' trascritte, dalla piu' vecchia alla piu' recente.

    La sessione in corso viene restituita intera: e' quella che l'utente si
    aspetta di ritrovare riaprendo la finestra. Le precedenti riempiono il
    resto fino a `limit`.
    """
    out: list = []
    with hist_lock:
        try:
            with open(SESSION_FILE) as fh:
                out = [json.loads(l) for l in fh if l.strip()]
        except Exception:  # noqa: BLE001
            out = []
    if len(out) >= limit:
        out = out[-limit:]
    seen: dict = {}
    for i, row in enumerate(out):
        rid = str(row.get("id", i))
        if rid in seen:
            seen[rid] += 1
            row = dict(row)
            row["id"] = f"{rid}~{seen[rid]}"
            out[i] = row
        else:
            seen[rid] = 0
    return out
    others = sorted(f for f in os.listdir(HIST_DIR)
                    if f.endswith(".jsonl") and os.path.join(HIST_DIR, f) != SESSION_FILE)
    for name in reversed(others):
        try:
            with open(os.path.join(HIST_DIR, name)) as fh:
                rows = [json.loads(l) for l in fh if l.strip()]
        except Exception:  # noqa: BLE001
            continue
        out = rows + out
        if len(out) >= limit:
            break
    out = out[-limit:]
    seen: dict = {}
    for i, row in enumerate(out):
        rid = str(row.get("id", i))
        if rid in seen:
            seen[rid] += 1
            row = dict(row)
            row["id"] = f"{rid}~{seen[rid]}"
            out[i] = row
        else:
            seen[rid] = 0
    return out


def broadcast(evt: dict, save: bool = False) -> None:
    if save:
        persist(evt)
        history.append(evt)
        del history[:-200]
    with sub_lock:
        dead = []
        for q in subscribers:
            try:
                q.put_nowait(evt)
            except Exception:  # noqa: BLE001
                dead.append(q)
        for q in dead:
            subscribers.remove(q)


# ----------------------------------------------------------------- traduzione
def _t_llm(text: str) -> str:
    if not API_KEY:
        return ""
    dst_name = LANG_NAME.get(ACTIVE["dst"], ACTIVE["dst"])
    payload = {
        "model": "gpt-oss-120b",
        "messages": [
            {"role": "system", "content":
                f"Sei un interprete simultaneo. Traduci in {dst_name} il frammento di parlato che "
                f"ricevi. Rispondi SOLO con la traduzione: niente virgolette, niente note. Il testo "
                f"puo' essere una frase incompleta: traducila cosi' com'e'."},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2, "max_completion_tokens": 400,
    }
    req = urllib.request.Request(
        "https://api.cerebras.ai/v1/chat/completions", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()


def _t_apple(text: str) -> str:
    """Traduzione on-device di macOS. Nessuna rete, nessuna quota."""
    src = ACTIVE["src"] if ACTIVE["src"] != "auto" else "und"
    script = os.path.join(HERE, "apple_translate")
    if not os.path.exists(script):
        return ""
    r = subprocess.run([script, src, ACTIVE["dst"], text], capture_output=True,
                       text=True, timeout=20)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "apple translate ko").strip()[:80])
    return r.stdout.strip()


# MyMemory: 1.000 parole/giorno anonimo, 10.000 dichiarando un'email.
# Una sessione di conversazione brucia il tier anonimo in una ventina di minuti.
MM_EMAIL = os.environ.get("LT_EMAIL", "").strip()


def _t_mymemory(text: str) -> str:
    src = ACTIVE["src"] if ACTIVE["src"] != "auto" else "autodetect"
    url = ("https://api.mymemory.translated.net/get?q=" + urllib.parse.quote(text[:480])
           + f"&langpair={src}|{ACTIVE['dst']}")
    if MM_EMAIL:
        url += "&de=" + urllib.parse.quote(MM_EMAIL)
    req = urllib.request.Request(url, headers={"User-Agent": "live-translate/1.0"})
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.loads(r.read())
    out = ((data.get("responseData") or {}).get("translatedText") or "").strip()
    up = out.upper()
    if "MYMEMORY WARNING" in up or "QUERY LENGTH" in up or "ALL AVAILABLE FREE" in up:
        raise RuntimeError("429 quota mymemory esaurita")
    return out


# Argos gira in un venv separato (argostranslate non supporta il python 3.9 di
# sistema) e resta caldo su una porta locale: caricare i modelli costa ~10s la
# prima volta, ~0.7s per frase dopo. Senza rete e senza quote.
ARGOS_PORT = int(os.environ.get("LT_ARGOS_PORT", "8778"))


def _t_argos(text: str) -> str:
    req = urllib.request.Request(
        f"http://127.0.0.1:{ARGOS_PORT}/",
        data=json.dumps({"text": text, "src": ACTIVE["src"], "dst": ACTIVE["dst"]}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return (json.loads(r.read()).get("text") or "").strip()


def start_argos() -> None:
    """Avvia il server locale se il venv c'e' e non e' gia' in ascolto."""
    venv = os.path.join(HERE, ".venv", "bin", "python")
    script = os.path.join(HERE, "argos_translate.py")
    if not (os.path.exists(venv) and os.path.exists(script)):
        return
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{ARGOS_PORT}/", timeout=1)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        subprocess.Popen([venv, script, str(ARGOS_PORT)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


ACTIVE = {"src": "pt", "dst": "it"}
# argos serve una frase alla volta: senza questo lock l'anteprima e la
# traduzione definitiva si accodano e la seconda arriva con 5s di ritardo
tr_lock = threading.Lock()
final_pending = threading.Event()
# ordine: locale prima. Le API remote sono utili solo se la coppia manca in
# locale o se una chiave con credito da' una qualita' migliore.
BACKENDS = [("argos", _t_argos), ("cerebras", _t_llm), ("apple", _t_apple),
            ("mymemory", _t_mymemory)]
DEAD_FILE = os.path.join(HERE, ".backends_dead.json")
DEAD_TTL = 6 * 3600   # dopo sei ore si riprova: una ricarica va vista


def _load_dead() -> set:
    try:
        with open(DEAD_FILE) as fh:
            data = json.load(fh)
        now = time.time()
        return {k for k, ts in data.items() if now - ts < DEAD_TTL}
    except Exception:  # noqa: BLE001
        return set()


def _save_dead() -> None:
    try:
        now = time.time()
        with open(DEAD_FILE, "w") as fh:
            json.dump({k: now for k in _dead}, fh)
    except Exception:  # noqa: BLE001
        pass


_dead: set = _load_dead()


def translate(text: str, src_l: str = "", dst_l: str = ""):
    src_l = src_l or CFG["src"]
    dst_l = dst_l or CFG["dst"]
    ACTIVE.update(src=src_l, dst=dst_l)
    if src_l == dst_l:
        return text, "passthrough"
    errs = []
    for name, fn in BACKENDS:
        if name in _dead:
            continue
        try:
            out = fn(text)
            if out:
                return out, name
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if name == "argos":
                pass  # 404 = coppia non installata: si prova il prossimo, senza bruciarlo
            elif any(c in msg for c in ("402", "429", "403", "401")) or name == "apple":
                _dead.add(name)
                _save_dead()
            errs.append(f"{name}: {msg[:60]}")
    return f"[traduttore non disponibile — {'; '.join(errs)}]", "none"


# ------------------------------------------------------------------- pipeline
def stderr_thread(proc, my_gen: int) -> None:
    """whisper.cpp scrive la lingua rilevata su stderr, non su stdout."""
    for raw in proc.stderr:
        if stop_flag.is_set() or my_gen != gen:
            break
        m = DET_RE.search(raw)
        if m:
            with det_lock:
                det_lang.update(code=m.group(1), p=float(m.group(2)), at=time.time())


STABLE_AFTER = 1.1    # pausa dopo cui la frase e' considerata chiusa
MIN_WORDS = 3         # sotto questa soglia si aspetta: tradurre 'vezes do' non serve
PREVIEW_EVERY = 1.2   # intervallo minimo fra due traduzioni provvisorie
MAX_OPEN = 4.0        # oltre questo un segmento va chiuso comunque
MAX_WORDS_OPEN = 14   # ...o quando e' diventato abbastanza lungo da bastare


def stream_reader(proc, my_gen: int) -> None:
    """Modalita' sliding window: whisper riscrive la riga corrente man mano.

    Il testo appare a schermo a ogni revisione (~700ms), ma la traduzione parte
    solo quando la frase smette di cambiare: whisper riscrive costantemente
    ('vezes do' -> 'as vezes do trabalho dele' -> ...) e tradurre ogni
    revisione produce frammenti inutili e brucia quota.
    """
    state = {"cur": "", "seg": 0, "last_change": 0.0, "sent": "", "opened": 0.0}
    lock = threading.Lock()

    def flusher():
        """Decide quando una frase e' finita.

        La sola pausa non basta: con due persone che parlano senza respiro il
        silenzio di 1.1s non arriva mai, e in 25 secondi di conversazione
        veniva salvata una riga sola su 32 revisioni. Quindi si chiude anche
        per durata o per lunghezza, cosi' il flusso continuo viene comunque
        spezzato in frasi utilizzabili.
        """
        while not stop_flag.is_set() and my_gen == gen:
            time.sleep(0.2)
            with lock:
                cur, lc, sent = state["cur"], state["last_change"], state["sent"]
                opened = state["opened"]
                if not cur or cur == sent or not lc:
                    continue
                nwords = len(re.findall(r"[\wà-ÿ']+", cur))
                if nwords < MIN_WORDS:
                    continue
                quiet = time.time() - lc >= STABLE_AFTER
                too_long = time.time() - opened >= MAX_OPEN
                too_big = nwords >= MAX_WORDS_OPEN
                if not (quiet or too_long or too_big):
                    continue
                state["sent"] = cur
                seg = state["seg"]
                state["seg"] += 1
                state["cur"] = ""
                state["last_change"] = 0.0
                state["opened"] = 0.0
            emit_final(cur, my_gen, seg)

    threading.Thread(target=flusher, daemon=True).start()

    def previewer():
        """Traduce una versione provvisoria mentre la frase e' ancora aperta.

        Non sostituisce la traduzione finale: serve solo a non lasciare vuota
        la riga in diretta. Si limita a una richiesta ogni PREVIEW_EVERY
        secondi, altrimenti brucerebbe la quota del traduttore in un minuto.
        """
        last_txt, last_at = "", 0.0
        while not stop_flag.is_set() and my_gen == gen:
            time.sleep(0.3)
            if not CFG.get("preview", True):
                continue
            with lock:
                cur, seg = state["cur"], state["seg"]
            if not cur or cur == last_txt:
                continue
            if time.time() - last_at < PREVIEW_EVERY:
                continue
            if len(re.findall(r"[\wà-ÿ']+", cur)) < MIN_WORDS:
                continue
            if final_pending.is_set() or src_q.qsize() > 0:
                continue  # con frasi in attesa l'anteprima ruberebbe il turno
            last_txt, last_at = cur, time.time()
            sl, dl, _ = route(cur)
            if not tr_lock.acquire(blocking=False):
                continue
            try:
                out, backend = translate(cur, sl, dl)
            finally:
                tr_lock.release()
            if backend != "none" and not final_pending.is_set():
                broadcast({"type": "livedst", "id": f"{RUN_ID}.{my_gen}.{seg}", "dst": out})

    threading.Thread(target=previewer, daemon=True).start()

    buf = ""
    while not stop_flag.is_set() and my_gen == gen:
        ch = proc.stdout.read(1)
        if not ch:
            break
        if ch == "\r":
            buf = ""
            continue
        if ch == "\n":
            # whisper manda newline a ogni revisione della finestra, non a fine
            # frase: non e' un segnale di chiusura. Chiude solo il timer.
            txt = ANSI_ANY.sub("", buf).strip()
            buf = ""
            if not txt:
                continue
            with lock:
                if txt == state["cur"]:
                    continue
                state["cur"] = txt
                state["last_change"] = time.time()
                if not state["opened"]:
                    state["opened"] = time.time()
                seg = state["seg"]
            broadcast({"type": "live", "id": f"{RUN_ID}.{my_gen}.{seg}", "src": txt,
                       "t": time.strftime("%H:%M:%S")})
            continue
        buf += ch
        if ANSI_CLR.search(buf):
            txt = ANSI_ANY.sub("", buf).strip()
            buf = ""
            if not txt:
                continue
            with lock:
                if txt == state["cur"]:
                    continue
                state["cur"] = txt
                state["last_change"] = time.time()
                if not state["opened"]:
                    state["opened"] = time.time()
                seg = state["seg"]
            broadcast({"type": "live", "id": f"{RUN_ID}.{my_gen}.{seg}", "src": txt,
                       "t": time.strftime("%H:%M:%S")})
    if my_gen == gen and not stop_flag.is_set():
        broadcast({"type": "status", "state": "off", "text": "cattura interrotta"})


_last_final = {"text": ""}


def _dedup(text: str, prev: str) -> str:
    """Toglie da `text` la coda di `prev` che si ripete.

    In sliding window la finestra successiva contiene ancora parte della frase
    gia' chiusa, quindi senza questo la stessa mezza frase verrebbe salvata e
    tradotta due volte.
    """
    if not prev:
        return text
    pw, tw = prev.split(), text.split()
    if not pw or not tw:
        return text
    norm = lambda ws: [w.lower().strip(".,!?;:\"'…-") for w in ws]  # noqa: E731
    pn, tn = norm(pw), norm(tw)
    best = 0
    for k in range(min(len(pn), len(tn)), 2, -1):
        if pn[-k:] == tn[:k]:
            best = k
            break
    if not best:
        # whisper riformula mentre rivede ('tu morar sozinha' -> 'morar
        # sozinha'), quindi il confronto letterale non basta: se quasi tutte
        # le parole erano gia' nella riga precedente e' la stessa frase
        if len(tn) <= len(pn) + 2:
            common = len(set(tn) & set(pn))
            if common >= max(3, int(len(set(tn)) * 0.75)):
                return ""
        if len(tn) <= len(pn) and " ".join(tn) in " ".join(pn):
            return ""
        return text
    return " ".join(tw[best:]).strip()


def emit_final(text: str, my_gen: int, seg_id: int) -> None:
    t = text.strip()
    if not t or NOISE_RE.match(t) or t.lower().strip(" .!?,") in HALLUC:
        broadcast({"type": "drop", "id": f"{RUN_ID}.{my_gen}.{seg_id}"})
        return
    t = _dedup(t, _last_final["text"])
    if not t or len(re.findall(r"[\wà-ÿ']+", t)) < MIN_WORDS:
        broadcast({"type": "drop", "id": f"{RUN_ID}.{my_gen}.{seg_id}"})
        return
    _last_final["text"] = t
    src_q.put((t, my_gen, f"{RUN_ID}.{my_gen}.{seg_id}", time.time()))


def reader_thread(proc, my_gen: int) -> None:
    buf = []
    for raw in proc.stdout:
        if stop_flag.is_set() or my_gen != gen:
            break
        line = raw.rstrip("\n")
        if line.startswith("### Transcription"):
            if "START" in line:
                buf = []
            else:
                chunk = " ".join(buf).strip()
                buf = []
                if chunk and not NOISE_RE.match(chunk) \
                        and chunk.lower().strip(" .!?,") not in HALLUC:
                    src_q.put((chunk, my_gen, "", time.time()))
            continue
        m = TS_RE.match(line)
        if m and m.group(1).strip():
            piece = TAG_RE.sub(" ", m.group(1)).strip()
            if piece:
                buf.append(piece)
    if my_gen == gen and not stop_flag.is_set():
        broadcast({"type": "status", "state": "off", "text": "cattura interrotta"})


def translator_thread() -> None:
    last = ""
    seq = int(time.time() * 10) % 100000
    while not stop_flag.is_set():
        try:
            text, g, ext_id, queued_at = src_q.get(timeout=0.3)
        except queue.Empty:
            continue
        if g != gen or text == last:
            continue
        # se si accumula troppo, il ritardo diventa inutilizzabile: si tiene
        # il testo trascritto e si salta la traduzione delle piu' vecchie
        if src_q.qsize() > 4:
            broadcast({"type": "line", "id": ext_id or seq, "src": text,
                       "dst": "", "backend": "saltata", "ms": 0,
                       "t": time.strftime("%H:%M:%S"), "srcLang": CFG["src"],
                       "dstLang": CFG["dst"], "how": "coda", "part": ""},
                      save=True)
            continue
        last = text
        seq += 1
        sid = ext_id or seq
        # la riga si fissa subito con il testo originale: la traduzione la
        # riempie quando arriva. Prima nasceva solo a traduzione conclusa e,
        # con la coda accumulata, la frase spariva dalla vista prima di
        # comparire nella lista.
        broadcast({"type": "partial", "id": sid, "src": text,
                   "t": time.strftime("%H:%M:%S")})
        broadcast({"type": "status", "state": "busy", "text": "traduco"})

        # due voci accavallate finiscono in un chunk solo: se dentro ci sono
        # davvero due lingue, si traduce pezzo per pezzo
        blocks = [(text, "")]
        if CFG["bidi"]:
            blocks = split_by_language(text, CFG["langA"], CFG["langB"])

        for bi, (btext, blang) in enumerate(blocks):
            bid = sid if bi == 0 else f"{sid}.{bi}"
            if blang:
                src_l = blang
                dst_l = CFG["langB"] if blang == CFG["langA"] else CFG["langA"]
                how = "misto"
            else:
                src_l, dst_l, how = route(btext)
            if len(blocks) > 1 and bi > 0:
                broadcast({"type": "partial", "id": bid, "src": btext,
                           "t": time.strftime("%H:%M:%S")})
            if how.startswith("scartato"):
                continue
            t0 = time.time()
            final_pending.set()
            try:
                with tr_lock:
                    out, backend = translate(btext, src_l, dst_l)
            finally:
                final_pending.clear()
            if backend == "none":
                broadcast({"type": "status", "state": "off", "text": "traduttore ko"})
                broadcast({"type": "drop", "id": bid})
                continue
            broadcast({"type": "line", "id": bid, "src": btext, "dst": out, "backend": backend,
                       "ms": int((time.time() - t0) * 1000), "t": time.strftime("%H:%M:%S"),
                       "queue": src_q.qsize(), "lag": round(time.time() - queued_at, 1),
                       "srcLang": src_l, "dstLang": dst_l, "how": how,
                       "part": f"{bi + 1}/{len(blocks)}" if len(blocks) > 1 else ""},
                      save=True)
            if CFG["tts"]:
                speak(out, dst_l)
        broadcast({"type": "status", "state": "live", "text": "in ascolto"})


def route(text: str):
    """Sceglie la direzione della traduzione. Ritorna (src, dst, come_e_stato_deciso)."""
    if not CFG["bidi"]:
        return CFG["src"], CFG["dst"], "fisso"
    a, b = CFG["langA"], CFG["langB"]
    # 1. le stopword sono piu' affidabili di whisper sulle frasi corte, ma servono >=4 parole
    g = guess_lang(text, (a, b))
    if g:
        return (a, b, "parole") if g == a else (b, a, "parole")
    # 2. altrimenti la lingua che whisper ha rilevato per questo chunk, se e' una delle due
    with det_lock:
        code, prob, at = det_lang["code"], det_lang["p"], det_lang["at"]
    if code in (a, b) and prob >= 0.5 and time.time() - at < 25:
        return (a, b, f"whisper {prob:.0%}") if code == a else (b, a, f"whisper {prob:.0%}")
    # 3. whisper puo' cadere su una lingua terza sul rumore ('Bez bez beri yapı dar'
    #    rilevato turco): quel chunk non e' parlato utile, non va tradotto
    if code and code not in (a, b) and prob >= 0.5 and time.time() - at < 25:
        return a, b, f"scartato:{code}"
    # 4. in dubbio si tiene la direzione principale: meglio non tradurre che invertire a caso
    return a, b, "default"


def start_whisper() -> None:
    global whisper_proc, gen
    model_path = os.path.join(HERE, "models", MODEL_FILE.get(CFG["model"], "ggml-small.bin"))
    if not os.path.exists(model_path):
        broadcast({"type": "status", "state": "off", "text": "modello mancante"})
        return
    with proc_lock:
        gen += 1
        my_gen = gen
        if whisper_proc and whisper_proc.poll() is None:
            whisper_proc.send_signal(signal.SIGINT)
            try:
                whisper_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                whisper_proc.kill()
        # M2 Max: 8 core performance, whisper.cpp gira su Metal per l'encoder
        # in bidirezionale whisper deve poter riconoscere entrambe le lingue
        lang = "auto" if CFG["bidi"] else CFG["src"]
        if CFG["stream"]:
            # sliding window: riscrive la riga ogni 700ms invece di aspettare
            # la fine della frase. Il testo compare mentre si parla.
            cmd = [WHISPER, "-m", model_path, "-l", lang, "--step", "700",
                   "--length", "5000", "--keep", "250",
                   "-c", CFG["capture"], "-t", "8"]
        else:
            cmd = [WHISPER, "-m", model_path, "-l", lang, "--step", "0", "--length", "6000",
                   "-vth", "0.6", "-c", CFG["capture"], "-t", "8"]
            if not CFG["bidi"]:
                cmd.append("-kc")  # aiuta, ma su due lingue alternate confonde
        whisper_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, bufsize=1)
        target = stream_reader if CFG["stream"] else reader_thread
        threading.Thread(target=target, args=(whisper_proc, my_gen), daemon=True).start()
        threading.Thread(target=stderr_thread, args=(whisper_proc, my_gen), daemon=True).start()
    broadcast({"type": "cfg", **CFG})
    broadcast({"type": "status", "state": "live", "text": "in ascolto"})


# ---------------------------------------------------------------------- voce
# `say` e' gia' nel sistema e ha voci native per tutte le lingue in elenco.
VOICES = {"it": "Alice", "pt": "Luciana", "en": "Samantha", "es": "Monica",
          "fr": "Amelie", "de": "Anna", "nl": "Xander", "ru": "Milena",
          "pl": "Zosia", "tr": "Yelda", "sv": "Alva", "da": "Sara",
          "ro": "Ioana", "el": "Melina", "cs": "Zuzana", "he": "Carmit",
          "hu": "Mariska", "th": "Kanya", "ar": "Maged", "zh": "Tingting",
          "ja": "Kyoko", "ko": "Yuna", "hi": "Lekha", "id": "Damayanti",
          "fi": "Satu", "no": "Nora", "vi": "Linh", "uk": "Lesya", "ca": "Montse"}
say_proc = None
say_lock = threading.Lock()


def speak(text: str, lang: str, interrupt: bool = True) -> bool:
    """Legge il testo ad alta voce. Ritorna False se la lingua non ha una voce."""
    global say_proc
    voice = VOICES.get(lang)
    if not voice or not text.strip():
        return False
    with say_lock:
        # una frase nuova annulla quella in corso: in conversazione la voce
        # deve stare dietro al parlato, non accodare minuti di ritardo
        if interrupt and say_proc and say_proc.poll() is None:
            say_proc.terminate()
        try:
            say_proc = subprocess.Popen(
                ["say", "-v", voice, "-r", str(CFG.get("rate", 210)), text[:600]],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            return False
    return True


def set_mic(vol: int) -> None:
    vol = max(0, min(100, int(vol)))
    CFG["mic"] = vol
    subprocess.run(["osascript", "-e", f"set volume input volume {vol}"],
                   capture_output=True)


def get_mic() -> int:
    r = subprocess.run(["osascript", "-e", "input volume of (get volume settings)"],
                       capture_output=True, text=True)
    try:
        return int(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return CFG["mic"]


# ------------------------------------------------------------------------ web
PAGE = r"""<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live translate</title>
<style>
 *{box-sizing:border-box}
 html,body{height:100%}
 body{margin:0;background:#07070b;color:#fff;overflow:hidden;
      font:16px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",Inter,sans-serif;
      display:flex;flex-direction:column}
 /* striscia superiore libera: sotto ci stanno i semafori di macOS, e qualunque
    controllo messo li' si accavalla con la gestione finestre di sistema */
 #drag{flex:0 0 auto;height:26px;-webkit-app-region:drag}
 header{display:flex;align-items:center;gap:8px;padding:6px 14px 9px;
        border-bottom:1px solid #16171f;flex:0 0 auto;flex-wrap:wrap;
        -webkit-app-region:drag}
 header select,header button,header .mic{-webkit-app-region:no-drag}
 .dot{width:9px;height:9px;border-radius:50%;background:#7de08d;box-shadow:0 0 10px #7de08d;
      transition:.2s;flex:0 0 auto}
 .dot.busy{background:#ffd166;box-shadow:0 0 10px #ffd166}
 .dot.off{background:#ff5c5c;box-shadow:0 0 10px #ff5c5c}
 #st{font-size:12.5px;color:#8a92a6;min-width:74px}
 .sp{flex:1}
 select,button{background:#111219;color:#c9d1e0;border:1px solid #23252f;border-radius:7px;
        padding:5px 8px;font-size:12.5px;font-family:inherit;cursor:pointer;outline:none}
 select:hover,button:hover{border-color:#3a3d4d;color:#fff}
 button.on{color:#4c8dff;border-color:#2b3a5c;background:#0e1626}
 /* icone disegnate, non emoji: le emoji cambiano faccia a ogni sistema */
 button.ico{padding:5px 7px;line-height:0}
 button.ico svg{width:15px;height:15px;fill:none;stroke:currentColor;
   stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round;display:block}
 button.ico.on svg{stroke:#4c8dff}
 .arrow{color:#4c5265;font-size:13px}
 /* VU meter */
 .mic{display:flex;align-items:center;gap:7px;background:#0d0e14;border:1px solid #1c1e26;
      border-radius:7px;padding:4px 9px}
 .bars{display:flex;gap:2px;align-items:flex-end;height:15px;width:52px}
 .bars i{flex:1;background:#22252f;border-radius:1px;height:22%;transition:height .07s,background .12s}
 .bars i.a{background:#4ade80}.bars i.b{background:#facc15}.bars i.c{background:#f87171}
 #vol{width:64px;accent-color:#4c8dff;cursor:pointer}
 #volv{font-size:11px;color:#5c6478;min-width:26px;text-align:right;font-variant-numeric:tabular-nums}
 /* la frase in corso e' l'ultima riga della lista, non una fascia separata:
    un solo punto dove guardare, e la riga diventa definitiva sul posto */
 #livewrap{display:none;border-left-color:#4c8dff;margin-top:15px}
 #livewrap.on{display:block}
 #liveit{color:#8fb4ff}
 #liveit:empty{display:none}
 #live{font-style:italic}
 #live b{color:#c3cad6;font-style:normal;font-weight:600}
 #caret{width:6px;height:11px;background:#4c8dff;display:inline-block;
   animation:blink 1s steps(2) infinite;border-radius:1px;vertical-align:-1px}
 @keyframes blink{50%{opacity:0}}
 main{flex:1;overflow-y:auto;padding:16px 20px 10px}
 #log{display:flex;flex-direction:column;gap:15px}
 .row{border-left:2px solid #1c1e28;padding-left:13px;animation:in .22s ease}
 .row.top{border-left-color:#4c8dff}
 .row.top .it{color:#fff}
 /* in attesa si legge l'originale, in grigio: il testo c'e' sempre */
 .row.wait .it{color:#7b8394;font-weight:500}
 .row.skip .it{color:#8a92a6}
 .row.skip .meta{color:#7a5a35}
 .row.old{opacity:.72}
 @keyframes in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
 .sep{font-size:10.5px;color:#3a3f4d;text-transform:uppercase;letter-spacing:.09em;
      border-top:1px dashed #1c1e28;padding-top:9px;margin-top:3px}
 .it{font-size:27px;font-weight:600;letter-spacing:-.015em;color:#b9c2d4;margin-bottom:3px}
 .pt{font-size:13.5px;color:#697082}
 .meta{font-size:10.5px;color:#383d4a;margin-top:3px;letter-spacing:.02em}
 footer{flex:0 0 auto;padding:6px 14px 10px;color:#383d4a;font-size:10.5px;
        border-top:1px solid #101119;display:flex;gap:12px;align-items:center}
 footer a{color:#4a5164;text-decoration:none;cursor:pointer}
 footer a:hover{color:#8a92a6}
 main::-webkit-scrollbar{width:8px}
 main::-webkit-scrollbar-thumb{background:#1a1c24;border-radius:4px}
 body.big .it{font-size:40px}
 body.big .pt,body.big .meta{display:none}
 /* in bidirezionale il verso opposto e' rientrato e di un altro colore */
 body.bidi .row.rev{border-left-color:#c084fc;margin-left:38px}
 body.bidi .row.rev.top{border-left-color:#e0a3ff}
 body.bidi .row.rev .it{color:#e9d5ff}
 .row{position:relative}
 .row .say{position:absolute;right:2px;top:2px;opacity:0;padding:3px 5px;
   background:transparent;border-color:transparent;transition:opacity .15s;line-height:0}
 .row .say svg{width:13px;height:13px;fill:none;stroke:currentColor;stroke-width:1.5;
   stroke-linecap:round;stroke-linejoin:round;display:block}
 .row:hover .say{opacity:.5}
 .row .say:hover{opacity:1;color:#4c8dff}
 .row.part{border-left-style:dashed}
 .row.part .it{font-size:23px}
 body.bidi .row.rev.top .it{color:#faf5ff}
</style></head><body>
<div id="drag"></div>
<header>
 <span class="dot" id="d"></span><span id="st">connessione…</span>
 <select id="src" title="lingua parlata"></select>
 <span class="arrow">&rarr;</span>
 <select id="dst" title="traduci in"></select>
 <button id="swap" class="ico" title="inverti le lingue">
  <svg viewBox="0 0 16 16"><path d="M2 5h9M8.5 2 11.5 5 8.5 8M14 11H5M7.5 8 4.5 11 7.5 14"/></svg>
 </button>
 <select id="mdl" title="modello di trascrizione"></select>
 <div class="mic" title="livello e volume del microfono">
   <div class="bars" id="bars"></div>
   <input type="range" id="vol" min="0" max="100" step="1">
   <span id="volv">&mdash;</span>
 </div>
 <span class="sp"></span>
 <button id="strm" class="ico" title="trascrive mentre parli, invece di attendere la fine frase">
  <svg viewBox="0 0 16 16"><path d="M2 8h2l2-5 2.5 10L11 8h3"/></svg>
 </button>
 <button id="bidi" class="ico" title="bidirezionale: riconosce la lingua e traduce nel verso giusto">
  <svg viewBox="0 0 16 16"><path d="M2 5.5h9M8.5 3 11 5.5 8.5 8M14 10.5H5M7.5 8 5 10.5 7.5 13"/></svg>
 </button>
 <button id="big" class="ico" title="testo grande, senza originale">
  <svg viewBox="0 0 16 16"><path d="M1.5 13 5 3l3.5 10M2.8 10h4.4M10 13l2.2-6 2.3 6M10.8 11h2.9"/></svg>
 </button>
 <button id="tts" class="ico" title="leggi ad alta voce ogni traduzione">
  <svg viewBox="0 0 16 16"><path d="M8 2.5 4.5 5.5H2v5h2.5L8 13.5zM11 6a3 3 0 0 1 0 4M13 4a6 6 0 0 1 0 8"/></svg>
 </button>
 <button id="pin" class="ico" title="tieni la finestra sopra tutte le altre">
  <svg viewBox="0 0 16 16"><path d="M6 1.5h4l-.6 4 2.6 2.2v1.1H4v-1.1L6.6 5.5zM8 8.8V14.5"/></svg>
 </button>
</header>
<main id="m"><div id="log"></div>
 <div id="livewrap" class="row live"><div class="it" id="liveit"></div>
  <div class="pt" id="live"></div><div class="meta"><span id="caret"></span></div></div>
</main>
<footer><span id="bk">—</span><span class="sp"></span>
 <a id="save">salva .txt</a><a id="clr">pulisci vista</a>
 <span>whisper.cpp locale · cronologia su disco</span></footer>
<script>
const $=i=>document.getElementById(i);
// resta agganciato in fondo, ma solo finche' l'utente non scorre indietro
// per rileggere: in quel caso le righe nuove non devono strappargli la vista
let stick=true;
const log=$('log'),d=$('d'),st=$('st'),bk=$('bk'),selS=$('src'),selD=$('dst'),selM=$('mdl');
let rows=new Map(),LS=[],LD=[],collide=0;

$('m').addEventListener('scroll',()=>{
  const el=$('m');stick=el.scrollHeight-el.scrollTop-el.clientHeight<40;});

for(let i=0;i<9;i++)$('bars').appendChild(document.createElement('i'));
const bars=[...$('bars').children];

function fill(sel,list,cur){sel.innerHTML='';list.forEach(([c,n])=>{
  const o=document.createElement('option');o.value=c;o.textContent=n;
  if(c===cur)o.selected=true;sel.appendChild(o);});}

function rowEl(e,old){
  let r=rows.get(e.id);
  if(r&&!old&&r.dataset.hist==='1'){r=null;e.id=e.id+'#'+(++collide);}
  if(!r){
    r=document.createElement('div');r.className='row wait'+(old?' old':'');
    r.innerHTML='<div class="it"></div><div class="pt"></div>'+
      '<div class="meta"></div><button class="say" title="leggi questa riga">'+
      '<svg viewBox="0 0 16 16"><path d="M8 2.5 4.5 5.5H2v5h2.5L8 13.5zM11 6a3 3 0 0 1 0 4"/>'+
      '</svg></button>';
    r.querySelector('.say').onclick=ev=>{ev.stopPropagation();
      const d=rows.get(e.id); if(!d)return;
      fetch('/speak',{method:'POST',body:JSON.stringify(
        {text:d.dataset.dst||'',lang:d.dataset.dstlang||''})});};
    if(old)r.dataset.hist='1';
    log.appendChild(r);rows.set(e.id,r);
    if(!old){[...log.children].forEach(c=>c.classList.remove('top'));r.classList.add('top');
             r.classList.remove('old');liveToEnd();scrollDown();}
    while(log.children.length>200){
      const g=log.firstChild;
      for(const [k,v] of rows) if(v===g){rows.delete(k);break;}
      log.removeChild(g);}
  }
  return r;
}
function partial(e){const r=rowEl(e);
  r.querySelector('.it').textContent=e.src;   // l'originale finche' non traduce
  r.querySelector('.pt').textContent='';
  r.querySelector('.meta').textContent=e.t+' · traduco…';
  liveToEnd();scrollDown();}
function line(e,old){const r=rowEl(e,old);r.classList.remove('wait');
  const skipped=!e.dst;
  r.classList.toggle('skip',skipped);
  r.querySelector('.it').textContent=skipped?e.src:e.dst;
  r.querySelector('.pt').textContent=skipped?'':e.src;
  r.querySelector('.meta').textContent=skipped
    ? e.t+' · non tradotta (traduttore in ritardo)'
    : e.t+' · '+e.srcLang+'→'+e.dstLang
      +(e.how&&e.how!=='fisso'?' ('+e.how+')':'')+(e.part?' · pezzo '+e.part:'')
      +' · '+e.backend+' · '+e.ms+'ms';
  if(e.part)r.classList.add('part');
  r.dataset.dir=e.srcLang;r.dataset.dst=e.dst;r.dataset.dstlang=e.dstLang;
  if(e.srcLang===selD.value&&document.body.classList.contains('bidi'))r.classList.add('rev');
  if(!old)bk.textContent=e.backend;}
function post(b){return fetch('/cfg',{method:'POST',body:JSON.stringify(b)});}

selS.onchange=()=>post({src:selS.value});
selD.onchange=()=>post({dst:selD.value});
selM.onchange=()=>post({model:selM.value});
$('swap').onclick=()=>{if(selD.value!=='auto')post({src:selD.value,dst:selS.value==='auto'?'it':selS.value});};
$('bidi').onclick=()=>post({bidi:!document.body.classList.contains('bidi')});
$('strm').onclick=()=>post({stream:!$('strm').classList.contains('on')});
$('tts').onclick=()=>post({tts:!$('tts').classList.contains('on')});
$('pin').onclick=()=>{const on=!$('pin').classList.contains('on');
  $('pin').classList.toggle('on',on);
  if(window.webkit?.messageHandlers?.host)
    window.webkit.messageHandlers.host.postMessage({cmd:'pin',on:on});};
window.__setPin=on=>$('pin').classList.toggle('on',!!on);

// riga in diretta: il testo cresce mentre whisper rivede il segmento
let liveId=null,liveTxt='';
function scrollDown(){if(stick)requestAnimationFrame(()=>{
  const m=$('m');m.scrollTop=m.scrollHeight;});}
function liveShow(e){
  const w=$('livewrap');
  if(!w.classList.contains('on')){w.classList.add('on');scrollDown();}
  liveId=e.id;
  // le parole nuove rispetto alla revisione precedente vanno in evidenza
  const old=liveTxt.split(/\s+/),now=e.src.split(/\s+/);
  let i=0;while(i<old.length&&i<now.length&&old[i]===now[i])i++;
  $('live').innerHTML=now.slice(0,i).join(' ')+(i<now.length?' <b>'+
    now.slice(i).join(' ').replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))+'</b>':'');
  liveTxt=e.src;
  scrollDown();
}
function liveClear(id){if(liveId===id||!id){$('livewrap').classList.remove('on');
  $('live').textContent='';$('liveit').textContent='';liveTxt='';liveId=null;}}
// la riga in corso resta sempre in fondo, sotto l'ultima frase conclusa
function liveToEnd(){const m=$('m');m.appendChild($('livewrap'));}
function liveTrans(e){if(liveId===e.id)$('liveit').textContent=e.dst;}
$('big').onclick=e=>{document.body.classList.toggle('big');e.target.classList.toggle('on');};
$('clr').onclick=()=>{log.innerHTML='';rows.clear();};
$('save').onclick=()=>{window.open('/export','_blank');};

let volT;
$('vol').oninput=e=>{$('volv').textContent=e.target.value;
  clearTimeout(volT);volT=setTimeout(()=>post({mic:+e.target.value}),160);};

// VU meter: stream indipendente, non contende il device a whisper
navigator.mediaDevices?.getUserMedia({audio:{echoCancellation:false,autoGainControl:false,
    noiseSuppression:false}}).then(s=>{
  const ac=new AudioContext(),an=ac.createAnalyser();
  an.fftSize=1024;an.smoothingTimeConstant=.55;
  ac.createMediaStreamSource(s).connect(an);
  const buf=new Uint8Array(an.fftSize);
  (function tick(){
    an.getByteTimeDomainData(buf);
    let p=0;for(let i=0;i<buf.length;i++){const v=Math.abs(buf[i]-128);if(v>p)p=v;}
    const lvl=Math.min(1,p/70);
    bars.forEach((b,i)=>{const on=lvl>(i+.6)/bars.length;
      b.style.height=(on?22+78*Math.min(1,(lvl-(i/bars.length))*3.2):22)+'%';
      b.className=on?(i>6?'c':i>4?'b':'a'):'';});
    requestAnimationFrame(tick);})();
}).catch(()=>{$('bars').style.opacity=.25;$('bars').title='permesso microfono negato al browser';});

fetch('/langs').then(r=>r.json()).then(j=>{
  LS=j.langs;LD=j.dstLangs;
  fill(selS,LS,j.src);fill(selD,LD,j.dst);
  fill(selM,j.models.map(m=>[m[0],m[1]]),j.model);
  $('vol').value=j.mic;$('volv').textContent=j.mic;
  document.body.classList.toggle('bidi',!!j.bidi);
  $('bidi').classList.toggle('on',!!j.bidi);
  $('strm').classList.toggle('on',!!j.stream);
  $('tts').classList.toggle('on',!!j.tts);
  if(!window.webkit?.messageHandlers?.host)$('pin').style.display='none';});

fetch('/history').then(r=>r.json()).then(j=>{
  if(!j.rows.length)return;
  j.rows.forEach(e=>line(e,true));
  const s=document.createElement('div');s.className='sep';
  s.textContent='— righe precedenti sopra · nuove qui sotto —';
  log.appendChild(s);
  liveToEnd();requestAnimationFrame(()=>{$('m').scrollTop=$('m').scrollHeight;});});

const es=new EventSource('/events');
es.onmessage=ev=>{const e=JSON.parse(ev.data);
  if(e.type==='line'){line(e);liveClear(e.id);}
  else if(e.type==='live')liveShow(e);
  else if(e.type==='livedst')liveTrans(e);
  else if(e.type==='drop')liveClear(e.id);
  else if(e.type==='partial')partial(e);
  else if(e.type==='cfg'){fill(selS,LS,e.src);fill(selD,LD,e.dst);
    if(selM.options.length)selM.value=e.model;
    $('vol').value=e.mic;$('volv').textContent=e.mic;
    document.body.classList.toggle('bidi',!!e.bidi);
    $('bidi').classList.toggle('on',!!e.bidi);
    $('strm').classList.toggle('on',!!e.stream);
    $('tts').classList.toggle('on',!!e.tts);
    if(!e.stream)liveClear();
    selS.disabled=false;$('swap').style.opacity=e.bidi?.35:1;}
  else if(e.type==='status'){d.className='dot '+(e.state==='live'?'':e.state);st.textContent=e.text;}
};
es.onerror=()=>{d.className='dot off';st.textContent='disconnesso';};
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/speak":
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n) or b"{}")
            except Exception:  # noqa: BLE001
                b = {}
            ok = speak(b.get("text", ""), b.get("lang", CFG["dst"]))
            self._json({"ok": ok})
            return
        if self.path != "/cfg":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            body = {}
        restart = False
        if body.get("bidi") is not None and bool(body["bidi"]) != CFG["bidi"]:
            CFG["bidi"] = bool(body["bidi"])
            if CFG["bidi"]:
                # le due lingue del dialogo sono quelle scelte nei menu
                CFG["langA"] = CFG["src"] if CFG["src"] != "auto" else "pt"
                CFG["langB"] = CFG["dst"]
            restart = True  # -l auto contro -l <lingua>: whisper va riavviato
        if body.get("src") and body["src"] != CFG["src"]:
            CFG["src"] = body["src"]
            CFG["langA"] = body["src"] if body["src"] != "auto" else CFG["langA"]
            restart = True
        if body.get("dst") and body["dst"] != CFG["dst"]:
            CFG["dst"] = body["dst"]
            CFG["langB"] = body["dst"]
            _dead.discard("apple")  # la coppia e' cambiata: ridai una chance all'on-device
        if body.get("stream") is not None and bool(body["stream"]) != CFG["stream"]:
            CFG["stream"] = bool(body["stream"])
            restart = True
        if body.get("model") and body["model"] != CFG["model"]:
            CFG["model"] = body["model"]
            restart = True
        if body.get("mic") is not None:
            set_mic(body["mic"])
        if body.get("tts") is not None:
            CFG["tts"] = bool(body["tts"])
            if not CFG["tts"]:
                with say_lock:
                    if say_proc and say_proc.poll() is None:
                        say_proc.terminate()
        if body.get("rate") is not None:
            CFG["rate"] = max(120, min(320, int(body["rate"])))
        self._json({"ok": True, **CFG})
        if restart:
            broadcast({"type": "status", "state": "busy", "text": "riavvio…"})
            threading.Thread(target=start_whisper, daemon=True).start()
        else:
            broadcast({"type": "cfg", **CFG})

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/langs":
            self._json({"langs": LANGS, "dstLangs": DST_LANGS,
                        "models": [[k, lbl, f] for k, lbl, f in MODELS
                                   if os.path.exists(os.path.join(HERE, "models", f))],
                        "src": CFG["src"], "dst": CFG["dst"],
                        "model": CFG["model"], "mic": get_mic(),
                        "bidi": CFG["bidi"], "stream": CFG["stream"],
                        "tts": CFG["tts"], "rate": CFG["rate"],
                        "voices": sorted(VOICES)})
            return
        if self.path == "/history":
            self._json({"rows": load_recent(400)})
            return
        if self.path == "/export":
            rows = load_recent(500)
            txt = "\n".join(f"[{r.get('t','')}] {r.get('dst','')}\n           {r.get('src','')}"
                            for r in rows)
            body = ("Trascrizione live-translate\n" + "=" * 40 + "\n\n" + txt).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="live-translate.txt"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q: queue.Queue = queue.Queue(maxsize=400)
            with sub_lock:
                subscribers.append(q)
            try:
                self._ev({"type": "cfg", **CFG})
                self._ev({"type": "status", "state": "live", "text": "in ascolto"})
                while not stop_flag.is_set():
                    try:
                        self._ev(q.get(timeout=10))
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except Exception:  # noqa: BLE001
                pass
            finally:
                with sub_lock:
                    if q in subscribers:
                        subscribers.remove(q)
            return
        self.send_error(404)

    def _ev(self, evt):
        self.wfile.write(b"data: " + json.dumps(evt, ensure_ascii=False).encode() + b"\n\n")
        self.wfile.flush()


def open_window(url: str) -> None:
    for app in ("Google Chrome", "Microsoft Edge", "Brave Browser"):
        r = subprocess.run(["open", "-na", app, "--args", f"--app={url}",
                            "--window-size=1020,470", "--window-position=340,520"],
                           capture_output=True)
        if r.returncode == 0:
            return
    subprocess.run(["open", url])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--src", default="pt", help="lingua parlata, o 'auto'")
    ap.add_argument("--dst", default="it")
    ap.add_argument("--capture", default="1", help="indice device avfoundation")
    ap.add_argument("--model", default="turbo", choices=[k for k, _, _ in MODELS])
    ap.add_argument("--mic", type=int, default=None, help="volume microfono 0-100")
    ap.add_argument("--bidi", action="store_true",
                    help="bidirezionale: rileva la lingua e traduce nel verso giusto")
    ap.add_argument("--tts", action="store_true",
                    help="legge ad alta voce ogni traduzione")
    ap.add_argument("--no-stream", action="store_true",
                    help="attende la fine frase invece di trascrivere mentre si parla")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    CFG.update(src=args.src, dst=args.dst, model=args.model, capture=args.capture,
               bidi=args.bidi, langA=args.src if args.src != "auto" else "pt", langB=args.dst,
               stream=not args.no_stream, tts=args.tts)

    if not os.path.exists(WHISPER):
        print("manca whisper-stream: brew install whisper-cpp", file=sys.stderr)
        return 2
    if args.mic is not None:
        set_mic(args.mic)
    else:
        CFG["mic"] = get_mic()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    start_argos()
    threading.Thread(target=translator_thread, daemon=True).start()
    start_whisper()

    url = f"http://127.0.0.1:{args.port}/"
    print(f"overlay: {url}   {CFG['src']} -> {CFG['dst']}   modello {CFG['model']}")
    print(f"cronologia: {SESSION_FILE}")
    if not args.no_open:
        open_window(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    stop_flag.set()
    if whisper_proc and whisper_proc.poll() is None:
        whisper_proc.send_signal(signal.SIGINT)
        try:
            whisper_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            whisper_proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
