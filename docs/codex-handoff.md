# Navodila za Codex na drugem računalniku

Ta dokument je predaja trenutnega dela, ne dokaz novih testov na ciljnem računalniku. Veljavna uporabnikova navodila imajo prednost. Pred spremembami preberi korenski `AGENTS.md` in morebitna dodatna navodila v podmapah.

## Začetek brez izgube stanja

1. Preberi `docs/continue-on-another-computer.md` in `docs/local-comparison-artifacts.json`.
2. Preveri dejansko pot klona, vejo, HEAD, remote in `git status --short`. Ne predpostavljaj poti ali konfiguracije prejšnjega računalnika. Ne izvajaj reset/clean in ne zavrzi obstoječega dela.
3. Obnovi zasebni paket po navodilih za prenos. GitHub sam ne vsebuje baz, modelov, uporabnikovih opravil ali PDF-jev. Preveri ZIP in vse datoteke z manifestom, preden jih uporabiš.
4. Preveri OS, arhitekturo, prosto fizično RAM, disk in lokalne izvajalnike. Navedba 64 GB je nameščena kapaciteta, ne meritev proste RAM.
5. Pred zagonom preveri procese, zasedena vrata, API naslov frontenda, profil in absolutne poti vseh treh baz. Ne podvajaj procesov. Uporabnikovega zavihka ne osvežuj in njegovega opravila ne nadomeščaj.
6. Poročaj, kaj je dejansko obnovljeno in preverjeno, ter nadaljuj prvi nedokončani korak. Ne ponavljaj celotne implementacije ali že opravljenih prenosov.

## Identiteta predaje

- Zadnja koda pred to dokumentacijsko predajo: `a94f2a1fc24b82c1c473efa04399f1616aad25b3`, veja `main`.
- Zasebni paket: `PDF-prevajalnik-64gb-transfer.zip`, 2.860.980.469 B.
- SHA-256 ZIP-a: `5cb21e44274771ae04b84cacd24b9a29a691c50cf2a9f09c7a4d9688bc8a634c`.
- Manifest paketa je vezan na zgornji commit. Poznejši dokumentacijski commit sam po sebi ne pomeni neveljavnega paketa: preglej razliko, ne spreminjaj manifesta ali hashov.
- Paket vsebuje 292 datotek in pet konsistentnih SQLite kopij. Ne vsebuje celotne zgodovine vseh tmp map.
- Model Qwen in llama.cpp sta že prenesena in hash-preverjena. Qwen v tem pilotu še nikoli ni bil naložen. Kakovost, hitrost in dejanska največja RAM zato še niso izmerjene.
- Zadnje preverjanje pred predajo: backend 699 passed / 3 skipped; frontend 247 passed in typecheck uspešen. Preskoki so bili opt-in lokalna modelna integracija in OCR brez Tesseracta. To so zgodovinski rezultati, ne rezultat na novem računalniku.

## Podatki in uporabniška opravila

Produkcijski zbirki in profil `fresh-user-test` ostanejo ločeni. Za uporabniški test uporabi njegove obstoječe tri baze: glosar, TM in trajno OCR-evidenco. Ne združuj profilov in ne prenašaj OCR-sprejemov med njimi.

Pred zapisom naredi konsistentne kopije s SQLite backup API. Postopek za vse tri profilne baze je v README, razdelek Persistent Local Acceptance Profile. Baze kopiraj z ustavljenimi pisci; ne kopiraj samo glavne datoteke aktivne WAL baze. Obnovi v novo mapo in preveri integriteto/FK, poti ter profil, preden zaženeš API.

Zavarovani uporabnikov `(45).json` ima deset veljavnih potrditev. Kontrolni in sprejemni izvozi niso njegovo zadnje urejanje. Če ima uporabnik novejše stanje, najprej zavaruj to. Njegovega opravila ne zamenjaj z v celoti potrjeno demonstracijo.

OCR-sprejem, jezikovna potrditev, aktualni QC in fizični izvoz so ločena stanja. JSON sam ni dokaz OCR-sprejema. Trajno strežniško dokazilo mora ustrezati profilu, PDF-ju, strani, območju, besedilu in veljavni reviziji. Manjkajočega dokazila ne nadomesti z ugibanjem ali avtomatsko potrditvijo.

## Pravila dela, ki ostanejo veljavna

- Delaj lokalno. Dokumentov ne pošiljaj zunanjemu prevajalniku. Prenos modela ni dovoljenje za zunanje prevajanje.
- Nove predloge iz TM, glosarja ali modela pusti nepotrjene. Pretekle odobritve veljajo samo za dokazljivo iste vsebinske različice in dovoljeni obseg.
- Sprememba prevoda razveljavi njegovo potrditev. Sam ponovni QC nespremenjenega besedila je ne sme izbrisati.
- Področje Tehnični listi je `technical_datasheets`. Prazna domena lahko pravilno izključi področni TM. Sprememba področja sama ne pomeni ponovnega prevoda obstoječih enot; osvežitev predlogov mora biti izrecna in prikazati spremembe.
- Ne uvajaj slepih terminoloških zamenjav ali globalnih popravkov poškodovanih virov, oklepajev in dvoumnih enot. Gostotno dovoljenje MS65 je ozko vezano na preverjeni vir in celico, ne na vse zapise `gr/ml`.
- Izvor ostane nespremenjen. Jezikovno kakovost presojaj ločeno od tehnične integritete: pravilen token ali uspešen test ni dokaz pravilnega pomena, zanikanja ali navodila.
- Običajni izvoz mora ohraniti varovala lastništva, geometrije, potrditve in QC. Rekonstrukcija tabele je ločen izrecni izbor; njeno preoblikovanje ni dokaz nespremenjenih izvornih risb v izbranem območju.
- Predogled in prenos morata pripadati istemu opravilu, vsebinski reviziji in nastavitvam. Zastarelega PDF-ja ne predstavljaj kot aktualnega; zavrni pozne odgovore prejšnjih zahtevkov.
- Testiraj izolirano. Produkcijskih statistik uporabe ne povečuj s praktično validacijo. Ločeno primerjaj vsebino, zgodovino in usage statistike; fizični SHA po checkpointu ni samodejen dokaz vsebinske spremembe.
- Pri PDF-ju preveri dejansko besedilo in izris vseh relevantnih strani, slike, vektorje, osnovnice in sosednje vrednosti. Uspešen klic writerja ni dokaz dobrega izrisa.
- Ob omrežni napaki preveri delne datoteke in delujoče procese. Ne podvajaj prenosov, ne izklapljaj TLS in ne ponavljaj povezovanja v nedogled.
- Pred commitom preglej staged diff. Baze, dokumenti, modeli, skrivnosti, odvisnosti in tmp rezultati ne sodijo v Git. Commit in push zahtevata dovoljenje za konkretni obseg; ta predaja ni trajno dovoljenje za prihodnje objave.

## Naslednji dovoljeni raziskovalni korak

Nadaljuj izolirani primerjalni preizkus Qwen/HPLT po obstoječem protokolu v zasebnem paketu. Ne vključuj Qwena v običajno aplikacijo in ne spreminjaj njenega ponudnika.

Najprej preveri hashe in različico izvajalnika, nato prosto RAM. Pripravljena spodnja meja 6 GiB ter želena rezerva 2 GiB sta oceni za varno pripravo, ne dejanska meritev porabe. Drugih programov ne zapiraj sam. HPLT in Qwen lahko nalagaš zaporedno, z zabeleženimi pogoji.

Začni z majhnim že videnim regresijskim naborom. Izmeri čas, PeakWorkingSet, PeakPrivateCommit in paging. Če je stabilen, primerjaj iste izvore in isti dovoljeni terminološki posnetek v treh postopkih: HPLT z zaščito/SI, neposredni Qwen, Qwenovo popravljanje HPLT ob dostopu do angleškega izvora. Na manjšem naboru loči še vpliv sosednjega konteksta. Ne predstavljaj večjega žarka ali pogovornih navodil kot pravega terminološkega omejevanja HPLT.

Osem zamrznjenih novih zaključnih primerov ostane nedotaknjenih do stabilnega delovanja. Njihov hash in stanje sta v `docs/local-comparison-artifacts.json`. Videni nabori so regresijski, ne več nevideni. Ne dodajaj odgovorov v TM pred meritvijo. Pred odločitvijo o zamenjavi jedra je potreben širši nevideni nabor in slepi jezikovni pregled; model ne sme biti edini ocenjevalec svojih izhodov.

Štirinajst novih terminoloških predlogov ni odobrenih. Uporabi samo sledljivo odobreni posnetek iz protokola. `Specific gravity` in preimenovanje `Full curing` nista globalni pravili. Nejasnosti glede šobe, pritiska in izvornih lastnosti označi.

## Kaj ostaja odprto

Tipografski popravek starega MS65 je pripravljen za uporabnikov vizualni pregled. Brez nove konkretne napake se ne vračaj k dodatnemu piljenju tega dokumenta. Grafična glava, `18 months` in pravna noga ostajajo neprevedena odprta vsebina zunaj trenutne pokritosti. Obnovljeno sprejemno opravilo in sveži prevod imata različna dokazila; uspeha enega ne predstavljaj kot uspeh drugega.

## Sporočilo za začetek novega pogovora

> Preberi AGENTS.md, docs/codex-handoff.md in docs/continue-on-another-computer.md. Preveri dejanski klon ter zasebni prenosni paket in obnovi podatke brez prepisovanja obstoječega stanja. Nadaljuj od prvega nedokončanega koraka izoliranega primerjalnega preizkusa HPLT/Qwen. Model in izvajalnik sta že dovoljena ter prenesena; najprej preveri identiteto, prosto RAM in majhen regresijski preizkus. Osem zaključnih primerov še ne uporabi. Ne spreminjaj produkcije, uporabniškega opravila ali običajnega ponudnika, ne potrjuj novih prevodov. Za nove commite in push počakaj na navodilo. Poročaj ločeno o preverjenih dejstvih, strojnih ocenah in odprtih vprašanjih.
