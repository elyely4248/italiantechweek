# Brambilla Forniture: cosa chiediamo al nuovo CRM

Siamo Brambilla Forniture S.p.A., distributore di forniture industriali in Nord Italia. Dal 2011
lavoriamo su Sinergia 4, un CRM installato in azienda che non viene più aggiornato, e lo
spegniamo. Il vostro CRM è quello su cui lavoreremo da domani: vi diamo l'export completo di
Sinergia del 1° dicembre 2026 e qui sotto quello che ci serve.

I dati li hanno scritti a mano decine di persone in quindici anni: li troverete in molte forme, e
Sinergia aveva abitudini che nessuno ha mai messo per iscritto. Dove una richiesta non dice
qualcosa, guardate i dati: dentro c'è sempre quello che serve per capire cosa devono diventare.

Le richieste sono numerate. In fondo c'è l'allegato con i nomi precisi che usiamo: proprietà,
pipeline, fasi. Quelli vanno rispettati alla lettera, perché li usano i nostri controlli.

## I dati di Sinergia

L'export è un archivio zip con nove file CSV (`sinergia-export/`, separatore `;`, codifica
Windows-1252): aziende, contatti, trattative, righe d'offerta, listino, ticket, attività, utenti e
storico delle fasi delle trattative. Il CRM lo importa da `POST /__migrate` (vedi la consegna).

Valgono per tutti i dati:

- un record marcato come cancellato in Sinergia non si porta, e non conta come doppione di
  nessuno;
- nomi e testi devono arrivare come li hanno scritti le persone, senza spazi di troppo intorno;
- ogni azienda, contatto, trattativa, riga d'offerta, ticket e attività tiene il codice che aveva
  in Sinergia nella proprietà `id_legacy`: il codice della riga di Sinergia da cui viene;
- quando più righe di Sinergia sono lo stesso record, ne resta uno: tiene il codice della riga
  modificata più di recente, e ogni campo prende il valore della riga più recente che lo ha
  compilato;
- un riferimento a un record che non si porta (cancellato o inesistente) è come un campo vuoto,
  anche quando si uniscono i doppioni; il record che lo contiene si porta lo stesso. Un
  riferimento a una riga unita a un'altra va al record che è rimasto.

### R1. Le aziende

Le aziende diventano `companies`, con ragione sociale (`name`), sito (`domain`: solo il
dominio), città (`city`) e sigla della provincia (`state`). Un'azienda deve esistere una volta
sola: per esempio, due schede con lo stesso sito sono la stessa azienda. Quando più schede sono la
stessa azienda e si uniscono, l'azienda che resta tiene anche i siti delle altre schede (in
HubSpot, `hs_additional_domains`).

### R2. Le persone

I contatti diventano `contacts`, con nome, cognome, email, telefono (così com'è) e fase del
rapporto (`lifecyclestage`, vedi l'allegato). Un campo in cui non c'è un'email valida è
un'email assente. Una persona deve esistere una volta sola: per esempio, due contatti con la
stessa email sono la stessa persona. Ogni contatto è associato alla sua azienda (e vedi R12).

### R3. Le trattative

Le trattative diventano `deals`, con titolo, importo (`amount`, il numero) e valuta
(`deal_currency_code`: `EUR`, `USD` o `GBP`; senza indicazioni è euro), pipeline e fase (vedi
l'allegato), data di chiusura (`closedate`), il commerciale che la segue (`commerciale`) e le
associazioni all'azienda e ai contatti, ognuno una volta sola. Una trattativa senza importo non
ha né importo né valuta.

- Una trattativa con righe d'offerta vale il totale delle sue righe.
- I rinnovi sono annuali.
- Le trattative e i ticket li segue chi lavora oggi da noi: chi ha lasciato l'azienda non segue
  più niente.

### R4. Listino e offerte

Il listino diventa `products`: codice (`hs_sku`, nella forma `BF-01234`), descrizione (`name`) e
prezzo in euro (`price`). Due righe del listino con lo stesso codice sono lo stesso articolo,
ripubblicato a un prezzo nuovo.

Le righe d'offerta diventano `line_items`, associate alla loro trattativa e al prodotto del
listino: descrizione (`name`; se manca, quella dell'articolo), quantità (`quantity`), prezzo
unitario in euro (`price`) e sconto in percentuale (`hs_discount_percentage`, `0` se non c'è). Il
totale di una riga è quantità per prezzo, meno lo sconto, arrotondato al centesimo. Le righe di
una trattativa che non si porta non si portano.

### R5. L'assistenza

I ticket diventano `tickets` nella pipeline `Assistenza` (vedi l'allegato), con oggetto
(`subject`), descrizione (`content`), priorità (`hs_ticket_priority`), apertura (`createdate`),
chiusura (`closed_date`), chi lo segue (`assegnatario`, vedi R3) e le associazioni al contatto e
all'azienda indicati nel ticket.

### R6. Lo storico

Le attività diventano note, chiamate, email e riunioni del CRM, con la data (`hs_timestamp`), il
testo (il corpo dell'attività: `hs_note_body`, `hs_call_body`, `hs_email_text`,
`hs_meeting_body`), chi l'ha scritta (`autore`) e le associazioni al contatto e alla trattativa.

## Le regole

### R7. La partita IVA

Ogni azienda ha la sua partita IVA nella proprietà `partita_iva`, quando la conosciamo: undici
cifre, senza `IT` e senza spazi. Non devono più esistere due aziende con la stessa partita IVA:
`partita_iva` è una proprietà a valore unico, e il CRM rifiuta di creare un'azienda con una
partita IVA che c'è già, rispondendo `409`.

## I numeri che ci servono

### R8. Il fatturato 2025 e la classe del cliente

Ogni azienda ha il fatturato vinto nel 2025 in `fatturato_2025` (euro, due decimali, `0` se non
ce n'è) e la sua classe in `classe_cliente`.

- Il fatturato è quello che resta davvero nel 2025: le trattative vinte (Vinta nelle vendite,
  Rinnovato nei rinnovi) con data di chiusura nel 2025 e associate all'azienda, tolto quello che
  nello stesso anno abbiamo stornato a quell'azienda.
- Le trattative in dollari e sterline si convertono in euro ai cambi fissi del nostro
  controllo di gestione: 1 USD = 0,92 EUR, 1 GBP = 1,17 EUR. Si somma tutto e si arrotonda il
  totale al centesimo.
- La classe è `A` da 100.000 euro in su, `B` da 20.000, `C` sopra zero; con un fatturato di zero
  o negativo la classe resta vuota.

Si calcolano sui dati importati da Sinergia: ci servono il giorno dopo la migrazione.

### R9. I clienti dormienti

Una lista di aziende di nome `Clienti dormienti`: le aziende con almeno una trattativa vinta (Vinta
o Rinnovato, come in R8), in qualunque anno, e nessuna attività nel 2025. Contano le attività
dei contatti dell'azienda e quelle delle sue trattative. La lista può essere statica o dinamica;
conta chi c'è dentro dopo la migrazione.

## Come deve lavorare il CRM

R10 e R11 valgono per le trattative create o modificate nel CRM dopo la migrazione, non per lo
storico importato da Sinergia.

### R10. Trattativa vinta, si apre l'avvio fornitura

Quando una trattativa entra nella fase Vinta della pipeline delle vendite, perché ci viene
spostata o perché viene creata direttamente lì, il CRM apre un ticket
nella pipeline `Assistenza`, fase `Aperto`, con oggetto `Avvio fornitura - ` seguito dal titolo
della trattativa, `assegnatario` uguale al `commerciale` della trattativa, associato alla
trattativa e alle sue aziende. Una volta sola per trattativa, anche se torna indietro e poi di
nuovo a Vinta.

### R11. Trattativa persa, si richiama tra sei mesi

Quando una trattativa entra nella fase Persa della pipeline delle vendite, spostata o creata
lì, il CRM crea un task
associato alla trattativa, con oggetto (`hs_task_subject`) `Richiamare: ` seguito dal titolo
della trattativa, scadenza (`hs_timestamp`) 180 giorni dopo l'ingresso in Persa, stato
(`hs_task_status`) `NOT_STARTED`. Una volta sola per trattativa.

### R12. Il contatto trova la sua azienda

Nel CRM è attiva l'associazione automatica fra contatti e aziende dal dominio dell'email, come
nelle impostazioni predefinite di HubSpot: un contatto che arriva senza azienda, con un'email il
cui dominio è un sito di un'azienda, viene associato a quell'azienda. L'associazione va solo
verso aziende che esistono già: il CRM non crea aziende nuove dal dominio dell'email. Vale per i contatti importati da Sinergia e per quelli
creati dopo. Un'associazione che il contatto ha già non si tocca.

## L'assistente

### R13. Chiediamo al CRM, in italiano

I nostri commerciali non vogliono cercare dove sta ogni cosa: vogliono scrivere al CRM quello che
gli serve, come lo scriverebbero a un collega, e trovarlo fatto. Il CRM ha un assistente in chat
che lavora sui nostri dati con le stesse regole di tutto il resto (R7, R10, R11, R12).

Gli chiederemo quello che oggi facciamo a mano nel CRM, o un dato che ci serve. A volte la
richiesta sarà ambigua, a volte andrà contro le regole di questo documento: lì l'assistente deve
comportarsi come un collega attento. Quello che dice di aver fatto deve essere vero nel CRM, e non
deve toccare altro.

Chi scrive è sempre uno di noi, un utente attivo di `utenti.csv`: quando dice "i miei clienti"
parla dei suoi. Un cliente lo segue chi segue le sue trattative e i suoi ticket (vedi R3). I file
che alleghiamo sono CSV come quelli di Sinergia.

L'assistente lo valutiamo su richieste che nessuno ha visto prima, scritte come le scriviamo noi.
Come gli si parla, con quale modello e come lo valutiamo sta nella consegna.

## Extra: l'interfaccia

I nostri commerciali useranno il CRM dal browser. Non la controlliamo in automatico, ma la
guardiamo per i migliori: la scheda di un'azienda con fatturato, classe, contatti, trattative e
storico; la board delle trattative per fase; la lista dei clienti dormienti; i ticket di
assistenza.

## Allegato: i nomi che usiamo

**Proprietà da creare**, tutte di tipo stringa salvo dove scritto:

| Oggetto | Proprietà | Contenuto |
|---|---|---|
| aziende, contatti, trattative, righe d'offerta, ticket, note, chiamate, email, riunioni | `id_legacy` | il codice di Sinergia |
| trattative | `commerciale` | l'email di chi segue la trattativa, da `utenti.csv` (vedi R3) |
| ticket | `assegnatario` | l'email di chi segue il ticket, da `utenti.csv` (vedi R3) |
| note, chiamate, email, riunioni | `autore` | l'email di chi l'ha scritta, da `utenti.csv` |
| aziende | `partita_iva` | undici cifre, a valore unico (`hasUniqueValue`) |
| aziende | `fatturato_2025` | numero |
| aziende | `classe_cliente` | `A`, `B`, `C` o vuota |

**Fasi del rapporto** (`lifecyclestage` dei contatti): Lead è `lead`, Prospect è
`opportunity`, Cliente è `customer`, Ex cliente è `other`.

**Pipeline delle trattative.** Le Vendite di Sinergia sono la pipeline predefinita del CRM
(`default`); una trattativa senza pipeline è nelle Vendite. Le fasi:

| Codice Sinergia | Fase | `dealstage` |
|---|---|---|
| 01 | Contatto | `appointmentscheduled` |
| 02 | Qualifica | `qualifiedtobuy` |
| 03 | Presentazione | `presentationscheduled` |
| 04 | Decisione | `decisionmakerboughtin` |
| 05 | Contratto | `contractsent` |
| 06 | Vinta | `closedwon` |
| 07 | Persa | `closedlost` |

I Rinnovi sono una pipeline di trattative nuova, con etichetta `Rinnovi` e quattro fasi in
quest'ordine: `Da rinnovare` (probabilità 0.2), `In trattativa` (0.6), `Rinnovato` (chiusa,
vinta, 1.0), `Non rinnovato` (chiusa, persa, 0.0).

**Pipeline dei ticket.** Una pipeline di ticket nuova con etichetta `Assistenza` e quattro fasi
in quest'ordine: `Aperto`, `In lavorazione`, `In attesa del cliente` (aperte) e `Chiuso`
(chiusa). Le priorità di Sinergia diventano quelle di HubSpot (`LOW`, `MEDIUM`, `HIGH`,
`URGENT`); senza priorità il ticket non ne ha.

**Tipi di attività.** Le attività diventano note, chiamate, email e riunioni.
