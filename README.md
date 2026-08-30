# live-translate

Sottotitoli tradotti in tempo reale da quello che sente il microfono.

```
live-translate                     # portoghese -> italiano
live-translate --src es --dst it   # spagnolo -> italiano
live-translate --capture 3         # audio di sistema (eqMac Export)
```

Si apre una finestra sopra le altre: traduzione grande, originale sotto.
Lingua sorgente e destinazione si cambiano dai due menu senza riavviare.
`Aa` = testo grande e solo traduzione. `⇄` = inverti.

## Come funziona
- **STT**: `whisper-cpp` in locale su Metal, modalità VAD (parla → trascrive a fine frase). Nessun audio esce dal Mac in questa fase.
- **Traduzione**: prima Cerebras `gpt-oss-120b` se la chiave ha credito, altrimenti MyMemory (gratis, senza chiave). Un backend che risponde 401/402/429 viene escluso per il resto della sessione invece di essere ritentato ogni frase.
- **UI**: server HTTP locale + SSE, la pagina è servita da `127.0.0.1`.

## Device audio
`ffmpeg -f avfoundation -list_devices true -i ""` elenca gli indici.
Su questo Mac: `1` = microfono interno, `3` = eqMac Export (audio di sistema).

## Modelli
`models/ggml-small.bin` (465 MB) di default, `ggml-base.bin` più veloce e meno preciso:
`live-translate --model models/ggml-base.bin`.

## Limiti noti
- Latenza ~1-2s: whisper aspetta la fine della frase (VAD), poi traduce in ~0,7s.
- MyMemory tronca a 480 caratteri per chiamata e ha una quota giornaliera anonima.
- Con audio molto rumoroso whisper allucina; i tag `[MÚSICA]` e i ringraziamenti tipici sono filtrati.
