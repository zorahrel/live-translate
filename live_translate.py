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
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
WHISPER = "/opt/homebrew/bin/whisper-stream"
HIST_DIR = os.path.join(HERE, "history")
os.makedirs(HIST_DIR, exist_ok=True)

ENV_FILE = os.path.expanduser("~/.claude/jarvis/router/.env")
API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
if not API_KEY and os.path.exists(ENV_FILE):
    with open(ENV_FILE) as fh:
        for line in fh:
            if line.startswith("CEREBRAS_API_KEY="):
                API_KEY = line.split("=", 1)[1].strip()
                break

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
       "bidi": False, "langA": "pt", "langB": "it"}

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


def guess_lang(text: str, candidates) -> str:
    """Rilevamento a stopword fra le sole lingue candidate. '' se e' indeciso."""
    words = re.findall(r"[a-zà-ÿ]+", text.lower())
    if len(words) < 4:
        return ""
    scores = {}
    for c in candidates:
        sw = STOPWORDS.get(c)
        if sw:
            scores[c] = sum(1 for w in words if w in sw)
    if not scores:
        return ""
    best = max(scores, key=scores.get)
    ranked = sorted(scores.values(), reverse=True)
    if ranked[0] == 0 or (len(ranked) > 1 and ranked[0] - ranked[1] < 2):
        return ""
    return best
src_q: "queue.Queue" = queue.Queue()
subscribers: "list" = []
sub_lock = threading.Lock()
stop_flag = threading.Event()
history: "list" = []
proc_lock = threading.Lock()
whisper_proc = None
gen = 0

SESSION_FILE = os.path.join(HIST_DIR, time.strftime("%Y-%m-%d_%H%M%S") + ".jsonl")
hist_lock = threading.Lock()


def persist(evt: dict) -> None:
    with hist_lock:
        with open(SESSION_FILE, "a") as fh:
            fh.write(json.dumps(evt, ensure_ascii=False) + "\n")


def load_recent(limit: int = 80) -> list:
    """Righe delle sessioni precedenti, dalla piu' vecchia alla piu' recente."""
    files = sorted(f for f in os.listdir(HIST_DIR) if f.endswith(".jsonl"))
    out: list = []
    for name in reversed(files):
        try:
            with open(os.path.join(HIST_DIR, name)) as fh:
                rows = [json.loads(l) for l in fh if l.strip()]
        except Exception:  # noqa: BLE001
            continue
        out = rows + out
        if len(out) >= limit:
            break
    return out[-limit:]


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


def _t_mymemory(text: str) -> str:
    src = ACTIVE["src"] if ACTIVE["src"] != "auto" else "autodetect"
    url = ("https://api.mymemory.translated.net/get?q=" + urllib.parse.quote(text[:480])
           + f"&langpair={src}|{ACTIVE['dst']}")
    req = urllib.request.Request(url, headers={"User-Agent": "live-translate/1.0"})
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.loads(r.read())
    out = ((data.get("responseData") or {}).get("translatedText") or "").strip()
    if "MYMEMORY WARNING" in out.upper() or "QUERY LENGTH" in out.upper():
        raise RuntimeError(out[:90])
    return out


ACTIVE = {"src": "pt", "dst": "it"}
BACKENDS = [("cerebras", _t_llm), ("apple", _t_apple), ("mymemory", _t_mymemory)]
_dead: set = set()


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
            if any(c in msg for c in ("402", "429", "403", "401")) or name == "apple":
                _dead.add(name)
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
                    src_q.put((chunk, my_gen))
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
            text, g = src_q.get(timeout=0.3)
        except queue.Empty:
            continue
        if g != gen or text == last:
            continue
        last = text
        seq += 1
        sid = seq
        src_l, dst_l, how = route(text)
        broadcast({"type": "partial", "id": sid, "src": text, "srcLang": src_l,
                   "dstLang": dst_l, "t": time.strftime("%H:%M:%S")})
        broadcast({"type": "status", "state": "busy", "text": "traduco"})
        t0 = time.time()
        out, backend = translate(text, src_l, dst_l)
        broadcast({"type": "line", "id": sid, "src": text, "dst": out, "backend": backend,
                   "ms": int((time.time() - t0) * 1000), "t": time.strftime("%H:%M:%S"),
                   "srcLang": src_l, "dstLang": dst_l, "how": how}, save=True)
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
    # 3. in dubbio si tiene la direzione principale: meglio non tradurre che invertire a caso
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
        cmd = [WHISPER, "-m", model_path, "-l", lang, "--step", "0", "--length", "6000",
               "-vth", "0.6", "-c", CFG["capture"], "-t", "8"]
        if not CFG["bidi"]:
            cmd.append("-kc")  # il contesto aiuta, ma su due lingue alternate confonde
        whisper_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, bufsize=1)
        threading.Thread(target=reader_thread, args=(whisper_proc, my_gen), daemon=True).start()
        threading.Thread(target=stderr_thread, args=(whisper_proc, my_gen), daemon=True).start()
    broadcast({"type": "cfg", **CFG})
    broadcast({"type": "status", "state": "live", "text": "in ascolto"})


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
 header{display:flex;align-items:center;gap:9px;padding:9px 14px;border-bottom:1px solid #16171f;
        flex:0 0 auto;flex-wrap:wrap}
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
 .arrow{color:#4c5265;font-size:13px}
 /* VU meter */
 .mic{display:flex;align-items:center;gap:7px;background:#0d0e14;border:1px solid #1c1e26;
      border-radius:7px;padding:4px 9px}
 .bars{display:flex;gap:2px;align-items:flex-end;height:15px;width:52px}
 .bars i{flex:1;background:#22252f;border-radius:1px;height:22%;transition:height .07s,background .12s}
 .bars i.a{background:#4ade80}.bars i.b{background:#facc15}.bars i.c{background:#f87171}
 #vol{width:64px;accent-color:#4c8dff;cursor:pointer}
 #volv{font-size:11px;color:#5c6478;min-width:26px;text-align:right;font-variant-numeric:tabular-nums}
 main{flex:1;overflow-y:auto;padding:16px 20px 10px}
 #log{display:flex;flex-direction:column-reverse;gap:15px}
 .row{border-left:2px solid #1c1e28;padding-left:13px;animation:in .22s ease}
 .row.top{border-left-color:#4c8dff}
 .row.top .it{color:#fff}
 .row.wait .it{color:#4e5464}
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
 body.bidi .row.rev.top .it{color:#faf5ff}
</style></head><body>
<header>
 <span class="dot" id="d"></span><span id="st">connessione…</span>
 <select id="src" title="lingua parlata"></select>
 <span class="arrow">&rarr;</span>
 <select id="dst" title="traduci in"></select>
 <select id="mdl" title="modello di trascrizione"></select>
 <div class="mic" title="livello e volume del microfono">
   <div class="bars" id="bars"></div>
   <input type="range" id="vol" min="0" max="100" step="1">
   <span id="volv">—</span>
 </div>
 <button id="bidi" title="bidirezionale: rileva chi parla e traduce nel verso giusto">&#8644; auto</button>
 <span class="sp"></span>
 <button id="swap" title="inverti le lingue">&#8646;</button>
 <button id="big" title="testo grande, senza originale">Aa</button>
</header>
<main id="m"><div id="log"></div></main>
<footer><span id="bk">—</span><span class="sp"></span>
 <a id="save">salva .txt</a><a id="clr">pulisci vista</a>
 <span>whisper.cpp locale · cronologia su disco</span></footer>
<script>
const $=i=>document.getElementById(i);
const log=$('log'),d=$('d'),st=$('st'),bk=$('bk'),selS=$('src'),selD=$('dst'),selM=$('mdl');
let rows=new Map(),LS=[],LD=[];

for(let i=0;i<9;i++)$('bars').appendChild(document.createElement('i'));
const bars=[...$('bars').children];

function fill(sel,list,cur){sel.innerHTML='';list.forEach(([c,n])=>{
  const o=document.createElement('option');o.value=c;o.textContent=n;
  if(c===cur)o.selected=true;sel.appendChild(o);});}

function rowEl(e,old){
  let r=rows.get(e.id);
  if(!r){
    r=document.createElement('div');r.className='row wait'+(old?' old':'');
    r.innerHTML='<div class="it"></div><div class="pt"></div><div class="meta"></div>';
    log.appendChild(r);rows.set(e.id,r);
    if(!old){[...log.children].forEach(c=>c.classList.remove('top'));r.classList.add('top');
             r.classList.remove('old');}
    while(log.children.length>200)log.removeChild(log.firstChild);
  }
  return r;
}
function partial(e){const r=rowEl(e);
  r.querySelector('.it').textContent='…';
  r.querySelector('.pt').textContent=e.src;
  r.querySelector('.meta').textContent=e.t;}
function line(e,old){const r=rowEl(e,old);r.classList.remove('wait');
  r.querySelector('.it').textContent=e.dst;
  r.querySelector('.pt').textContent=e.src;
  r.querySelector('.meta').textContent=e.t+' · '+e.srcLang+'→'+e.dstLang
    +(e.how&&e.how!=='fisso'?' ('+e.how+')':'')+' · '+e.backend+' · '+e.ms+'ms';
  r.dataset.dir=e.srcLang;
  if(e.srcLang===selD.value&&document.body.classList.contains('bidi'))r.classList.add('rev');
  if(!old)bk.textContent=e.backend;}
function post(b){return fetch('/cfg',{method:'POST',body:JSON.stringify(b)});}

selS.onchange=()=>post({src:selS.value});
selD.onchange=()=>post({dst:selD.value});
selM.onchange=()=>post({model:selM.value});
$('swap').onclick=()=>{if(selD.value!=='auto')post({src:selD.value,dst:selS.value==='auto'?'it':selS.value});};
$('bidi').onclick=()=>post({bidi:!document.body.classList.contains('bidi')});
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
  $('bidi').classList.toggle('on',!!j.bidi);});

fetch('/history').then(r=>r.json()).then(j=>{
  if(!j.rows.length)return;
  j.rows.forEach(e=>line(e,true));
  const s=document.createElement('div');s.className='sep';
  s.textContent='— sessioni precedenti sopra · nuove qui sotto —';
  log.appendChild(s);});

const es=new EventSource('/events');
es.onmessage=ev=>{const e=JSON.parse(ev.data);
  if(e.type==='line')line(e);
  else if(e.type==='partial')partial(e);
  else if(e.type==='cfg'){fill(selS,LS,e.src);fill(selD,LD,e.dst);
    if(selM.options.length)selM.value=e.model;
    $('vol').value=e.mic;$('volv').textContent=e.mic;
    document.body.classList.toggle('bidi',!!e.bidi);
    $('bidi').classList.toggle('on',!!e.bidi);
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
        if body.get("model") and body["model"] != CFG["model"]:
            CFG["model"] = body["model"]
            restart = True
        if body.get("mic") is not None:
            set_mic(body["mic"])
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
                        "bidi": CFG["bidi"]})
            return
        if self.path == "/history":
            self._json({"rows": load_recent(80)})
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
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    CFG.update(src=args.src, dst=args.dst, model=args.model, capture=args.capture,
               bidi=args.bidi, langA=args.src if args.src != "auto" else "pt", langB=args.dst)

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
