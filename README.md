# Mongolia Weekly — guida passo-passo (versione GitHub Desktop)

Un sistema che ogni lunedì mattina raccoglie le notizie della settimana rilevanti per
UN Mongolia, le fa scrivere a Claude in un report curato (quattro categorie, tag agenzia, sezione
"Also noted"), le rivede da solo in un piccolo loop di qualità, e apre una **bozza** che la tua
collega approva con un click. In parallelo produce una seconda pagina, **In the Media**, che
raccoglie chi ha citato l'ONU in Mongolia, con che tono, e — quando è possibile stabilirlo — a
quale nostra pubblicazione si riferiva. Tutto va live su un sito statico: impaginazione da
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
  [collect.py]           raccoglie le notizie      (fonti dal workbook condiviso)
  [index_un.py]          indicizza le NOSTRE pubblicazioni (finestra 180 giorni)
  [collect_mentions.py]  cerca chi ci ha citati, scarica gli articoli
        |
        v
  [generate.py]          Claude scrive il report -> loop di auto-revisione -> MN
  [generate_mentions.py] tono + tipo + prominenza + quale nostra fonte era citata
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
sources/sources.xlsx     <- IL FOGLIO CONDIVISO: fonti, testate, nomi, siti UN, glossario
                            (e' qui che l'ufficio lavora, non nel codice)
config.yaml              <- categorie, agenzie, modello, soglie, stile, media monitoring
requirements.txt
scripts/
  sources_loader.py      <- legge il workbook, valida, raccoglie i problemi
  make_sources_xlsx.py   <- crea il workbook di partenza (si lancia UNA volta)
  un_style.py            <- linter UN Editorial Manual (nomi Paese, date, grafie)
  collect.py             <- raccoglie le notizie (niente AI, niente chiave)
  generate.py            <- Claude + loop di revisione + stile UN + traduzione MN
  index_un.py            <- indice a scorrimento delle nostre pubblicazioni
  collect_mentions.py    <- trova le menzioni e scarica il testo degli articoli
  match_sources.py       <- collega la menzione alla nostra pubblicazione citata
  generate_mentions.py   <- tono, tipo, prominenza, fonte citata, traduzione
  pr_body.py             <- scrive il testo della pull request del lunedi
  migrate_reports.py     <- converte i report vecchi alle quattro categorie
  build.py               <- genera il sito statico in docs/
templates/
  report.html.j2         <- layout del report settimanale
  media.html.j2          <- layout della pagina "In the Media"
assets/style.css, app.js <- stile e interattivita del sito
reports/<data>.json      <- i report generati (fonte di verita)
mentions/<data>.json     <- le menzioni della settimana (fonte di verita)
index/un_publications.json <- indice delle nostre pubblicazioni (si accumula)
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

## Passo 8 — Cambiare fonti, agenzie, categorie, chiave

**Le fonti si cambiano nel foglio Excel, non nel codice.** Vedi la sezione "Il foglio
condiviso" piu sotto.

Il resto si cambia in **`config.yaml`** (editabile anche dal sito: apri il file -> matita ->
Commit):
- **Agenzie** del filtro: lista `agencies`.
- **Categorie** e loro ordine: lista `categories` (sono quattro).
- **Tag secondari**: lista `secondary_tags`.
- **Termini** di rilevanza: `mongolia_terms` e `regional_terms`.
- **Stile UN**: `style.linter` (on/off) e `style.flag_only_terms` (i nomi che il sistema
  segnala invece di decidere).
- **Media monitoring**: `media_monitoring.match_min_confidence` (sotto questa soglia la fonte
  citata viene mostrata come "non identificata" invece che indovinata),
  `media_monitoring.un_index_days` (quanto indietro va l'indice delle nostre pubblicazioni).
- **Modello**: campo `model` (`claude-sonnet-5`, oppure `claude-haiku-4-5-20251001` per
  spendere meno).

Per passare alla **chiave d'ufficio**: aggiorna il secret `ANTHROPIC_API_KEY` (Passo 3) col
nuovo valore. Nient'altro cambia.

---

## Il foglio condiviso (`sources/sources.xlsx`)

E' il punto in cui l'ufficio lavora. Sei fogli:

| Foglio | A cosa serve |
|---|---|
| `README` | istruzioni per la collega, dentro il file stesso |
| `sources` | ricerche e feed per il report settimanale |
| `outlets` | testate da sorvegliare per le menzioni |
| `roster` | RC e head of office, **con tutti gli alias in cirillico** |
| `un_sites` | le NOSTRE pagine, indicizzate per ricostruire la citazione |
| `glossary` | terminologia UN concordata in mongolo, usata alla lettera |

Il ciclo di lavoro della collega: apre il file in Excel, aggiunge o disattiva righe (colonna
`active`: `yes`/`no`), salva, e lo ricarica su GitHub (`sources` -> **Add file** -> **Upload
files** -> trascina -> **Commit changes**). Ha effetto dal lunedi successivo.

Una riga sbagliata **non ferma il run**: finisce nella sezione "Source problems" della pull
request, con foglio e numero di riga. Le celle `<FILL IN>` nel foglio `roster` vanno compilate
per prime: finche' sono vuote, le menzioni che citano l'RC per nome non vengono trovate.

Per spostare tutto su un Google Sheet piu avanti: **File -> Condividi -> Pubblica sul web ->
CSV**, un URL per foglio, e li incolli in `workbook.csv_urls` in `config.yaml`. Nient'altro
cambia.

---

## La pagina "In the Media"

Ogni menzione mostra testata, data, lingua, tono (**Supportive / Neutral / Critical**), tipo
di menzione, prominenza, agenzie citate, e la riga **"Refers to →"** con la nostra
pubblicazione citata. Tre esiti possibili, tenuti volutamente distinti:

- **linked in the article** — l'articolo contiene un link a un dominio ONU. E' una prova, non
  una deduzione: confidenza 100.
- **probable, 78% confidence** — nessun link: la fonte e' dedotta da cifre condivise,
  formulazioni, prossimita' di date. La ragione concreta e' scritta sotto la riga.
- **not identified** — sotto la soglia. Preferiamo dirlo che indovinare.

Il tono giudica **come viene presentata l'ONU nell'articolo**, non la qualita' del pezzo ne'
l'argomento: un articolo cupo che riporta le nostre cifre senza commento e' *Neutral*.

In fondo alla pagina c'e' l'archivio cumulativo: totali, testate piu' frequenti, agenzie piu'
citate, andamento settimana per settimana.

---

## Lo stile UN Editorial Manual

Due strati, perche' il prompt da solo non basta a tenere la stessa casa editoriale tra un
lunedi e l'altro:

1. **Nei prompt** — registro istituzionale, numeri, maiuscole nei titoli di carica, forma
   breve UN dei Paesi.
2. **Nel linter** (`scripts/un_style.py`) — correzioni meccaniche deterministiche: *Russia* ->
   *the Russian Federation*, *South Korea* -> *the Republic of Korea*, *Vietnam* -> *Viet Nam*,
   *percent* -> *per cent*, *program* -> *programme*, *July 14, 2026* -> *14 July 2026*. URL,
   citazioni dirette e nomi di testata sono mascherati prima della sostituzione, quindi
   "Russia Today" resta "Russia Today".

I **nomi politicamente sensibili** (Taiwan, Kosovo, Palestine, Crimea, Western Sahara...) non
vengono mai riscritti automaticamente: compaiono in cima alla pull request sotto "Sensitive
names — decide before merging", col contesto in cui appaiono. E' materia da RCO, non da
modello.

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

Lo stesso vale, in modo ancora piu' netto, per la pagina **In the Media**: il tono e' un
giudizio automatico su una pagina pubblica, e l'attribuzione della fonte citata e' una
probabilita' dichiarata. Il disclaimer metodologico in fondo alla pagina lo dice
esplicitamente: e' la vostra protezione, conviene lasciarlo.
