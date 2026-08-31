# live-translate

Sottotitoli tradotti in tempo reale da quello che sente il microfono.

```
live-translate                       # portoghese -> italiano, modello turbo
live-translate --bidi                # conversazione a due: traduce in entrambi i versi
live-translate --src auto            # rileva la lingua parlata
live-translate --model base          # piu' rapido, meno preciso
live-translate --capture 3           # audio di sistema (eqMac Export)
```

## Bidirezionale
`⇄ auto` nella barra, o `--bidi`. Le due lingue del dialogo sono quelle nei due
menu; da li' in poi ogni frase viene instradata da sola e il verso opposto
appare rientrato e viola, cosi' si vede chi parla senza leggere.

La direzione si decide in cascata, e ogni riga dichiara nei metadati come:
1. **`parole`** — stopword confrontate *solo* fra le due lingue attive. Su otto frasi reali pt/it: 6 corrette, 2 indecise, 0 sbagliate. Richiede almeno 4 parole e uno scarto di almeno 2 occorrenze.
2. **`whisper N%`** — la lingua auto-rilevata da whisper (`-l auto`), sopra il 50% di confidenza e non piu' vecchia di 25s. Copre le frasi corte: `"Ai que medo!"` e' andata cosi'.
3. **`default`** — in dubbio si tiene la direzione principale. Tradurre a rovescio per sbaglio confonde piu' che non tradurre.

In bidi whisper perde `-kc` (contesto fra chunk): su due lingue alternate il
contesto lo spinge a restare in quella precedente.

### Frasi miste
Quando due voci si accavallano whisper le fonde in un chunk solo, e nella
stessa riga finiscono entrambe le lingue: nella cronologia c'e' il caso vero
`"Fala a doppia traduzione. Traduz as duas coisas."`. In bidi ogni chunk viene
quindi spezzato su punteggiatura forte e ogni pezzo etichettato per conto suo;
i pezzi contigui della stessa lingua si riaccorpano, quelli indecisi ereditano
dal vicino. Se non risultano almeno **due** lingue diverse con certezza, la
riga resta intera: spezzare una frase monolingue la peggiora e basta.
I pezzi appaiono come righe separate, bordo tratteggiato e `pezzo 1/2`.

Su 10 casi (5 misti, 5 monolingui): 10/10, nessuna frase intera spezzata a
torto. Il rilevamento sui pezzi corti usa, oltre alle stopword, marcatori
esclusivi (`você`, `perché`, `gli`) e n-grammi impossibili nell'altra lingua
(`ão`, `nh`, `gn`, `zione`).

### Rumore rilevato come lingua terza
Con `-l auto` il rumore di fondo diventa a volte una lingua a caso: dal vivo e'
uscito `"Bez bez beri yapı dar bir daha"` rilevato turco al 55%. Se whisper e'
sicuro di una lingua che non e' nessuna delle due del dialogo, il chunk viene
scartato invece di essere tradotto: prima passava intatto attraverso il
traduttore e sporcava la cronologia.

## Come si legge
Un solo flusso che scorre, come una chat. In fondo c'e' la frase in corso,
azzurra e con il cursore che lampeggia: cresce mentre si parla, con le parole
nuove in evidenza e una traduzione provvisoria sopra. Quando la frase finisce
diventa definitiva **sul posto** e la successiva le si mette sotto. Lo scroll
resta agganciato in fondo e si sgancia da solo se si scorre indietro a
rileggere.

Si apre una finestra sopra le altre: traduzione grande, originale sotto.
Dalla barra si cambiano a caldo **lingua sorgente, lingua di arrivo, modello e
volume del microfono**; il VU meter accanto allo slider dice se il mic sta
davvero prendendo qualcosa (nove barre, verde/giallo/rosso).
`⇄` inverte le lingue, `Aa` ingrandisce e nasconde l'originale.

## Cronologia
Ogni riga tradotta finisce in `history/<data>_<ora>.jsonl` mentre appare a
schermo, quindi sopravvive a crash e riavvii. All'apertura l'overlay ricarica
le ultime 80 righe delle sessioni precedenti, in grigio e sopra un separatore.
`salva .txt` scarica le ultime 500 righe leggibili.
`pulisci vista` svuota solo lo schermo: il file su disco resta.

## Come funziona
- **STT**: `whisper-cpp` in locale, encoder su Metal, VAD (parla → trascrive a fine frase). L'audio non esce dal Mac.
- **Modelli**: `turbo` = large-v3-turbo q5_0 (547 MB), default su questo M2 Max perche' la differenza si sente: con `small` una frase usciva come "E a nuvem. Me senti.", con turbo la stessa scena da' frasi intere e coerenti. `small` e `base` restano per audio facile o batteria.
- **Traduzione**, in ordine: Cerebras `gpt-oss-120b` → Apple Translate on-device → MyMemory. Un backend che risponde 401/402/429 viene escluso per il resto della sessione invece di essere ritentato a ogni frase.
- **UI**: server HTTP locale + SSE su `127.0.0.1`.

## Stato dei backend di traduzione
- **Cerebras**: chiave presente ma **402 payment required**. Rientra da sola appena la ricarichi.
- **Apple on-device**: binario `apple_translate` compilato e funzionante, ma la coppia pt→it risponde `notInstalled` e `prepareTranslation()` non la scarica da un eseguibile CLI. Per attivarla: Impostazioni → Generali → Lingua e Zona → Lingue tradotte, scarica portoghese e italiano. Dopo, diventa il backend preferito: zero rete, zero quota.
- **MyMemory**: gratis e senza chiave, e' quello in uso ora. Tronca a 480 caratteri e ha una quota giornaliera anonima.

## Device audio
`ffmpeg -f avfoundation -list_devices true -i ""` elenca gli indici.
Qui: `1` = microfono interno, `3` = eqMac Export (audio di sistema).

## Limiti noti
- Latenza ~1,5-2,5s con turbo: whisper aspetta la fine della frase, poi traduce in ~0,8s.
- In bidi whisper gira in `-l auto`, un filo piu' lento e un filo meno preciso di quando la lingua e' dichiarata.
- Il VU meter chiede il permesso microfono al browser: se lo neghi resta grigio, la trascrizione funziona lo stesso (sono due stream distinti).
