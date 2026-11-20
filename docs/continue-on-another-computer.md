# Nadaljevanje lokalnega primerjalnega preizkusa na drugem računalniku

GitHub vsebuje kodo, teste in ta navodila. Lokalni prenosni paket vsebuje podatke, že prenesene modele, izvorne dokumente in eksperimentalne artefakte. Klon sam ne obnovi naučenih prevodov ali sprejetega OCR-ja.

## Trenutna kontrolna točka

Qwen3-4B-Instruct-2507 Q4_K_M in uradni Windows CPU llama.cpp sta prenesena in preverjena. Model še ni bil naložen; ni Qwenovih prevodov, meritev kakovosti ali dokazane prednosti pred HPLT. Preizkus se je ustavil pri premalo prostem fizičnem RAM. Model ni vključen v običajni prevajalnik.

Koda vključuje skupno preverjanje dolžine na časovno obdobje, npr. `3 mm/24 hours`, in izrecno dovoljene celotne sklonjene oblike priporočilnih terminov. Te spremembe niso splošno pomensko preverjanje ali samodejno odobren glosar. Poškodovanih virov, full curing in specific gravity ne popravljamo z globalnim pravilom.

## Kaj prenesti ločeno od Gita

V lokalnem `local-data/handoff/2026-11-20-64gb/` je prenosni ZIP z manifestom in ločeno SHA-256 datoteko. Paket prenesite po zaupanja vredni poti, npr. USB ali internem varnem prenosu. Vsebuje zasebne podatke, zato ga ne nalagajte v javni repozitorij ali javni GitHub Release.

Paket je kontrolna točka za nadaljevanje tega preizkusa, ne kopija vseh zgodovinskih začasnih map. Vsebuje:

- `local-models/`: HPLT, obstoječi OPUS, Qwen, licence in uradni Windows CPU izvajalnik;
- `local-fixtures/`: lokalne PDF-je, tudi točno dvostransko datoteko `TDS_-_Zettex_MS_Polymer_65_-_EN.pdf`;
- pet SQLite kopij: produkcijski glosar/TM in ločene tri baze profila `fresh-user-test`;
- pripeto gostotno dokazilo, profilni zagon in obnovitvena navodila;
- izbrane `tmp` mape analize, auditov, predpon, primerjalnega protokola in zadnjega prenosa;
- zavarovano uporabnikovo `(45).json` z desetimi potrditvami; drugi izvozi niso samodejno njegovo trenutno opravilo;
- `TRANSFER-MANIFEST.json` s SHA-256 vsake datoteke in revizijo kode.

Baze so izdelane s SQLite backup API, ne s kopiranjem živega WAL. Manifest beleži preverjanje integritete in FK. Izvirniki in statistike niso spremenjeni. `.env`, prijavni podatki, `.venv`, `node_modules`, `.next`, stari WAL/SHM in gradnje niso del paketa.

## Obnova na praznem Windows x64 delovnem mestu

Najprej preverite OS, arhitekturo, prosto RAM in disk. 64 GB nameščene RAM ni isto kot 64 GB proste RAM. Pripravljeni izvajalnik je za Windows x64; na drugem OS pridobite ustreznega iz uradnega projekta in ga preverite, ne zaganjajte tega binarnega paketa.

```powershell
git clone https://github.com/AlexTheGreatV/PDF-prevajalnik.git C:\Projects\PDF-prevajalnik
Set-Location C:\Projects\PDF-prevajalnik
git status --short
git rev-parse HEAD
```

Primerjajte HEAD z `repository_head` v manifestu paketa. ZIP najprej preverite z `Get-FileHash -Algorithm SHA256` in priloženo `.sha256` datoteko. Razširite ga v novo prazno začasno mapo. Preverite SHA-256 vseh datotek iz manifesta in SQLite integrity/FK; uporabite priloženi `VERIFY-TRANSFER.py`. Šele nato prekopirajte mape znotraj `payload/` v svež klon. Korenskih `TRANSFER-MANIFEST.json` in `VERIFY-TRANSFER.py` ne kopirajte v koren repozitorija. Ne prepisujte obstoječih baz ali opravila in ne mešajte baz z obstoječim WAL/SHM. Če na cilju že obstajajo podatki, najprej naredite ločeno varnostno kopijo in obnovite v novo mapo.

Ohranite ime profila `fresh-user-test`, vse tri pripadajoče baze in isto OCR evidenco. JSON sam ni dokaz sprejema OCR. Na drugi profil se sprejemi ne prenesejo; ob neujemajočem se dokazu je potreben pregled, ne samodejna potrditev. Ne združujte testnih in produkcijskih podatkov.

Ustvarite novo okolje. Stare `.venv` in odvisnosti se ne kopirajo:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r apps/api/requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install ctranslate2==4.8.2 sentencepiece==0.2.2
```

Prvotno okolje: Python 3.13.14, Node 24.22.1; pnpm različico izberite po `apps/web/package.json` (`packageManager`). Uporabite lockfile v `apps/web`. Poppler je ločena lokalna odvisnost za PDF-renderiranje; preverite `pdftoppm -v` in po potrebi nastavite `POPPLER_PDFTOPPM`. OCR integracijski preizkus dodatno potrebuje lokalni Tesseract.

Za običajni uporabniški frontend v novem klonu pripravite samo javni API naslov, brez kopiranja skrivnosti:

```powershell
Set-Content apps/web/.env.local 'NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000'
pnpm --dir apps/web install --frozen-lockfile
pnpm --dir apps/web run build
pnpm --dir apps/web run test
```

Ne kopirajte slepo `.env.example` s CORS omejitvijo na druga vrata. Brez `.env` so lokalni privzeti CORS naslovi že definirani; če jo uporabite, mora dovoljevati dejanski frontend 3001. `pnpm run test` pripravi tudi `tmp/web-tests`, ki ga uporabljajo lokalni primerjalni skripti. Obnova celotne aplikacije ni pogoj za izolirani Qwen preizkus.

Za običajni profil je v zasebnem paketu `local-data/profiles/fresh-user-test/start-local.ps1`. Ta ohrani tudi obstoječe, hash-pripeto gostotno dokazilo. Pred zagonom preverite njegove poti in da vrata niso zasedena. Qwena ne vpisujte v ta zaganjalnik. Uporabnikovega opravila z desetimi potrditvami ne nadomeščajte z demonstracijskim sprejemnim opravilom.

## Naslednji korak primerjalnega preizkusa

Preberite `tmp/qwen-local-comparison/recovery-2026-11-20/report.md`, nato `tmp/quality-comparison-preparation/comparison-protocol.json` in `freeze.json` iz paketa. Zgodovinski nizki RAM tega računalnika ni meritev novega stroja.

1. Preverite velikost/hash modela in ZIP/CLI hash iz `docs/local-comparison-artifacts.json` ter izvedite samo `llama-cli --version`.
2. Ponovno izmerite prosto fizično RAM. Pripravljena spodnja meja je 6 GiB; ocena modela 4–6 GiB in dodatna želena sistemska rezerva 2 GiB nista dejanska meritev. Če ni rezerve, ne nalagajte modela. Ne zapirajte tujih programov.
3. Najprej samo že videni regresijski primeri. Zabeležite celotni vhod, naslov, omejeni kontekst, vse različice, čas, procesni PeakWorkingSet/PeakPrivateCommit in paging skozi čas. Preverite slovenski izhod in pomen celotnih povedi, ne samo prisotnosti terminov.
4. Na istem majhnem naboru primerjajte HPLT z zaščito/SI, neposredni Qwen in Qwen popravljalnik ob angleškem izvoru. Qwenovo neposredno in popravljalno različico preverite tudi brez sosednjega konteksta, z ostalimi nastavitvami enakimi. HPLT nima enakega kanala za navodila; te razlike ne prikrijte.
5. Vsi kandidati ostanejo nepotrjeni. Izhodi se ne zapisujejo v odobren TM ali glosar. Istočasno uporabljajte isti zamrznjeni posnetek upravičenih 26 odobrenih terminov; 14 novih predlogov iz audita ni odobrenih.
6. Osem kratkih zaključnih primerov pustite nedotaknjenih do stabilnega delovanja. Pri obetavnem rezultatu pred izbiro jedra pripravite še širši nevideni nabor daljših navodil, tabelnih oznak in večpomenskih izrazov. Regresijski nabor ni več neviden.
7. Pripravite slepi pregled dejanskih treh izhodov. Popravljeni termini ob napačnih prepovedih, pogojih ali izpustih niso uspešen prevod. Ločite strojni pregled od človekove potrditve.

Qwen je pripravljena raziskovalna možnost, ne nova izbira ponudnika aplikacije. Izboljšave kakovosti, prednost neposrednega prevoda/popravljalnika ter dejanski RAM/čas Qwena še niso dokazani. Grafična glava, `18 months` in pravna noga starega MS65 ostajajo ločene odprte naloge, ne opravljeni prevodi.

## Kratko navodilo za nadaljevanje z agentom

> Delaj v C:\Projects\PDF-prevajalnik. Preberi AGENTS.md, ta dokument in lokalni paket/manifest. Preveri obnovo vseh datotek in baz brez spreminjanja izvorov. Prenosa modela in ločenega llama.cpp ter primerjalni preizkus sta že dovoljena; če sta hash-pravilno obnovljena, ju ne prenašaj ponovno. Najprej izmeri dejansko prosto RAM in izvedi le majhen že videni regresijski smoke test. Osem novih zaključnih primerov ostaja rezerviranih. Ne vključuj Qwena v običajno aplikacijo, ne potrjuj prevodov samodejno in ne spreminjaj produkcijskih podatkov.
