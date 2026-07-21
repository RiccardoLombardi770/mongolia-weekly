# UN Mongolia Weekly — guida passo-passo (versione GitHub Desktop)

Un sistema che ogni lunedì mattina raccoglie le notizie della settimana rilevanti per
UN Mongolia, le fa scrivere a Claude in un report curato (per tema, con tag agenzia e sezione
"Also noted"), le rivede da solo in un piccolo loop di qualità, e apre una **bozza** che la tua
collega approva con un click. Il report finito va live su un sito statico — impaginazione da
testata giornalistica, card ordinate, toggle EN/MN, filtro per agenzia e archivio delle
settimane passate.

Tutto gira su GitHub, senza server e senza n8n. Costo stimato: **~2-5 $/mese** di API.

Questa guida usa **GitHub Desktop** (app con interfaccia grafica): niente riga di comando per
mettere online il progetto. Tutto il resto si fa dal sito github.com.

---

## Come funziona il flusso (il quadro mentale)

```
  Lunedi 08:00 Ulaanbaatar
        |
        v
  [collect.py]  raccoglie le notizie da ReliefWeb, GDELT, RSS   (fonti gratuite)
        |
        v
  [generate.py] Claude scrive il report -> loop di auto-revisione -> traduce in MN
        |            (bozza -> critica -> correggi, fino a confidenza >= 90)
        v
  apre una PULL REQUEST con la bozza  --->  la tua collega riceve un'email
        |
        v
  lei legge, corregge se serve, clicca "Merge"
        |
        v
  [build.py] rigenera il sito  ->  va LIVE su GitHub Pages
        |
        v
  lei inoltra il link al canale dell'ufficio
```

Il **report JSON** (`reports/<data>.json`) e l'unica fonte di verita: il sito HTML viene sempre
ricostruito da li, quindi resta coerente e non si tocca mai l'HTML a mano.

---

## Cosa c'e nel progetto

```
config.yaml              <- fonti, agenzie, temi, modello, soglie  (quasi tutto si cambia QUI)
requirements.txt
scripts/
  collect.py             <- raccoglie le notizie (niente AI, niente chiave)
  generate.py            <- Claude + loop di revisione + traduzione MN
  build.py               <- genera il sito statico in docs/
templates/report.html.j2 <- layout (masthead serif + card + sidebar + toggle + filtro)
assets/style.css, app.js <- stile e interattivita del sito
reports/<data>.json      <- i report generati (fonte di verita; c'e un esempio dentro)
docs/                    <- il sito generato (e cio che GitHub Pages pubblica)
.github/workflows/
  weekly.yml             <- lunedi: raccogli+genera+apri PR
  build.yml              <- dopo il merge: ricostruisci e pubblica
```

---

## Prerequisiti (una volta sola)

1. Un **account GitHub** gratuito (tuo). La tua collega ne creera uno suo piu avanti.
2. **GitHub Desktop** installato: scaricalo da https://desktop.github.com e accedi con il tuo
   account GitHub.
3. Una **chiave API Anthropic**. Vai su https://console.anthropic.com, crea una API key, e
   tienila da parte. Per ora usi la tua (con i ~5 $ di credito bastano decine di prove); la
   sostituirai con quella d'ufficio piu avanti, cambiando un solo campo (vedi Passo 8).

(Python NON e obbligatorio per partire: il primo report puoi generarlo nel cloud. Serve solo
se vuoi fare il test locale piu comodo del Passo 6.)

---

## Passo 1 — Prepara la cartella del progetto

1. Scompatta lo `.zip` del progetto in una cartella facile da ritrovare (es. in Documenti).
2. Aprila in Esplora File e controlla di vedere DENTRO i file veri: `config.yaml`, `README.md`,
   `scripts`, `templates`. Se invece vedi un'altra cartella `mongolia-weekly` dentro la prima,
   entra in quella interna: e quella giusta (lo zip a volte crea una cartella dentro l'altra).
   La cartella "giusta" e sempre quella che contiene direttamente `config.yaml`.

---

## Passo 2 — Metti il progetto su GitHub (con GitHub Desktop)

1. Apri GitHub Desktop. Menu **File -> Add local repository**.
2. Scegli la cartella "giusta" del Passo 1 (quella con dentro `config.yaml`).
3. Desktop dira che non e ancora un repository e offrira un link **"create a repository"**:
   cliccalo, poi **Create repository** (le impostazioni di default vanno bene).
4. In basso a sinistra vedi la lista dei file, ognuno con una spunta. Nel campo in basso
   scrivi un messaggio nel riquadro **Summary**, es. `Initial setup`, e clicca
   **Commit to main**.

   > Il pulsante **Commit to main** resta grigio (non cliccabile)? Cause tipiche:
   > - **Non appare NESSUN file nella lista** (scheda "Changes" vuota). Due possibilita:
   >   (a) hai aggiunto la cartella sbagliata — quella che vedi in GitHub Desktop non
   >   contiene i file del progetto. Controlla il percorso in **Repository -> Repository
   >   settings**; se e sbagliato, fai **Current repository -> tasto destro sul repo ->
   >   Remove**, e rifai il Passo 2 scegliendo la cartella con dentro `config.yaml`.
   >   (b) avevi gia fatto un commit di questa cartella in un tentativo precedente: allora e
   >   normale che non ci sia nulla da committare — salta direttamente al punto 5 (Publish).
   > - **I file appaiono ma senza spunta**: in alto nella lista spunta la casella
   >   "select all", cosi tutti i file rientrano nel commit.
   > - **Il Summary e vuoto o non e stato registrato**: clicca dentro il riquadro *Summary*,
   >   scrivi `Initial setup`, poi clicca una volta fuori dal campo (o premi Tab). A volte il
   >   pulsante si attiva solo dopo che il campo perde il focus.
5. In alto clicca **Publish repository**. Nella finestra: lascia il nome `mongolia-weekly` e
   **togli la spunta** da "Keep this code private" (il sito dev'essere pubblico). Poi
   **Publish repository**.

Fatto: i tuoi file sono su GitHub. Aprendo github.com dal tuo profilo vedrai il repo.

> Hai creato repo di troppo per errore? Si cancellano dal sito: apri la repo su github.com ->
> **Settings** -> in fondo, **Danger Zone** -> **Delete this repository** -> conferma
> digitando il nome. Tienine una sola, quella pubblicata qui sopra.

---

## Passo 3 — Salva la chiave API come "secret"

Sul **sito** github.com, nella tua repo: **Settings -> Secrets and variables -> Actions ->
New repository secret**.
- Name: `ANTHROPIC_API_KEY`
- Secret: la tua chiave.

E cifrata e invisibile nei log. E l'unico posto dove vive la chiave.

---

## Passo 4 — Dai i permessi alle Actions

Sempre in **Settings -> Actions -> General**, sezione "Workflow permissions":
- scegli **Read and write permissions**;
- spunta **Allow GitHub Actions to create and approve pull requests**;
- Save.

Senza questo, la Pull Request automatica non puo essere creata.

---

## Passo 5 — Accendi il sito (GitHub Pages)

In **Settings -> Pages**, sezione "Build and deployment":
- Source: **Deploy from a branch**
- Branch: **main**, cartella **/docs** -> Save.

Dopo un minuto il sito e online a `https://<tuo-utente>.github.io/mongolia-weekly/`.
Poiche il progetto include gia un report d'esempio, dovresti vederlo subito: e la conferma
che tutto e collegato bene.

---

## Passo 6 — Genera il primo report vero

Due modi, scegli quello che preferisci.

**A) Nel cloud (nessun Python da installare).**
Sul sito, tab **Actions -> Weekly report -> Run workflow**. Parte la raccolta + generazione e,
al termine, apre una Pull Request con la bozza. Poi vai al Passo 7 per approvarla.

**B) In locale (per mettere a punto i prompt con calma).** Richiede Python 3.12+ sul tuo
computer. Nella cartella del progetto:
```
pip install -r requirements.txt
set ANTHROPIC_API_KEY=sk-ant-...tua-chiave...      (Windows CMD)
$env:ANTHROPIC_API_KEY="sk-ant-..."                (Windows PowerShell)
python scripts/collect.py     -> data/raw_news.json
python scripts/generate.py    -> reports/<data>.json
python scripts/build.py       -> docs/index.html   (aprilo nel browser)
```
Se il tono o la struttura non convincono, modifica i prompt in `scripts/generate.py`
(`DRAFT_SYSTEM`, `REVIEW_SYSTEM`, `TRANSLATE_SYSTEM`) e rilancia. Quando sei contento, in
GitHub Desktop fai **Commit to main** e **Push** per caricare le modifiche.

---

## Passo 7 — Il flusso settimanale della tua collega (i 3 gesti)

Una volta sola:
1. Lei si crea un account GitHub gratuito.
2. Tu la aggiungi al repo: sul sito, **Settings -> Collaborators -> Add people**.
3. Per farle arrivare l'email ogni settimana, apri `.github/workflows/weekly.yml` e togli il
   commento alla riga `reviewers: her-github-username`, mettendo il suo username. (In
   alternativa lei clicca "Watch -> All Activity" sul repo.)

Ogni settimana, per lei sono tre gesti:
1. **Riceve l'email** "Weekly report — ready for review" e apre la Pull Request.
2. Nella scheda **"Files changed"** legge il report. Se una frase va sistemata, clicca la
   matita e corregge il testo nei campi `"en"` / `"mn"` del JSON (e testo semplice ed
   etichettato — non serve saper programmare).
3. Clicca **"Merge pull request"**. Il sito si ricostruisce da solo e va live. Poi lei
   **inoltra il link** del sito al canale dell'ufficio.

> Nota sull'anteprima: in questa versione lei vede il testo nel JSON, non ancora il rendering
> finale, prima del merge. Se dopo la pubblicazione nota qualcosa, rifa la correzione e il
> sito si ricostruisce. Un'anteprima live pre-merge e un miglioramento aggiungibile dopo.

---

## Passo 8 — Cambiare fonti, agenzie, temi, chiave

Quasi tutto si cambia in **`config.yaml`** (puoi editarlo anche dal sito: apri il file ->
matita -> Commit):
- **Fonti**: feed RSS sotto `sources.rss_feeds`, query GDELT sotto `sources.gdelt.queries`.
- **Agenzie** del filtro: lista `agencies`.
- **Temi** e loro ordine: lista `themes`.
- **Termini** di rilevanza: `mongolia_terms` e `regional_terms`.
- **Modello**: campo `model` (`claude-sonnet-5`, oppure `claude-haiku-4-5-20251001` per
  spendere meno).

Per passare alla **chiave d'ufficio**: aggiorna il secret `ANTHROPIC_API_KEY` (Passo 3) col
nuovo valore. Nient'altro cambia.

---

## Costi

Volume settimanale piccolo (~40k token input, ~7-8k output EN+MN, piu 1-2 giri di
auto-revisione). Con **Claude Sonnet 5** siamo su pochi centesimi a settimana; scenario
peggiore ~5-6 $/mese. Con Haiku scendi ancora, a scapito di un po' di finezza.

---

## Nota importante sull'affidabilita

Un modello linguistico puo occasionalmente enfatizzare o confondere una notizia, e la
"confidenza" che si auto-assegna nel loop **non e una garanzia oggettiva**. Il loop alza la
qualita e cattura gli errori evidenti, ma la **revisione umana della tua collega resta il
filtro decisivo** e va mantenuta anche a regime. I prompt sono gia scritti in chiave prudente
(cita sempre la fonte, non inventare, nel dubbio declassa ad "Also noted").
