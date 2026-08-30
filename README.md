# live-translate

Sottotitoli tradotti in tempo reale da quello che sente il microfono.

```
live-translate                       # portoghese -> italiano, modello turbo
live-translate --src auto            # rileva la lingua parlata
live-translate --model base          # piu' rapido, meno preciso
live-translate --capture 3           # audio di sistema (eqMac Export)
```

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
- Il VU meter chiede il permesso microfono al browser: se lo neghi resta grigio, la trascrizione funziona lo stesso (sono due stream distinti).
