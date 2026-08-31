#!/usr/bin/env python3
"""Applica a live-translate una richiesta dettata a voce.

Riceve una frase in italiano ("il testo e' troppo piccolo", "non leggere le
righe corte"), la passa a un agente che modifica il codice, e verifica che il
risultato parta ancora. Se non parte, torna indietro.

L'auto-modifica senza rete di sicurezza e' un modo per perdere il lavoro: qui
ogni tentativo parte da un albero pulito, viene controllato e, se rompe
qualcosa, annullato.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
LOG = os.path.join(HERE, "self-improve.log")
AGENT = os.path.expanduser("~/.local/bin/claude")

PROMPT = """Sei dentro il repo di live-translate, uno strumento macOS che
trascrive il microfono con whisper.cpp e traduce in tempo reale.

L'utente ha dettato a voce questa richiesta:

    {request}

La trascrizione puo' contenere errori di riconoscimento: interpreta l'intento,
non le parole alla lettera.

File principali:
- live_translate.py : server, pipeline, e l'interfaccia (HTML/CSS/JS dentro la
  costante PAGE). Quasi tutte le richieste si risolvono qui.
- app/main.swift    : finestra nativa (dimensioni, pin, comportamento macOS)
- argos_translate.py: traduttore locale

Regole:
- Fai la modifica minima che soddisfa la richiesta. Niente refactor non chiesti.
- Non aggiungere opzioni di configurazione se non sono state chieste.
- Niente emoji nell'interfaccia: le icone sono SVG disegnati.
- Commenta solo cio' che non si capisce dal codice, in italiano.
- Non toccare la cartella history/, ne' i file .env.
- Alla fine scrivi UNA riga che inizia con 'FATTO:' e dice cosa hai cambiato,
  in italiano, per un utente non tecnico.

Se la richiesta non e' chiara o non riguarda questo software, non modificare
niente e scrivi una riga che inizia con 'NULLA:' spiegando perche'.
"""


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as fh:
        fh.write(line + "\n")


def git(*args, check=True):
    return subprocess.run(["git", "-C", HERE, *args], capture_output=True,
                          text=True, check=check)


def syntax_ok() -> str:
    """Verifica che il codice sia ancora eseguibile. Ritorna '' se va bene."""
    r = subprocess.run([sys.executable, "-c",
                        f"import ast;ast.parse(open({os.path.join(HERE, 'live_translate.py')!r}).read())"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return "python: " + (r.stderr.strip().splitlines() or ["errore"])[-1]
    # il server deve almeno arrivare ad ascoltare su una porta di prova
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "live_translate.py"),
                          "--no-open", "--port", "8911"],
                         cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True)
    try:
        import urllib.request
        for _ in range(30):
            time.sleep(0.4)
            if p.poll() is not None:
                out = (p.stdout.read() or "")[-300:]
                return "non parte: " + out.strip().replace("\n", " ")[:200]
            try:
                urllib.request.urlopen("http://127.0.0.1:8911/", timeout=1)
                return ""
            except Exception:  # noqa: BLE001
                continue
        return "non risponde entro 12s"
    finally:
        p.terminate()
        try:
            p.wait(timeout=4)
        except subprocess.TimeoutExpired:
            p.kill()


def run(request: str) -> dict:
    request = request.strip()
    if len(request) < 8:
        return {"ok": False, "message": "richiesta troppo corta"}
    if not os.path.exists(AGENT):
        return {"ok": False, "message": "agente non disponibile su questa macchina"}

    # si parte solo da un albero pulito: altrimenti un rollback cancellerebbe
    # anche modifiche che non ha fatto l'agente
    dirty = git("status", "--porcelain").stdout.strip()
    if dirty:
        keep = [l for l in dirty.splitlines() if "history/" not in l and ".log" not in l]
        if keep:
            return {"ok": False,
                    "message": "ci sono modifiche non salvate: fai un commit prima"}

    before = git("rev-parse", "HEAD").stdout.strip()
    log(f"richiesta: {request}")

    r = subprocess.run(
        [AGENT, "-p", PROMPT.format(request=request),
         "--permission-mode", "acceptEdits", "--model", "sonnet"],
        cwd=HERE, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
    out = (r.stdout or "").strip()
    log(f"agente: {out[-400:]}")

    done = [l for l in out.splitlines() if l.startswith("FATTO:")]
    nothing = [l for l in out.splitlines() if l.startswith("NULLA:")]

    changed = git("status", "--porcelain").stdout.strip()
    changed = "\n".join(l for l in changed.splitlines()
                        if "history/" not in l and ".log" not in l)
    if not changed:
        msg = nothing[0][6:].strip() if nothing else "nessuna modifica fatta"
        return {"ok": False, "message": msg}

    err = syntax_ok()
    if err:
        log(f"scartata, {err}")
        git("checkout", "--", ".", check=False)
        git("clean", "-fd", "--", "app", check=False)
        return {"ok": False, "message": f"modifica annullata: {err}"}

    summary = done[0][6:].strip() if done else request[:80]
    git("add", "-A", check=False)
    git("-c", "user.email=porto@local", "-c", "user.name=live-translate",
        "commit", "-q", "-m", f"su richiesta vocale: {summary}\n\ndettato: {request}",
        check=False)
    after = git("rev-parse", "HEAD").stdout.strip()
    log(f"applicata: {summary} ({before[:7]} -> {after[:7]})")
    return {"ok": True, "message": summary, "commit": after[:7]}


if __name__ == "__main__":
    print(json.dumps(run(" ".join(sys.argv[1:])), ensure_ascii=False))
