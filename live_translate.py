#!/usr/bin/env python3
"""Traduzione live dal microfono, mostrata in un overlay nel browser.

whisper.cpp (locale, Metal, VAD) -> traduzione -> pagina web via SSE.
Lingua sorgente e destinazione si cambiano a caldo dall'overlay.

Uso:  live-translate [--src pt] [--dst it] [--port 8777] [--capture N]
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
}

# whisper.cpp accetta questi codici; MyMemory usa gli stessi ISO-639-1
LANGS = [
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

CFG = {"src": "pt", "dst": "it", "model": "", "capture": "1"}
src_q: "queue.Queue" = queue.Queue()
subscribers: "list" = []
sub_lock = threading.Lock()
stop_flag = threading.Event()
history: "list" = []
proc_lock = threading.Lock()
whisper_proc = None
gen = 0  # generazione: invalida gli eventi delle sessioni whisper vecchie


def broadcast(evt: dict) -> None:
    if evt.get("type") in ("line", "partial"):
        if evt["type"] == "line":
            history.append(evt)
            del history[:-60]
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
    dst_name = LANG_NAME.get(CFG["dst"], CFG["dst"])
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


def _t_mymemory(text: str) -> str:
    url = ("https://api.mymemory.translated.net/get?q=" + urllib.parse.quote(text[:480])
           + f"&langpair={CFG['src']}|{CFG['dst']}")
    req = urllib.request.Request(url, headers={"User-Agent": "live-translate/1.0"})
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.loads(r.read())
    out = ((data.get("responseData") or {}).get("translatedText") or "").strip()
    if "MYMEMORY WARNING" in out.upper() or "QUERY LENGTH" in out.upper():
        raise RuntimeError(out[:90])
    return out


BACKENDS = [("cerebras", _t_llm), ("mymemory", _t_mymemory)]
_dead: set = set()


def translate(text: str):
    if CFG["src"] == CFG["dst"]:
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
            if any(c in msg for c in ("402", "429", "403", "401")):
                _dead.add(name)
            errs.append(f"{name}: {msg[:60]}")
    return f"[traduttore non disponibile — {'; '.join(errs)}]", "none"


# ------------------------------------------------------------------- pipeline
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
        broadcast({"type": "status", "state": "off", "text": "cattura audio interrotta"})


def translator_thread() -> None:
    last = ""
    seq = 0
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
        # mostra subito l'originale, la traduzione arriva dopo
        broadcast({"type": "partial", "id": sid, "src": text,
                   "t": time.strftime("%H:%M:%S")})
        broadcast({"type": "status", "state": "busy", "text": "traduco"})
        t0 = time.time()
        out, backend = translate(text)
        broadcast({"type": "line", "id": sid, "src": text, "dst": out, "backend": backend,
                   "ms": int((time.time() - t0) * 1000), "t": time.strftime("%H:%M:%S"),
                   "srcLang": CFG["src"], "dstLang": CFG["dst"]})
        broadcast({"type": "status", "state": "live", "text": "in ascolto"})


def start_whisper() -> None:
    """(Ri)avvia whisper.cpp con la lingua sorgente corrente."""
    global whisper_proc, gen
    with proc_lock:
        gen += 1
        my_gen = gen
        if whisper_proc and whisper_proc.poll() is None:
            whisper_proc.send_signal(signal.SIGINT)
            try:
                whisper_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                whisper_proc.kill()
        whisper_proc = subprocess.Popen(
            [WHISPER, "-m", CFG["model"], "-l", CFG["src"], "--step", "0", "--length", "6000",
             "-vth", "0.6", "-c", CFG["capture"], "-t", "6"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        threading.Thread(target=reader_thread, args=(whisper_proc, my_gen), daemon=True).start()
    broadcast({"type": "cfg", **CFG})
    broadcast({"type": "status", "state": "live", "text": "in ascolto"})


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
 header{display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid #16171f;
        flex:0 0 auto;flex-wrap:wrap}
 .dot{width:9px;height:9px;border-radius:50%;background:#7de08d;box-shadow:0 0 10px #7de08d;
      transition:.2s}
 .dot.busy{background:#ffd166;box-shadow:0 0 10px #ffd166}
 .dot.off{background:#ff5c5c;box-shadow:0 0 10px #ff5c5c}
 #st{font-size:13px;color:#8a92a6;min-width:78px}
 .sp{flex:1}
 select{background:#111219;color:#dfe4ee;border:1px solid #23252f;border-radius:7px;
        padding:5px 8px;font-size:13px;font-family:inherit;cursor:pointer;outline:none}
 select:hover{border-color:#3a3d4d}
 .arrow{color:#4c5265;font-size:13px}
 button{background:#111219;color:#8a92a6;border:1px solid #23252f;border-radius:7px;
        padding:5px 10px;font-size:12px;cursor:pointer;font-family:inherit}
 button:hover{color:#fff;border-color:#3a3d4d}
 button.on{color:#4c8dff;border-color:#2b3a5c}
 main{flex:1;overflow-y:auto;padding:18px 22px 10px}
 #log{display:flex;flex-direction:column-reverse;gap:16px}
 .row{border-left:2px solid #1c1e28;padding-left:14px;animation:in .22s ease}
 .row.top{border-left-color:#4c8dff}
 .row.top .it{color:#fff}
 .row.wait .it{color:#5b6273;font-style:italic}
 @keyframes in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
 .it{font-size:28px;font-weight:600;letter-spacing:-.015em;color:#b9c2d4;margin-bottom:3px}
 .pt{font-size:14px;color:#69708291;color:#697082}
 .meta{font-size:10.5px;color:#383d4a;margin-top:3px;letter-spacing:.02em}
 footer{flex:0 0 auto;padding:7px 16px 11px;color:#383d4a;font-size:11px;
        border-top:1px solid #101119;display:flex;gap:14px}
 main::-webkit-scrollbar{width:8px}
 main::-webkit-scrollbar-thumb{background:#1a1c24;border-radius:4px}
 body.big .it{font-size:40px}
 body.big .pt{display:none}
</style></head><body>
<header>
 <span class="dot" id="d"></span><span id="st">connessione…</span>
 <select id="src" title="lingua parlata"></select>
 <span class="arrow">&rarr;</span>
 <select id="dst" title="traduci in"></select>
 <span class="sp"></span>
 <button id="swap" title="inverti le lingue">&#8646;</button>
 <button id="big" title="testo grande, senza originale">Aa</button>
 <button id="clr" title="pulisci">&#10005;</button>
</header>
<main id="m"><div id="log"></div></main>
<footer><span id="bk">—</span><span class="sp"></span>
 <span>microfono &rarr; whisper.cpp locale &rarr; traduzione</span></footer>
<script>
const $=i=>document.getElementById(i);
const log=$('log'),d=$('d'),st=$('st'),bk=$('bk'),selS=$('src'),selD=$('dst');
let LANGS=[],rows=new Map();

function fill(sel,cur){sel.innerHTML='';LANGS.forEach(([c,n])=>{
  const o=document.createElement('option');o.value=c;o.textContent=n;
  if(c===cur)o.selected=true;sel.appendChild(o);});}

function rowEl(e){
  let r=rows.get(e.id);
  if(!r){
    r=document.createElement('div');r.className='row wait';
    r.innerHTML='<div class="it"></div><div class="pt"></div><div class="meta"></div>';
    log.appendChild(r);rows.set(e.id,r);
    [...log.children].forEach(c=>c.classList.remove('top'));
    r.classList.add('top');
    while(log.children.length>60){const old=log.firstChild;log.removeChild(old);}
  }
  return r;
}
function partial(e){
  const r=rowEl(e);
  r.querySelector('.it').textContent='…';
  r.querySelector('.pt').textContent=e.src;
  r.querySelector('.meta').textContent=e.t;
}
function line(e){
  const r=rowEl(e);r.classList.remove('wait');
  r.querySelector('.it').textContent=e.dst;
  r.querySelector('.pt').textContent=e.src;
  r.querySelector('.meta').textContent=e.t+' · '+e.srcLang+'→'+e.dstLang+' · '+e.backend+' · '+e.ms+'ms';
  bk.textContent=e.backend;
}
function post(body){fetch('/cfg',{method:'POST',body:JSON.stringify(body)});}
selS.onchange=()=>post({src:selS.value});
selD.onchange=()=>post({dst:selD.value});
$('swap').onclick=()=>post({src:selD.value,dst:selS.value});
$('big').onclick=e=>{document.body.classList.toggle('big');e.target.classList.toggle('on');};
$('clr').onclick=()=>{log.innerHTML='';rows.clear();};

fetch('/langs').then(r=>r.json()).then(j=>{LANGS=j.langs;fill(selS,j.src);fill(selD,j.dst);});

const es=new EventSource('/events');
es.onmessage=ev=>{const e=JSON.parse(ev.data);
  if(e.type==='line')line(e);
  else if(e.type==='partial')partial(e);
  else if(e.type==='cfg'){if(LANGS.length){fill(selS,e.src);fill(selD,e.dst);}}
  else if(e.type==='clear'){log.innerHTML='';rows.clear();}
  else if(e.type==='status'){d.className='dot '+(e.state==='live'?'':e.state);st.textContent=e.text;}
};
es.onerror=()=>{d.className='dot off';st.textContent='disconnesso';};
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
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
        if body.get("src") and body["src"] != CFG["src"]:
            CFG["src"] = body["src"]
            restart = True
        if body.get("dst") and body["dst"] != CFG["dst"]:
            CFG["dst"] = body["dst"]
        self._json({"ok": True, "src": CFG["src"], "dst": CFG["dst"]})
        broadcast({"type": "clear"})
        history.clear()
        if restart:
            broadcast({"type": "status", "state": "busy", "text": "cambio lingua…"})
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
            self._json({"langs": LANGS, "src": CFG["src"], "dst": CFG["dst"]})
            return
        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q: queue.Queue = queue.Queue(maxsize=300)
            with sub_lock:
                subscribers.append(q)
            try:
                self._ev({"type": "cfg", **CFG})
                self._ev({"type": "status", "state": "live", "text": "in ascolto"})
                for evt in history[-15:]:
                    self._ev(evt)
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
        self.wfile.write(b"data: " + json.dumps(evt).encode() + b"\n\n")
        self.wfile.flush()


def open_window(url: str) -> None:
    for app in ("Google Chrome", "Microsoft Edge", "Brave Browser"):
        r = subprocess.run(["open", "-na", app, "--args", f"--app={url}",
                            "--window-size=980,440", "--window-position=380,560"],
                           capture_output=True)
        if r.returncode == 0:
            return
    subprocess.run(["open", url])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--src", default="pt", help="lingua parlata (ISO-639-1)")
    ap.add_argument("--dst", default="it", help="lingua di destinazione")
    ap.add_argument("--capture", default="1", help="indice device audio avfoundation")
    ap.add_argument("--model", default=os.path.join(HERE, "models", "ggml-small.bin"))
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    CFG.update(src=args.src, dst=args.dst, model=args.model, capture=args.capture)

    if not os.path.exists(args.model):
        print(f"modello mancante: {args.model}", file=sys.stderr)
        return 2
    if not os.path.exists(WHISPER):
        print("whisper-stream mancante: brew install whisper-cpp", file=sys.stderr)
        return 2

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    threading.Thread(target=translator_thread, daemon=True).start()
    start_whisper()

    url = f"http://127.0.0.1:{args.port}/"
    print(f"overlay: {url}   ({CFG['src']} -> {CFG['dst']})")
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
