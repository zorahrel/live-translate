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
./verify.py --drag     # trascina la finestra con eventi veri (vedi sotto)
./verify.py --drag --bundle   # come app vera: chiede il permesso al bundle
```

Il riconoscimento della lingua si misura su `goldset.json`, un campione di
frasi vere prese dalla cronologia dove **due** giudici esterni concordano:
langdetect e il correttore ortografico di macOS. Uno solo non basta, langdetect
chiama 'italiano' del portoghese nel 4% dei casi.

Quel file **non e' nel repo**, ed e' voluto: sono frasi di conversazioni
private, e un campione di misura non e' un motivo sufficiente per pubblicare
quello che si sono detti due persone a cena. Ognuno costruisce il suo dalla
propria cronologia, con `.venv/bin/python build_goldset.py`. Senza, gli
strumenti di misura lo dicono e si fermano invece di inventare un numero.

Un numero solo su quel campione pero' media rami che non si assomigliano.
`.venv/bin/python error_by_branch.py` lo spacca per come la direzione e' stata
decisa, leggendo il ramo che ogni riga di cronologia si porta dietro:

| ramo | quota dell'italiano | errore | cosa e' |
|---|---|---|---|
| `parole` | 78,8% | **0%** (0/404) | guarda il testo |
| `whisper` | 17,9% | 3,3% (3/92) | si fida della lingua del chunk |
| `default` | 2,9% | **100%** (15/15) | non decide: tiene la direzione principale |

`default` non e' un ramo che sbaglia spesso, e' un ramo che sull'italiano
sbaglia **sempre**, per costruzione. Da solo fa i tre quarti dell'errore
italiano (2,92 punti su 3,90): quello che conta non e' la sua percentuale, e'
quanto parlato ci finisce dentro. E' il ramo su cui lavorare, non `parole`, che
e' gia' a zero.

Due avvertenze che lo strumento stampa da solo. La **copertura**: `default` ha
un'etichetta su 16 righe (6%), quindi il suo numero e' una voce, non una
misura. E il fatto che rilanciare `route()` su un file di testo **non e'** il
router vivo: la lingua rilevata da whisper esiste solo mentre l'app ascolta, e
senza 8 delle 31 frasi che contano come errore nel 5,39% dal vivo erano state
decise bene.

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
- La misura della lingua vale sulla **trascrizione**, non sull'audio: se whisper
  impasta una frase italiana in qualcosa che sembra portoghese, i due giudici
  dicono portoghese e il router risulta "giusto" su una riga sbagliata. La
  cronologia non conserva l'audio, quindi questo si puo' dichiarare ma non
  misurare.

## Trascinamento: la prova col permesso

`./verify.py` prova che la finestra **si puo'** spostare, con `setFrameOrigin`.
Ma `setFrameOrigin` e' proprio la strada che `performDrag` non percorre: se il
trascinamento si rompesse dentro `performDrag`, quella riga resterebbe verde.

`./verify.py --drag` preme davvero sulla striscia con eventi di mouse veri e
guarda se la finestra segue. Per un secondo muove il puntatore. Postare eventi
sintetici richiede, da Mojave, il permesso di Accessibilita' — e quel permesso
e' **per programma**, non per utente. Da qui due strade:

| strada | chi deve essere autorizzato | quando si usa |
|---|---|---|
| binario diretto (default) | il **terminale** da cui lanci: il figlio eredita | sempre, gira senza toccare interruttori |
| app vera (`--drag --bundle`) | **LiveTranslate.app** | passata fedele quando tocchi il codice della finestra |

Il default lancia `LiveTranslate.app/Contents/MacOS/LiveTranslate` come figlio
della shell: eredita l'autorizzazione di chi ha aperto il terminale, quindi non
chiede niente a nessuno e **non si spegne a ogni `./build-app.sh`** — la firma
e' ad-hoc, cambia a ogni compilazione, e macOS considera stantia
l'autorizzazione data al bundle. Il prezzo e' che prova la meccanica del gesto
sotto l'identita' del terminale: per «l'app autorizzata trascina» serve
`--drag --bundle`, che passa da `open -n` (figlio di launchd, identita' TCC
propria) ed e' il percorso d'uso reale.

Se la prima strada non e' autorizzata si ripiega da sola sulla seconda. Il
ripiego scatta **solo su un saltato**: un rotto e' una risposta, e riprovare
altrove servirebbe solo a cercarne una piu' comoda.

Quando nessuna delle due e' autorizzata il test si dichiara `[salt]` e non
conta ne' come passato ne' come rotto: un test verde perche' non e' stato
eseguito e' peggio di un buco dichiarato.

I denti sono verificati al contrario, che e' l'unico modo di sapere che un test
possa fallire: togliendo `performDrag` da `mouseDown` e rimettendo la striscia
sulla costante vecchia, la riga diventa rossa con `dx=0 dy=0`; sul codice buono
torna `dx=90 dy=-45`. Attenzione a cosa **non** copre: `isMovable = false` e
`isMovableByWindowBackground = false` non la fanno arrossire, perche'
`performDrag` scavalca entrambi i flag — quelli li guarda l'autodiagnosi, con
le sue righe apposta.

Questa prova ha gia' pagato: alla prima esecuzione ha trovato la striscia di
trascinamento posizionata sulle misure iniziali della finestra (1020x480)
invece che su quelle vere ripristinate dall'autosave (1193x738). Restava a
mezz'aria in mezzo alla pagina, in alto non c'era niente da afferrare — e
l'autodiagnosi diceva "ok", perche' cercava la striscia alle stesse coordinate
con cui l'aveva messa: la prova e il difetto condividevano la costante.

## Perche' e' fermo, e cosa ha insegnato

Il progetto e' fermo, e vale la pena dire perche' invece di lasciarlo
scadere in silenzio: il collo di bottiglia non era il router delle lingue,
che dopo tre misure era arrivato a **0% di errore su 404 frasi** nel ramo
che guarda il testo. Era whisper, un livello piu' sotto.

In una conversazione vera di 21 minuti, **il 21% delle righe trascritte era
inglese**: `Thank you.`, `- Ready? - Try to look in there.`, roba che nessuno
aveva detto. Whisper stava trascrivendo il silenzio e il rumore della stanza.
E quelle righe il router le instrada come portoghese con piena confidenza,
perche' non ha un ramo "nessuna delle due" — vede parole non italiane e
conclude. Il campione di misura non poteva accorgersene **per costruzione**:
teneva solo le frasi che i giudici dicevano it o pt, quindi buttava via
esattamente il modo in cui il sistema falliva.

La cosa si misura in quattro file e due comandi. Stesso audio, due motori:

| audio | Apple `SpeechAnalyzer` | whisper large-v3-turbo `-l auto` |
|---|---|---|
| 180s di silenzio digitale | *niente* | `you you you you you I` |
| 120s di rumore di stanza (-65 dB) | *niente* | `Thank you. Thank you. Thank you. Thank you.` |
| 120s di rumore forte (-40 dB) | *niente* | `Thank you. Thank you. Thank you. Thank you.` |
| frase italiana + 25s di silenzio | la frase, esatta | la frase **+ `Grazie a tutti.`** |

Non e' una questione di soglie: e' architettura. Whisper e' un
encoder-decoder autoregressivo, *deve* emettere token, e sul silenzio pesca
il testo piu' probabile del suo addestramento — i ringraziamenti e i crediti
dei sottotitoli su cui e' stato addestrato. Un motore streaming non ha un
decoder da alimentare e sul silenzio non emette niente. Nessun `--vad`,
nessun filtro a valle e nessuna lista di frasi note porta il primo dove il
secondo parte.

Le tre lezioni che restano, e che valgono piu' del codice:

1. **Una misura puo' essere circolare al 100% senza che si veda.** La prima
   versione selezionava le frasi italiane con le stesse parole che il
   detector usa per riconoscerle: «da 4,2% a 1,41% su 426 frasi vere» non
   valeva niente. Da li' i due giudici indipendenti di `build_goldset.py`.
2. **Un numero solo media rami che non si assomigliano.** Spaccato per ramo,
   `default` risultava sbagliato sull'italiano *sempre* — e faceva da solo
   tre quarti dell'errore. Le ultime tornate di tuning stavano limando il
   ramo che era gia' a zero.
3. **La prova e il difetto non devono condividere una costante.** Vale per il
   campione di misura e vale per il trascinamento della finestra: entrambi
   dicevano "ok" perche' si controllavano con lo stesso numero con cui erano
   stati scritti.

## Due Voci — il seguito, su un motore che non allucina

`native/` e' la ripartenza dopo quella misura: stessa idea, motore diverso.
Trascrizione con `SpeechAnalyzer` di macOS 26 e traduzione con
`Translation.framework`, tutto in locale, **76 MB di RAM** contro i 547 del
solo modello whisper. Si costruisce con `native/build.sh`.

La differenza rispetto a tutte le app che fanno questo: quelle traducono in
UNA direzione, perche' il caso normale e' "guardo un video straniero". Qui
girano **due trascrittori sullo stesso microfono**, uno italiano e uno
portoghese, e chi ha parlato si decide confrontando quello che hanno capito.
Quello sbagliato non produce parole sbagliate — produce frammenti: dove il
portoghese sente `Ontem à noite eu falei com a minha mãe no telefone`,
l'italiano sente `a noi con`. Vince chi ha piu' parole che il correttore
ortografico di sistema riconosce come proprie; a parita', chi ne ha di piu'
esclusive. Il router sta in `Router.swift`, compilato **sia dall'app sia dal
banco di misura**: il banco non ne ha una copia, o misurerebbe la copia.

### Il banco, e i tre modi in cui una misura di latenza puo' mentire

`native/bench` rimpiazza il microfono con file audio riprodotti a velocita'
reale. Tre cose sono state necessarie perche' misurasse qualcosa invece di
tacere, e nessuna era prevedibile:

- la cadenza va tenuta su una **scadenza assoluta**: con `sleep(100ms)` in un
  ciclo che fa anche altro si arriva a dare 3,5 s di audio in 5,4 s, e i tempi
  misurati diventano i tempi del banco;
- il silenzio fra le frasi dev'essere **fruscio, non zero digitale**: un
  microfono vero non produce mai zeri;
- i modelli vanno chiesti con `AssetInventory` **anche quando il sistema li
  elenca gia' come installati**: senza, i trascrittori partono, non danno
  errore, e non producono niente.

### Cosa e' cambiato, misurato con lo stesso banco

| | prima | dopo |
|---|---|---|
| primo testo mentre parli | **mai** | **879 ms** |
| riga pubblicata dopo che hai smesso | 7203 ms | **3501 ms** |
| lingua indovinata | 4/6 | **6/6** |

La leva e' una sola opzione: `.fastResults` accanto a `.volatileResults`.
`volatileResults` da solo **non basta** — senza il compagno il primo testo non
arriva mai mentre parli. Sta scritto qui perche' non e' deducibile
dall'API: si vede solo aprendo `SpeechTranscriber.Preset.progressiveTranscription`
e guardando cosa contiene.

Il resto della latenza e' politica di chiusura. I due trascrittori non
finalizzano insieme: misurato, da 30 ms a 2 secondi di scarto. Aspettare
sempre il secondo costerebbe quei 2 secondi a ogni frase; non aspettarlo mai
vorrebbe dire decidere con meta' delle prove. Quindi si guarda chi guidava
**sui provvisori**: se a chiudere e' lui, la prova c'e' gia' e si pubblica
subito; se e' l'altro, si concedono 1,2 s. Chi non ha ancora chiuso viene
giudicato sull'ultimo provvisorio, che e' cio' che ha capito anche se non
l'ha ancora dichiarato.

### Due trappole del framework, pagate

`downloadAndInstall()` chiamato dal thread principale **non torna mai**: l'app
resta ferma su quella riga per sempre. Lo stesso identico codice in un binario
da terminale, che non e' isolato sul main, dura 0,2 s. Non e' lentezza, e' un
blocco — in `DueVoci.swift` la chiamata e' `nonisolated` apposta.

E i risultati vanno consumati con `for try await`: l'errore dello stream, se
lo si ingoia con un `catch {}` vuoto, fa sembrare "silenzioso" un motore che
sta invece fallendo.

### Vederla funzionare senza parlare

```bash
open -n --env DV_PROVA=/percorso/frase1.wav:/percorso/frase2.wav native/DueVoci.app
```

Da' in pasto i file invece del microfono, alla velocita' con cui sarebbero
stati pronunciati, e parte da solo. Serve a controllare che l'app funzioni
prima di sedersi a tavola con qualcuno — e a fotografare la striscia in
diretta senza far uscire un suono.

### Cosa non e' ancora misurato

Il 6/6 e il 20/20 di prima sono **voci sintetiche su audio pulito**, senza
rumore e senza voci sovrapposte. Il parlato vero e' peggio, e questi numeri
vanno rifatti li'. E la latenza e' misurata dal banco, che alimenta i
trascrittori da file: il microfono vero aggiunge la sua, non misurata.

## Licenza

MIT.
