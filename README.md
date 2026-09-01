# Live Translate

Sottotitoli tradotti in tempo reale di quello che sente il microfono.
Trascrizione in locale, traduzione in locale, finestra sempre in primo piano.

<p align="center"><img src="icon/icon.png" width="120" alt=""></p>

Parli, e le parole compaiono mentre le pronunci: la frase in corso cresce in
fondo alla lista con la traduzione provvisoria sopra, poi si fissa al suo posto
e la successiva le scivola sotto. In conversazione a due riconosce chi sta
parlando e traduce nel verso giusto senza che tu tocchi niente.

Serve per seguire una conversazione in una lingua che non parli, sottotitolare
un video mentre lo guardi, o rileggere dopo cosa è stato detto: tutto quello che
passa resta scritto su disco.

## Installazione

```bash
git clone https://github.com/zorahrel/live-translate
cd live-translate
./setup.sh          # whisper.cpp, modello, traduttore locale, app
open LiveTranslate.app
```

macOS 13 o superiore, Apple Silicon consigliato. Il setup è idempotente: si può
rilanciare, salta quello che c'è già. Lingue diverse dalla coppia predefinita:
`LT_SETUP_SRC=es LT_SETUP_DST=en ./setup.sh`.

Per verificare che tutto regga dopo una modifica:

```bash
./verify.py            # esce non-zero se qualcosa si rompe
./verify.py --ciclo    # aggiunge chiusura e riapertura (spegne l'app aperta)
```

Il riconoscimento della lingua si misura su `goldset.json`, un campione di
frasi vere prese dalla cronologia dove **due** giudici esterni concordano:
langdetect e il correttore ortografico di macOS. Uno solo non basta, langdetect
chiama 'italiano' del portoghese nel 4% dei casi. Si ricostruisce con
`.venv/bin/python build_goldset.py` quando la cronologia cresce.

## Uso

```bash
open LiveTranslate.app               # finestra nel Dock, con pin
./live-translate                     # da terminale, apre il browser
./live-translate --bidi              # conversazione a due
./live-translate --src es --dst en   # altra coppia di lingue
./live-translate --tts               # legge le traduzioni ad alta voce
./live-translate --capture 3         # audio di sistema invece del microfono
./live-translate --idle-exit 0       # non spegnerti quando la finestra si chiude
```

Chiudere la finestra spegne il motore. Whisper tiene 700 MB e macina il
microfono in continuo: lasciarlo acceso a vuoto costa RAM e ventola per una
cronologia che nessuno leggerà. Se nessuno guarda l'overlay per 90 secondi il
processo esce da solo, argos compreso; `--idle-exit` cambia la soglia, `0` la
toglie per chi vuole tenerlo su in background.

Dalla barra si cambiano **a caldo** lingue, modello di trascrizione e volume del
microfono, senza riavviare. Il VU meter accanto allo slider dice se il microfono
sta davvero prendendo qualcosa.

I pulsanti a destra nella barra, in ordine:

| pulsante | |
|---|---|
| frecce incrociate | inverte le due lingue |
| fulmine | streaming: trascrive mentre parli invece di attendere la fine frase |
| doppia freccia | bidirezionale: riconosce la lingua e traduce nel verso giusto |
| A grande e piccola | testo grande, senza originale, per usarla come sottotitolo |
| altoparlante | legge ad alta voce ogni traduzione (o una riga sola, dal pulsante che compare sopra la riga) |
| puntina | tiene la finestra sopra tutte le altre |

## Come funziona

```
microfono → whisper.cpp (locale, Metal) → traduzione → finestra
                    ↓
              history/*.jsonl
```

**Trascrizione**: `whisper.cpp` in sliding window, encoder su Metal. L'audio non
esce mai dalla macchina. Il default è `large-v3-turbo` quantizzato, e la
differenza si sente: con `small` una frase usciva come *"E a nuvem. Me senti."*,
con turbo la stessa scena dà frasi intere e coerenti. `small` e `base` restano
selezionabili per audio facile o batteria.

**Traduzione**, in ordine di preferenza: Argos Translate in locale → Cerebras
(se hai una chiave con credito) → Apple Translate on-device → MyMemory. Un
backend che risponde 401/402/429 viene escluso per sei ore invece di essere
ritentato a ogni frase, e lo stato sopravvive ai riavvii.

**Interfaccia**: un server HTTP su `127.0.0.1` e una finestra `WKWebView`
nativa. Nessuna connessione in uscita se usi il traduttore locale.

### Conversazione bidirezionale

`⇄ auto` o `--bidi`. Le due lingue del dialogo sono quelle nei menu; da lì ogni
frase viene instradata da sola e il verso opposto appare rientrato e viola, così
si vede chi parla senza leggere. La direzione si decide in cascata, e ogni riga
dichiara nei metadati come:

1. **`parole`** — stopword, marcatori esclusivi e n-grammi confrontati *solo*
   fra le due lingue attive. Su otto frasi reali pt/it: 6 corrette, 2 indecise,
   0 sbagliate. Richiede almeno 4 parole e uno scarto netto.
2. **`whisper N%`** — la lingua auto-rilevata da whisper, sopra il 50% di
   confidenza. Copre le frasi troppo corte per il punto 1.
3. **`default`** — in dubbio si tiene la direzione principale. Tradurre a
   rovescio confonde più che non tradurre.

**Frasi miste**: quando due voci si accavallano whisper le fonde in un chunk
solo e nella stessa riga finiscono entrambe le lingue. In bidi ogni chunk viene
spezzato sulla punteggiatura e ogni pezzo etichettato per conto suo; si divide
**solo** se emergono almeno due lingue diverse con certezza, perché spezzare una
frase monolingue la peggiora. Su 10 casi (5 misti, 5 no): 10/10.

## Cronologia

Ogni riga finisce in `history/<data>.jsonl` mentre appare a schermo, quindi
sopravvive a crash e riavvii. All'apertura la sessione in corso viene ricaricata
**intera**, le precedenti in grigio sopra un separatore. Riavviare a metà
conversazione non apre un file nuovo: se l'ultimo è recente si continua lì.
`salva .txt` esporta le ultime 500 righe leggibili.

## Voce

`say` con le voci native di sistema, 29 lingue. O automatica su ogni traduzione,
o su richiesta dal pulsante che appare passando sopra una riga. Una frase nuova
interrompe quella in corso: in conversazione la voce deve stare dietro al
parlato, non accodare minuti di ritardo.

## Audio di sistema

`ffmpeg -f avfoundation -list_devices true -i ""` elenca gli indici dei device.
Per catturare l'audio del computer invece del microfono serve un device
virtuale (BlackHole, eqMac o simili), poi `--capture <indice>`.

## Configurazione

| variabile | |
|---|---|
| `CEREBRAS_API_KEY` | opzionale, migliora la qualità della traduzione |
| `LT_EMAIL` | alza la quota MyMemory da 1.000 a 10.000 parole al giorno |
| `LT_WHISPER` | percorso di `whisper-stream` se non è nel PATH |
| `LT_PORT` | porta del server locale (default 8777) |

La chiave si può mettere anche in un `.env` accanto allo script.

## Limiti noti

- Latenza ~1-2s: whisper deve sentire abbastanza audio prima di trascrivere.
- Argos è irregolare, da 0,7s a 5s sulla stessa frase. Se la coda si accumula
  oltre cinque frasi, le più vecchie vengono mostrate non tradotte: una
  traduzione che arriva mezzo minuto dopo non serve a nessuno.
- Il tier gratuito di MyMemory si esaurisce in una ventina di minuti di
  conversazione. È il motivo per cui il traduttore locale è il primo della lista.
- Apple Translate funziona solo se hai già scaricato le lingue da Impostazioni →
  Generali → Lingua e Zona → Lingue tradotte.
- Con audio molto rumoroso whisper allucina; i tag `[MÚSICA]`, i ringraziamenti
  tipici e le lingue terze rilevate sul rumore sono filtrati.

## Licenza

MIT.
