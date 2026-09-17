export const TRANSLATION_PROFILE_ID = "sl-general-sskj-v1";
export const TRANSLATION_PROFILE_VERSION = 1;
export const TRANSLATION_PROFILE_SOURCE_SHA256 = "f51b8252a623014406244a07b2f263f7c2d16d5117b95903aad8094115659adc";
export const TRANSLATION_PROFILE_CANONICAL_SHA256 = "406c0db25141db3b66494a0e51f0412b5e11323b111324ff1726c7226b5d1cf6";
export const TRANSLATION_PROFILE_CANONICAL_DATE = "2026-09-17";

export type TranslationRuleExecutionMode =
  | "enforce"
  | "normalize_safe"
  | "warn"
  | "manual_review"
  | "external_source_required";
export type TranslationRuleImportance = "blocking" | "warning" | "informational";
export type TranslationDiagnosticKind = "blocking_error" | "warning" | "external_source_unchecked" | "informational";
export type TranslationRuleApplicability = "applicable" | "not_applicable" | "unknown";
export type TranslationChecklistStatus = "passed" | "failed" | "needs_review" | "not_checkable" | "not_applicable";
export type TranslationRuleScope = "document" | "segment";

export type TranslationProfileRule = {
  rule_id: string;
  source_section: number;
  section_title: string;
  source_order: number;
  section_order: number;
  source_text: string;
  execution_mode: TranslationRuleExecutionMode;
  importance: TranslationRuleImportance;
  user_message: string;
};

export type TranslationRuleConflict = {
  higher_priority: string;
  lower_priority: string;
  winner: string;
  reason: string;
};

export type TerminologySourceRecord = {
  selected_term: string;
  domain: string;
  source_or_citation: string | null;
  confidence: "low" | "medium" | "high" | null;
  user_confirmed: boolean;
};

export type TranslationProfileDiagnostic = {
  kind: TranslationDiagnosticKind;
  rule_id: string | null;
  message: string;
  segment_id: string | null;
  source_text: string | null;
  required_source_type: string | null;
  source_status: "not_required" | "not_supplied" | "supplied_unconfirmed" | "user_confirmed";
};

export type TranslationRuleEvaluation = {
  rule_id: string;
  scope: TranslationRuleScope;
  applicability: TranslationRuleApplicability;
  status: TranslationChecklistStatus;
  segment_ids: string[];
  message: string;
};

export type TranslationQualityCheck = {
  check_id: string;
  source_rule_id: string;
  label: string;
  status: TranslationChecklistStatus;
  message: string;
};

export type TranslationProfileSnapshot = {
  profile_id: string;
  profile_version: number;
  target_language: "sl";
  source_sha256: string;
  canonical_sha256: string;
  canonical_date: string;
  applied_rule_ids: string[];
  manual_review_rule_ids: string[];
  unverifiable_rule_ids: string[];
  diagnostics: TranslationProfileDiagnostic[];
  conflicts: TranslationRuleConflict[];
  terminology_sources: TerminologySourceRecord[];
  evaluation_status: "current" | "stale" | "unknown_profile";
  rule_evaluations: TranslationRuleEvaluation[];
};

type SourceSection = { number: number; title: string; rules: readonly string[] };

const SOURCE_SECTIONS: readonly SourceSection[] = [
  {
    number: 1,
    title: `Splošna načela prevajanja`,
    rules: [
      `Prevajaj natančno, celovito in pomensko zvesto. Ne izpuščaj, ne dodajaj, ne povzemaj in ne razširjaj vsebine, razen kadar je prilagoditev nujna zaradi slovničnih ali skladenjskih pravil ciljnega jezika.`,
      `Ohrani namen, ton, slog, register, stopnjo formalnosti in strokovno raven izvirnika.`,
      `Pri neformalnem ali pogovornem besedilu naj bo prevod naraven, vendar slovnično pravilen in pomensko zvest.`,
      `Besednega reda in stavčne zgradbe ne posnemaj mehanično. Prilagodi ju naravni in slovnično pravilni slovenščini, ne da bi pri tem spremenil pomen.`,
      `Ohrani vse logične odnose, stopnjo gotovosti, modalnost, zanikanje, časovne odnose, pogoje, omejitve, izjeme in poudarke izvirnika.`,
      `Natančnega izraza ne nadomeščaj s splošnejšim samo zato, da bi bil prevod bolj tekoč ali slogovno preprostejši.`,
      `Ne dodajaj komentarjev, razlag, opomb, povzetkov ali informacij, ki jih izvirnik ne vsebuje.`,
      `Kadar je izvirnik dvoumen, dvoumnost po možnosti ohrani tudi v prevodu. Ne izberi samovoljno samo ene možne razlage.`,
      `Če bistvene dvoumnosti brez dodatnega konteksta ni mogoče ustrezno ohraniti, postavi eno kratko in natančno vprašanje. V vseh drugih primerih vrni samo prevedeno besedilo.`,
    ],
  },
  {
    number: 2,
    title: `Jezikovna in pravopisna pravilnost`,
    rules: [
      `Uporabljaj sodobno knjižno slovenščino, razen če je izrecno zahtevana drugačna jezikovna zvrst.`,
      `Upoštevaj veljavno slovensko pravopisno in slovnično normo.`,
      `SSKJ oziroma eSSKJ uporabljaj predvsem za preverjanje pomena, slogovne zaznamovanosti, vezljivosti in splošne rabe besed, ne pa kot edini vir za pravopisna ali strokovnoterminološka vprašanja.`,
      `Dosledno uporabljaj pravilna ločila, velike in male začetnice ter pravilno pisanje skupaj, narazen in z vezajem.`,
      `Ohranjaj slovnično skladnost v spolu, številu, sklonu, osebi in glagolskem času.`,
      `Besedni red prilagodi naravni slovenski skladnji in berljivosti.`,
      `Pri zanikanem prehodnem glagolu uporabi rodilnik zanikanja, kadar zanikanje neposredno zajema predmet.`,
      `Pri nedoločnikih, elipsah, povedkovih določilih, ustaljenih zvezah in drugih skladenjsko posebnih primerih izberi sklon glede na dejanski skladenjski odnos in veljavno knjižno normo.`,
    ],
  },
  {
    number: 3,
    title: `Strokovna terminologija`,
    rules: [
      `Strokovne izraze prevajaj na podlagi njihovega pomena in pojma v posamezni stroki, ne samo na podlagi splošnega slovarskega pomena ali besedne podobnosti.`,
      `Vedno uporabljaj terminološki seznam, glosar ali druga terminološka navodila, ki jih zagotovi uporabnik.`,
      `Če je izraz določen v uporabnikovem terminološkem seznamu, ga uporabljaj dosledno v celotnem dokumentu, razen če bi bil v posameznem primeru očitno pomensko napačen.`,
      `Če ustreznega izraza ni na uporabnikovem seznamu, preveri strokovne vire po naslednjem prednostnem vrstnem redu:`,
      `uradni naziv iz veljavnega predpisa, standarda, tehnične specifikacije ali uradnega dokumenta;`,
      `slovenski standardi SIST in ustrezni standardi EN, ISO ali IEC;`,
      `slovenski področni terminološki slovarji in Terminologišče;`,
      `IATE za pravo Evropske unije, evropske institucije in področja, urejena na ravni Evropske unije;`,
      `uradne nomenklature, klasifikacije in terminološke zbirke posamezne stroke;`,
      `strokovni učbeniki, znanstvena literatura in publikacije priznanih strokovnih ustanov;`,
      `splošni slovarji in korpusna raba kot dopolnilni vir.`,
      `Pri uporabi terminološkega vira vedno upoštevaj področje, sobesedilo, definicijo pojma in zanesljivost vira.`,
      `Pogostost izraza sama po sebi ne pomeni, da je izraz strokovno pravilen.`,
      `Kadar obstaja več možnih izrazov, izberi tistega, ki najnatančneje označuje isti pojem in je potrjen v najzanesljivejšem razpoložljivem strokovnem viru.`,
      `Pogostost rabe upoštevaj samo, kadar so izrazi strokovno in pomensko enakovredni ter nobeden nima jasne terminološke ali normativne prednosti.`,
      `Izbrani strokovni izraz uporabljaj dosledno v celotnem dokumentu, razen kadar izvirnik namerno razlikuje med različnimi pojmi.`,
      `Ne ustvarjaj novega slovenskega termina, kadar že obstaja uveljavljen in pomensko ustrezen strokovni izraz.`,
      `Kadar izraz v različnih strokah pomeni različne pojme, izberi ustreznico glede na konkretno stroko in sobesedilo.`,
    ],
  },
  {
    number: 4,
    title: `Napake in neskladja v izvirniku`,
    rules: [
      `Očitne tipkarske, pravopisne in slovnične napake v izvirniku diskretno popravi, če popravek ne vpliva na pomen.`,
      `Ne popravljaj samovoljno številk, datumov, osebnih ali zemljepisnih imen, tehničnih parametrov, pravnih navedb, številk členov, oznak, merskih vrednosti ali morebitnih vsebinskih napak.`,
      `Protislovij, strokovnih nepravilnosti in dejanskih napak v izvirniku ne odpravljaj z ugibanjem.`,
      `Kadar je mogoče očitno napako popraviti samo z izbiro ene od več možnih razlag, ohrani nevtralno rešitev ali zaprosi za pojasnilo.`,
    ],
  },
  {
    number: 5,
    title: `Številke, datumi, valute in merske enote`,
    rules: [
      `Pri prevodu v slovenščino uporabljaj decimalno vejico: 3,14.`,
      `Večmestna števila zapisuj z razmiki med skupinami po tri števke: 1 000, 10 000 in 1 000 000.`,
      `Razmikov med števkami ne uporabljaj pri letnicah, identifikacijskih številkah, serijskih številkah, kataloških oznakah, telefonskih številkah in drugih zapisih, za katere veljajo posebna pravila.`,
      `Med številom in mersko enoto, odstotnim znakom ali oznako valute uporabi nedeljivi presledek: 20 cm, 15 %, 3 kg, 500 USD in 1 000 EUR.`,
      `Številčnih vrednosti, valut in merskih enot ne pretvarjaj brez izrecne zahteve.`,
      `Ne spreminjaj natančnosti številčnih podatkov, števila decimalnih mest ali matematičnih predznakov.`,
      `Datume in časovne zapise prilagodi slovenskim pravilom samo, kadar ne gre za strojno berljiv, standardiziran ali uradno določen zapis.`,
      `Pri programski kodi, podatkovnih zapisih, formulah, tehničnih specifikacijah in uradnih identifikatorjih ohrani izvirni zapis.`,
    ],
  },
  {
    number: 6,
    title: `Okrajšave, kratice in simboli`,
    rules: [
      `Okrajšave uporabljaj v skladu s slovenskim pravopisom: npr., tj., ipd., itd. in oz.`,
      `Razlikuj med okrajšavami, kraticami in simboli.`,
      `Okrajšave praviloma vsebujejo piko, kratice in merski simboli pa praviloma ne: EU, SIST, kg, cm in kW.`,
      `Uradnih kratic, oznak, simbolov in kod ne spreminjaj.`,
      `Tujo kratico prevedi ali nadomesti s slovensko samo, kadar obstaja uradna in v stroki uveljavljena slovenska različica.`,
      `Ob prvi pojavitvi lahko polno poimenovanje in kratico prilagodiš ustaljeni slovenski rabi, če s tem ne spremeniš uradnega naziva ali pomena.`,
    ],
  },
  {
    number: 7,
    title: `Tuje besede in izrazi`,
    rules: [
      `Tuje izraze prevedi, kadar obstaja ustaljen, pomensko enakovreden in strokovno ustrezen slovenski izraz.`,
      `Ne prevajaj tujega izraza, kadar gre za uradno ime, blagovno znamko, tehnično oznako, programski ukaz ali v stroki ustaljen mednarodni izraz brez primerne slovenske ustreznice.`,
      `Neprevedene tuje izraze oblikuj v skladu s pravili ciljne besedilne vrste in navodili uporabnika.`,
      `Ležečega tiska ne odstranjuj, kadar označuje naslov, poudarek, jezikovni zgled, znanstveno ime ali drugo strokovno oziroma slogovno funkcijo.`,
      `Latinske in druge mednarodne izraze uporabljaj samo, kadar so v besedilni vrsti ali stroki ustaljeni.`,
    ],
  },
  {
    number: 8,
    title: `Lastna imena`,
    rules: [
      `Osebna imena praviloma ohrani v izvirni obliki.`,
      `Uporabi ustaljeno slovensko obliko osebnega imena samo, kadar je takšna oblika zgodovinsko, kulturno ali jezikovno splošno uveljavljena.`,
      `Pri zemljepisnih imenih uporabi ustaljeno slovensko ime, kadar obstaja, na primer Dunaj, Rim, Benetke in Carigrad.`,
      `Kadar ustaljenega slovenskega zemljepisnega imena ni, ohrani izvirno oziroma uradno obliko.`,
      `Pri poštnih naslovih, registriranih nazivih, uradnih dokumentih in identifikacijskih podatkih ohrani uradno obliko imena.`,
      `Imen blagovnih znamk, izdelkov, storitev in organizacij ne prevajaj, razen če obstaja njihov uradni slovenski naziv.`,
      `Pri uradnih nazivih institucij preveri, ali obstaja potrjeno slovensko poimenovanje.`,
    ],
  },
  {
    number: 9,
    title: `Pravna, upravna in normativna besedila`,
    rules: [
      `Pravne in upravne izraze prevajaj v skladu z uradno terminologijo veljavnih predpisov in dokumentov.`,
      `Pri pravu Evropske unije uporabljaj izraze iz uradnih slovenskih različic zakonodaje Evropske unije in ustrezne terminologije IATE.`,
      `Ne zamenjuj pravnih pojmov, ki so v splošnem jeziku lahko sopomenski, v pravu pa imajo različen pomen.`,
      `Ohrani razlikovanje med obveznostjo, dovoljenjem, prepovedjo, priporočilom, možnostjo in pogojem.`,
      `Besed, kot so »mora«, »lahko«, »sme«, »naj«, »je dolžan« in »je upravičen«, ne zamenjuj brez upoštevanja njihovega pravnega učinka.`,
      `Sklice na zakone, člene, odstavke, priloge in druge dele dokumentov ohrani natančno.`,
    ],
  },
  {
    number: 10,
    title: `Tehnična in znanstvena besedila`,
    rules: [
      `Tehnične izraze prevajaj v skladu s terminologijo konkretnega strokovnega področja, na primer elektrotehnike, strojništva, gradbeništva, kemije, medicine ali informacijske tehnologije.`,
      `Pri izrazih z več strokovnimi pomeni izberi ustreznico na podlagi sistema, naprave, postopka ali pojava, ki ga besedilo opisuje.`,
      `Ohrani matematične formule, enačbe, kemijske formule, oznake veličin, indekse, eksponente in znanstveno nomenklaturo.`,
      `Simbolov fizikalnih veličin in merskih enot ne prevajaj.`,
      `Pri opozorilih, varnostnih navodilih in tehničnih zahtevah ohrani stopnjo nujnosti in vse varnostno pomembne podrobnosti.`,
      `Ne poenostavljaj tehničnega besedila na način, ki bi zmanjšal njegovo natančnost.`,
    ],
  },
  {
    number: 11,
    title: `Literarna, oglaševalska in slogovno zaznamovana besedila`,
    rules: [
      `Pri literarnih in stiliziranih besedilih ohrani slogovne značilnosti, metaforiko, ritem, besedne igre, arhaizme in druge izrazne prvine, kolikor to dopušča ciljni jezik.`,
      `Pri oglaševalskih besedilih ohrani prepričevalni učinek, ton in namen izvirnika, vendar ne dodajaj novih trditev ali obljub.`,
      `Pri besednih igrah uporabi pomensko in slogovno najbližjo rešitev, ne dobesednega prevoda, ki bi izgubil učinek.`,
      `Pogovorne, narečne ali žargonske prvine prenesi v ustrezno slovensko neknjižno obliko samo, kadar je njihova slogovna funkcija pomembna ali kadar je to izrecno zahtevano.`,
    ],
  },
  {
    number: 12,
    title: `Oblikovanje in struktura`,
    rules: [
      `Ohrani izvirno strukturo, vrstni red, odstavke, naslove, podnaslove, alineje, številčenje, tabele, opombe in sklice.`,
      `Ne prilagajaj oblikovanja, razen če je to izrecno zahtevano ali nujno zaradi tehnične pravilnosti.`,
      `Ohrani krepko, ležeče in podčrtano oblikovanje, kadar ima pomensko, slogovno ali strukturno funkcijo.`,
      `Pri tabelah in obrazcih ohrani razporeditev podatkov in vrstni red polj.`,
      `Ne združuj ali razdružuj odstavkov, alinej in celic brez potrebe.`,
    ],
  },
  {
    number: 13,
    title: `HTML, XML, Markdown in druga tehnična vsebina`,
    rules: [
      `Oznake HTML, XML, Markdown in druge tehnične oznake ohrani nespremenjene.`,
      `Prevedi samo uporabniku vidljivo besedilo znotraj tehnične strukture, kjer je to smiselno.`,
      `Ne prevajaj ali spreminjaj imen spremenljivk, programskih ukazov, funkcij, razredov, parametrov, poti do datotek in imen podatkovnih polj, razen če je to izrecno zahtevano.`,
      `Ohrani nadomestne oznake, kot so {name}, \${value}, %s, %d, {{variable}}, [LINK] in podobne oblike.`,
      `Ohrani spletne naslove, elektronske naslove, serijske številke, kode izdelkov, kataloške oznake in druge strojno berljive elemente.`,
      `Ne spreminjaj zaporedja, velikosti črk ali ločil v tehničnih oznakah.`,
    ],
  },
  {
    number: 14,
    title: `Terminološka in slogovna doslednost`,
    rules: [
      `Isti pojem prevajaj z istim izrazom v celotnem besedilu.`,
      `Različnih pojmov ne prevajaj z istim izrazom, če bi to povzročilo pomensko nejasnost.`,
      `Ohrani dosledno poimenovanje oseb, organizacij, izdelkov, funkcij, postopkov in sestavnih delov.`,
      `Pri daljših dokumentih upoštevaj že uporabljene terminološke in slogovne rešitve.`,
      `Ne uvajaj sopomenk samo zaradi slogovne raznolikosti, kadar gre za strokovno besedilo.`,
      `Slogovno raznolikost uporabljaj samo tam, kjer ne vpliva na terminološko natančnost.`,
    ],
  },
  {
    number: 15,
    title: `Prednostni vrstni red pravil`,
    rules: [
      `izrecna navodila uporabnika za konkretno besedilo;`,
      `uporabnikov terminološki seznam ali potrjeni glosar;`,
      `uradna terminologija veljavnih predpisov, standardov in uradnih dokumentov;`,
      `strokovna in terminološka pravilnost;`,
      `ohranitev pomena, namena in logičnih odnosov izvirnika;`,
      `veljavna pravopisna in slovnična norma ciljnega jezika;`,
      `slogovna naravnost in tekočnost prevoda.`,
      `Slogovne izboljšave nikoli ne smejo imeti prednosti pred pomensko, pravno, tehnično ali terminološko natančnostjo.`,
    ],
  },
  {
    number: 16,
    title: `Končni nadzor kakovosti`,
    rules: [
      `ali je prevedena vsa vsebina in nič ni izpuščeno;`,
      `ali prevod vsebuje informacije, ki jih v izvirniku ni;`,
      `ali so pravilno ohranjeni pomen, namen, ton, register in stopnja gotovosti;`,
      `ali so zanikanja, pogoji, izjeme, omejitve in obveznosti pravilno preneseni;`,
      `ali so strokovni izrazi pojmovno pravilni in dosledno uporabljeni;`,
      `ali so številke, datumi, valute, enote, formule in tehnični podatki nespremenjeni;`,
      `ali so lastna imena, uradni nazivi, kratice in oznake pravilno zapisani;`,
      `ali so sklici, členi, priloge, naslovi in notranje povezave pravilno ohranjeni;`,
      `ali so HTML, XML, Markdown, nadomestne oznake in drugi tehnični elementi ostali nedotaknjeni;`,
      `ali je prevod slovnično pravilen, naraven in primeren za svojo besedilno vrsto;`,
      `ali je isti pojem povsod preveden z istim strokovnim izrazom.`,
    ],
  },
  {
    number: 17,
    title: `Oblika odgovora`,
    rules: [
      `Vedno vrni samo prevedeno besedilo, brez uvoda, zaključka, komentarjev, pojasnil ali utemeljitev.`,
      `Ohrani izvirno oblikovanje in strukturo.`,
      `Dodatno vprašanje postavi samo takrat, kadar brez odgovora ni mogoče izdelati pomensko zanesljivega prevoda.`,
      `Kadar je mogoče izbrati nevtralno in pomensko varno rešitev, prevod dokončaj brez dodatnega vprašanja.`,
    ],
  },
];

const EXTERNAL_SOURCE_RULES = new Set([
  "SL-SSKJ-02-003",
  "SL-SSKJ-03-004", "SL-SSKJ-03-005", "SL-SSKJ-03-006", "SL-SSKJ-03-007",
  "SL-SSKJ-03-008", "SL-SSKJ-03-009", "SL-SSKJ-03-010", "SL-SSKJ-03-011",
  "SL-SSKJ-03-012", "SL-SSKJ-03-015",
  "SL-SSKJ-06-005", "SL-SSKJ-06-006",
  "SL-SSKJ-07-001", "SL-SSKJ-07-002", "SL-SSKJ-07-005",
  "SL-SSKJ-08-002", "SL-SSKJ-08-003", "SL-SSKJ-08-004", "SL-SSKJ-08-006", "SL-SSKJ-08-007",
  "SL-SSKJ-09-001", "SL-SSKJ-09-002",
  "SL-SSKJ-10-001", "SL-SSKJ-10-002",
]);

const SAFE_NORMALIZATION_RULES = new Set([
  "SL-SSKJ-05-001",
  "SL-SSKJ-05-002",
  "SL-SSKJ-05-004",
]);

const ENFORCED_RULES = new Set([
  "SL-SSKJ-05-005",
  "SL-SSKJ-05-006",
  "SL-SSKJ-06-004",
  "SL-SSKJ-10-004",
  "SL-SSKJ-13-004",
  "SL-SSKJ-13-005",
  "SL-SSKJ-17-001",
]);

const WARNING_RULES = new Set([
  "SL-SSKJ-01-008", "SL-SSKJ-01-009",
  "SL-SSKJ-03-013", "SL-SSKJ-03-014", "SL-SSKJ-03-016", "SL-SSKJ-03-017", "SL-SSKJ-03-018",
  "SL-SSKJ-04-001", "SL-SSKJ-04-002", "SL-SSKJ-04-003", "SL-SSKJ-04-004",
  "SL-SSKJ-14-001", "SL-SSKJ-14-002", "SL-SSKJ-14-003", "SL-SSKJ-14-004", "SL-SSKJ-14-005", "SL-SSKJ-14-006",
]);

export const TRANSLATION_RULE_PRIORITY = [
  { rank: 1, id: "user_text_instructions", label: "Izrecna navodila uporabnika za konkretno besedilo" },
  { rank: 2, id: "user_glossary", label: "Uporabnikov terminološki seznam ali potrjeni glosar" },
  { rank: 3, id: "official_terminology", label: "Uradna terminologija predpisov, standardov in uradnih dokumentov" },
  { rank: 4, id: "professional_correctness", label: "Strokovna in terminološka pravilnost" },
  { rank: 5, id: "meaning_and_logic", label: "Ohranitev pomena, namena in logičnih odnosov izvirnika" },
  { rank: 6, id: "language_norm", label: "Veljavna pravopisna in slovnična norma ciljnega jezika" },
  { rank: 7, id: "style", label: "Slogovna naravnost in tekočnost prevoda" },
] as const;

export const TRANSLATION_PROFILE_RULES: readonly TranslationProfileRule[] = (() => {
  let sourceOrder = 0;
  return SOURCE_SECTIONS.flatMap((section) => section.rules.map((sourceText, index) => {
    sourceOrder += 1;
    const ruleId = `SL-SSKJ-${String(section.number).padStart(2, "0")}-${String(index + 1).padStart(3, "0")}`;
    const executionMode = executionModeFor(ruleId);
    return {
      rule_id: ruleId,
      source_section: section.number,
      section_title: section.title,
      source_order: sourceOrder,
      section_order: index + 1,
      source_text: sourceText,
      execution_mode: executionMode,
      importance: importanceFor(executionMode),
      user_message: userMessageFor(executionMode, sourceText),
    };
  }));
})();

export const TRANSLATION_QUALITY_CHECK_RULE_IDS = Array.from({ length: 11 }, (_, index) =>
  `SL-SSKJ-16-${String(index + 1).padStart(3, "0")}`,
);

export function resolveTranslationRuleConflict(
  first: (typeof TRANSLATION_RULE_PRIORITY)[number]["id"],
  second: (typeof TRANSLATION_RULE_PRIORITY)[number]["id"],
): TranslationRuleConflict {
  const left = TRANSLATION_RULE_PRIORITY.find((item) => item.id === first)!;
  const right = TRANSLATION_RULE_PRIORITY.find((item) => item.id === second)!;
  const winner = left.rank < right.rank ? left : right;
  const loser = winner === left ? right : left;
  return {
    higher_priority: winner.id,
    lower_priority: loser.id,
    winner: winner.id,
    reason: `${winner.label} ima prednost pred: ${loser.label}. Slog ne sme preglasiti tehnične, pravne, terminološke ali pomenske natančnosti.`,
  };
}

export function createTranslationProfileSnapshot(): TranslationProfileSnapshot {
  return {
    profile_id: TRANSLATION_PROFILE_ID,
    profile_version: TRANSLATION_PROFILE_VERSION,
    target_language: "sl",
    source_sha256: TRANSLATION_PROFILE_SOURCE_SHA256,
    canonical_sha256: TRANSLATION_PROFILE_CANONICAL_SHA256,
    canonical_date: TRANSLATION_PROFILE_CANONICAL_DATE,
    applied_rule_ids: [],
    manual_review_rule_ids: [],
    unverifiable_rule_ids: [],
    diagnostics: [{
      kind: "external_source_unchecked",
      rule_id: null,
      message: "Terminološki vir ni preverjen",
      segment_id: null,
      source_text: null,
      required_source_type: "strokovni ali uradni terminološki vir",
      source_status: "not_supplied",
    }],
    conflicts: [],
    terminology_sources: [],
    evaluation_status: "current",
    rule_evaluations: [],
  };
}

export function createTranslationQualityChecklist(): TranslationQualityCheck[] {
  return TRANSLATION_QUALITY_CHECK_RULE_IDS.map((ruleId, index) => {
    const rule = TRANSLATION_PROFILE_RULES.find((item) => item.rule_id === ruleId)!;
    return {
      check_id: `quality-${String(index + 1).padStart(2, "0")}`,
      source_rule_id: ruleId,
      label: rule.source_text,
      status: "not_checkable",
      message: "Tega pogoja ni mogoče zanesljivo potrditi samodejno; potreben je ročni pregled.",
    };
  });
}

function executionModeFor(ruleId: string): TranslationRuleExecutionMode {
  if (EXTERNAL_SOURCE_RULES.has(ruleId)) return "external_source_required";
  if (SAFE_NORMALIZATION_RULES.has(ruleId)) return "normalize_safe";
  if (ENFORCED_RULES.has(ruleId)) return "enforce";
  if (WARNING_RULES.has(ruleId)) return "warn";
  return "manual_review";
}

function importanceFor(mode: TranslationRuleExecutionMode): TranslationRuleImportance {
  if (mode === "enforce") return "blocking";
  if (["warn", "manual_review", "external_source_required"].includes(mode)) return "warning";
  return "informational";
}

function userMessageFor(mode: TranslationRuleExecutionMode, sourceText: string): string {
  if (mode === "external_source_required") return `Zunanji ali uradni vir ni bil preverjen: ${sourceText}`;
  if (mode === "manual_review") return `Potreben je ročni pregled: ${sourceText}`;
  if (mode === "warn") return `Preverite opozorilo: ${sourceText}`;
  if (mode === "normalize_safe") return `Varna normalizacija je dovoljena samo ob nedvoumnem ujemanju: ${sourceText}`;
  return `Pravilo se uveljavlja z obstoječimi zaščitami: ${sourceText}`;
}
