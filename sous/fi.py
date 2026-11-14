"""Finnish for the gateway's own explanations: rule texts, finding sentences, approval reasons, unit notes and
mandate summaries.

Nothing here is translated at runtime. Every Finnish sentence is written from the same parameters as its English
twin (the amounts, dates and names the rule fired on), and the English stays exactly as it was. Signed payloads
(quotes, receipts, mandates) never carry Finnish: it goes only into unsigned fields next to them:

  quote response   explanations_fi = {"findings": {"<index in quote.mandate_preview.findings>": "...", ...},
                                      "unit_notes": {"<index in quote.lines>": "...", ...}}
  order            order.explanations_fi = {"findings": {... decision.findings ...}, "unit_notes": {... lines ...},
                                            "result_reason": "..."}   (result_reason after a decision)
  GET /rules       every rule has text_fi; ?lang=fi serves Finnish in `text` (English in text_en)

Formatting is Finnish: 1 234,50 €, 20 %, 15.11.2026, 15.11.2026 klo 16.45.
"""

from __future__ import annotations

from datetime import date, datetime

# --- formatting -------------------------------------------------------------------------------------------------

UNITS = {"ST": "kpl", "KPL": "kpl", "KG": "kg", "L": "l", "PAK": "pkt", "KAR": "krt", "LTK": "ltk", "PRK": "prk",
         "units": "kpl"}
WEEKDAYS = ["ma", "ti", "ke", "to", "pe", "la", "su"]


def num(x: float, decimals: int | None = None) -> str:
    """1234.5 -> '1 234,5' (decimals=None: as few as needed, like :g)."""
    s = f"{x:,.{decimals}f}" if decimals is not None else f"{x:,.3f}".rstrip("0").rstrip(".")
    return s.replace(",", " ").replace(".", ",")


def eur(x: float | None, whole: bool = False) -> str:
    """1234.5 -> '1 234,50 €'; whole=True drops ',00' (500 €)."""
    if x is None:
        return "?"
    s = num(x, 2)
    return (s[:-3] if whole and s.endswith(",00") else s) + " €"


def pct(x: float) -> str:
    """20 -> '20 %'."""
    return f"{num(x)} %"


def ratio(x: float) -> str:
    """0.9 -> '90 %'."""
    return f"{round(x * 100):d} %"


def unit(u: str | None) -> str:
    return UNITS.get(u or "", u or "")


def _date(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def day(v) -> str:
    """'2026-11-15' -> '15.11.2026'."""
    d = _date(v)
    return f"{d.day}.{d.month}.{d.year}" if d else str(v or "")


def weekday_day(v) -> str:
    """'2026-11-16' -> 'ma 16.11.2026'."""
    d = _date(v)
    return f"{WEEKDAYS[d.weekday()]} {day(d)}" if d else str(v or "")


def when(v) -> str:
    """ISO datetime -> '15.11.2026 klo 16.45' (Helsinki time when it has an offset)."""
    from . import config
    if v is None or v == "":
        return ""
    try:
        t = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    except ValueError:
        return str(v)
    if t.tzinfo is not None:
        t = t.astimezone(config.TZ)
    return f"{day(t)} klo {t.hour}.{t.minute:02d}"


def short_day(iso: str) -> str:
    """'2026-11-15...' -> '15.11.' (the English messages use dd.mm)."""
    d = _date(iso)
    return f"{d.day}.{d.month}." if d else str(iso or "")


# --- names ------------------------------------------------------------------------------------------------------

ALLERGENS = {
    "AM": "maito", "ML": "laktoosi", "AE": "kananmuna", "AF": "kala", "AC": "äyriäiset", "UM": "nilviäiset",
    "AP": "maapähkinä", "AN": "pähkinät", "SA": "manteli", "SH": "hasselpähkinä", "SW": "saksanpähkinä",
    "SC": "cashewpähkinä", "SP": "pekaanipähkinä", "SR": "parapähkinä", "ST": "pistaasipähkinä",
    "SM": "macadamiapähkinä", "NM": "muut pähkinät", "AW": "gluteenia sisältävät viljat", "UW": "vehnä",
    "NR": "ruis", "GB": "ohra", "GO": "kaura", "GS": "speltti", "GK": "kamut", "AY": "soija", "AS": "seesami",
    "BM": "sinappi", "BC": "selleri", "NL": "lupiini", "AU": "sulfiitit", "X99": "ei ilmoitettavia allergeeneja",
}

COUNTRIES = {
    "ae": "Arabiemiirikunnat", "ar": "Argentiina", "at": "Itävalta", "au": "Australia", "az": "Azerbaidžan",
    "bd": "Bangladesh", "be": "Belgia", "bf": "Burkina Faso", "bg": "Bulgaria", "bo": "Bolivia", "br": "Brasilia",
    "ca": "Kanada", "ch": "Sveitsi", "ci": "Norsunluurannikko", "cl": "Chile", "cn": "Kiina", "co": "Kolumbia",
    "cr": "Costa Rica", "cu": "Kuuba", "cy": "Kypros", "cz": "Tšekki", "de": "Saksa", "dk": "Tanska",
    "do": "Dominikaaninen tasavalta", "dz": "Algeria", "ec": "Ecuador", "ee": "Viro", "eg": "Egypti",
    "es": "Espanja", "et": "Etiopia", "fi": "Suomi", "fr": "Ranska", "gb": "Iso-Britannia", "gh": "Ghana",
    "gl": "Grönlanti", "gr": "Kreikka", "gt": "Guatemala", "hr": "Kroatia", "ht": "Haiti", "hu": "Unkari",
    "id": "Indonesia", "ie": "Irlanti", "il": "Israel", "in": "Intia", "ir": "Iran", "is": "Islanti",
    "it": "Italia", "jm": "Jamaika", "jp": "Japani", "ke": "Kenia", "kh": "Kambodža", "kr": "Etelä-Korea",
    "kz": "Kazakstan", "la": "Laos", "lb": "Libanon", "li": "Liechtenstein", "lk": "Sri Lanka", "lt": "Liettua",
    "lu": "Luxemburg", "lv": "Latvia", "ma": "Marokko", "mg": "Madagaskar", "mk": "Pohjois-Makedonia",
    "mm": "Myanmar", "mt": "Malta", "mu": "Mauritius", "mw": "Malawi", "mx": "Meksiko", "my": "Malesia",
    "nl": "Alankomaat", "no": "Norja", "nz": "Uusi-Seelanti", "pe": "Peru", "ph": "Filippiinit",
    "pk": "Pakistan", "pl": "Puola", "pt": "Portugali", "py": "Paraguay", "ro": "Romania", "ru": "Venäjä",
    "sc": "Seychellit", "se": "Ruotsi", "sg": "Singapore", "si": "Slovenia", "sk": "Slovakia",
    "sv": "El Salvador", "th": "Thaimaa", "tn": "Tunisia", "tr": "Turkki", "tw": "Taiwan", "ua": "Ukraina",
    "ug": "Uganda", "us": "Yhdysvallat", "vc": "Saint Vincent ja Grenadiinit", "vn": "Vietnam",
    "za": "Etelä-Afrikka",
}
ORIGIN_GROUPS = {"EU": "EU", "EEA": "ETA", "NORDIC": "Pohjoismaat", "BALTIC": "Baltian maat", "EUROPE": "Eurooppa"}

CLAIMS = {"FREE_FROM_LACTOSE": "laktoositon", "LOW_LACTOSE": "vähälaktoosinen", "FREE_FROM_GLUTEN": "gluteeniton",
          "FREE_FROM_MILK": "maidoton", "ORGANIC": "luomu", "VEGAN": "vegaaninen", "FREE_FROM_EGG": "munaton"}


def allergens(codes) -> str:
    return ", ".join(ALLERGENS.get(c, c) for c in codes)


def country(c: str | None) -> str:
    return COUNTRIES.get(c or "", (c or "").upper() or "tuntematon")


def origins(tokens) -> str:
    """'Suomi, Ruotsi, EU' (the Finnish twin of origin.label)."""
    return ", ".join(ORIGIN_GROUPS.get(t, t) if t in ORIGIN_GROUPS else country(t) for t in tokens or [])


def claims(codes) -> str:
    return ", ".join(CLAIMS.get(c, c.replace("_", " ").lower()) for c in codes)


# --- the rule register in Finnish -------------------------------------------------------------------------------

OWNERS = {
    "buyer": "Ostajan valtakirja: ostaja päättää säännöt, Sous valvoo niitä",
    "valio": "Valio Aimo: saatavuus, tilausajat ja hinnat; valtakirjalla niistä ei voi sopia toisin",
    "gateway": "Sousin turvasäännöt: samat kaikille ostajille, eikä valtakirjalla voi ohittaa niitä",
}
TIERS = {"silent": "tilataan, kirjataan lokiin", "notify": "tilataan, ostaja saa ilmoituksen",
         "ask": "odottaa, kunnes ihminen hyväksyy (pending_approval)", "block": "estetään"}
KINDS = {"finding": "osa tilauspäätöstä; tiukin taso ratkaisee",
         "refusal": "pysäyttää kutsun virhekoodilla ennen päätöstä"}

AVAILABILITY = {
    "NO_PRICE": "Valio Aimo ei antanut tuotteelle hintaa, joten sitä ei voi tilata.",
    "NO_AVAILABILITY": "Valio Aimo ei antanut tuotteelle saatavuustietoa.",
    "SALES_BLOCK": "Valio Aimo on estänyt tuotteen myynnin.",
    "UNDELIVERABLE": "Valio Aimo ei pysty toimittamaan tuotetta minään päivänä.",
    "OUT_OF_STOCK": "Loppu varastosta: Valio Aimolla ei ole sille toimituspäivää.",
    "CUTOFF_PASSED": "Toimituspäivän tilausaika on jo umpeutunut. Se tarkistetaan kelloa vasten, ja uudelleen "
                     "tilattaessa ja hyväksyttäessä.",
    "UNIT_MISMATCH": "Tarve on litroina, mutta tuotetta myydään painon mukaan (tai toisin päin). Sous ei arvaa "
                     "muunnosta.",
    "CONTENT_UNKNOWN": "Sous ei tiedä, paljonko yhdessä pakkauksessa on, joten määrää ei voi laskea.",
    "OFFER_VAT_UNKNOWN": "Valio Aimon erikoistarjouksella hinnoitellulle riville ei tullut Valiolta alv-kantaa eikä "
                         "verollista hintaa, josta sen voisi laskea. Sous ei koskaan oleta 0 %:a, joten rivi "
                         "estetään (sous/offers.py).",
}

RULES = {
    "mandate.revoked": "Ostaja on perunut valtakirjan: sillä ei tilata enää mitään, eikä odottavia tilauksia voi "
                       "enää hyväksyä.",
    "mandate.expired": "Valtakirjan voimassaolo on päättynyt.",
    "mandate.agent": "Valtakirjalla saa toimia vain se agentti, jolle se on annettu.",
    "mandate.ok": "Kaikki tarkistukset menivät läpi: tilaus tehdään (ja ostaja saa ilmoituksen, jos jokin "
                  "ilmoitussääntö laukesi).",
    "order.empty": "Pyyntöön ei löytynyt mitään tilattavaa.",
    "autonomy.advise": "Valtakirjan mukaan agentti saa vain neuvoa: se saa hakea ja pyytää tarjouksia, mutta jokainen "
                       "tilaus odottaa ihmisen hyväksyntää.",
    "autonomy.reorder": "Valtakirjan mukaan agentti saa tilata itse vain ostajan tavallisia tuotteita (tilattu tällä "
                        "valtakirjalla ennenkin tai usual_products-listalla). Muut tuotteet odottavat ihmisen "
                        "hyväksyntää.",
    "autonomy.capped": "Valio Aimo on rajannut tämän ostajan (tai kaikkien ostajien) agentit valtakirjaa "
                       "pienempiin oikeuksiin (GET /autonomy-caps): tilaus odottaa ihmisen hyväksyntää, ja "
                       "perusteluna on Valion syy. Rajaus ei koskaan anna lisää oikeuksia eikä päästä läpi sitä, "
                       "minkä jokin muu sääntö pysäyttää.",
    "ordering.paused": "Valio Aimo on keskeyttänyt agenttien tilaukset kaikilta tai tältä ostajalta (GET "
                       "/agent-ordering): mitään ei tilata eikä odottavia tilauksia voi hyväksyä, ennen kuin Valio "
                       "Aimo jatkaa niitä. Haku ja tarjoukset toimivat yhä.",
    "account.payment_block": "Valio Aimo on asettanut asiakastilille maksuesteen (esim. erääntyneet laskut): "
                             "tilauksia ei tehdä eikä odottavia voi hyväksyä, ennen kuin este poistuu.",
    "account.credit_limit": "Tilaus (sis. alv) on suurempi kuin asiakastilillä jäljellä oleva luotto: "
                            "luottoraja miinus avoin saldo ja tilin vielä toimittamattomat Sous-tilaukset. Ihminen "
                            "vahvistaa tilauksen.",
    "account.delivery_day": "Toimituspäivä ei ole asiakkaan toimituspäivä, tai asiakkaan jakelureitin tilausaika "
                            "sille päivälle on umpeutunut.",
    "account.unknown": "Valtakirjan ostajalle ei löydy asiakastiliä. Koskee vain tuotantoa (SOUS_REQUIRE_ACCOUNT=1); "
                       "muuten ostajalle ilman tiliä ei tehdä tilitarkistuksia.",
    "spend.hard_limit": "Ehdottoman ylärajan (alv 0 %) ylittävä tilaus estetään suoraan.",
    "spend.auto_limit": "Tilaus, joka ylittää summan, jonka agentti saa käyttää itse, odottaa ihmisen hyväksyntää.",
    "spend.notify": "Tämän summan ylittävä tilaus tehdään, ja ostaja saa siitä ilmoituksen.",
    "spend.weekly_limit": "Kuluvan kalenteriviikon (ma–su, Suomen aikaa) tilaukset yhteensä tämän tilauksen kanssa "
                          "eivät saa ylittää viikkorajaa ilman ihmisen hyväksyntää.",
    "spend.split_orders": "Tilaukset, jotka kukin jäävät agentin oman rajan alle mutta ylittävät sen yhdessä "
                          "aikaikkunan sisällä, odottavat ihmisen hyväksyntää: agentti ei voi pilkkoa isoa tilausta "
                          "pieniksi välttääkseen hyväksynnän. Mukaan lasketaan ikkunassa läpi menneet tilaukset; "
                          "hyväksyntää odottaneilla oli jo ihminen.",
    "spend.daily_limit": "Tämän päivän tilaukset yhteensä tämän tilauksen kanssa eivät saa ylittää päivärajaa "
                         "ilman ihmisen hyväksyntää. Ison tilauksen pilkkominen pieniksi ei auta: rajan ylittävä "
                         "tilaus odottaa hyväksyntää.",
    "quantity.unusual": "Jos samanlaista tuotetta tilataan paljon tavallista enemmän (yli valtakirjan kertoimen "
                        "verran aiempien tilausten mediaania samassa yksikössä; vaatii vähintään {n} aiempaa "
                        "tilausta), tilaus odottaa ihmisen hyväksyntää. Näin jää kiinni ylimääräinen nolla tai "
                        "pakkausten lukeminen litroiksi.",
    "price.changed": "Tuote, jonka yksikköhinta (alv 0 %) poikkeaa ylös tai alas yli valtakirjan prosentin siitä, "
                     "mitä siitä viimeksi maksettiin tällä valtakirjalla, odottaa ihmisen hyväksyntää: hinta on "
                     "noussut, tai eri pakkaus on luettu tavalliseksi.",
    "price.comparison_changed": "Tuote, jonka litra- tai kilohinta on yli valtakirjan prosentin kalliimpi kuin "
                                "mitä ostaja viimeksi maksoi samanlaisesta tuotteesta (kalliimpi merkki, pakkaus tai "
                                "versio), odottaa ihmisen hyväksyntää. Saman tuotteen omaa hintaa valvoo "
                                "price.changed.",
    "price.comparison_cap": "Tuote, jonka litra- tai kilohinta ylittää ostajan hintakaton sille tuotetyypille, "
                            "odottaa ihmisen hyväksyntää. Katon voi asettaa tuotetyypille, tuoteperheelle tai "
                            "tuoteryhmälle (esim. maito 2,50 €/l), joten maidon katto ei pysäytä kermaa; "
                            "tarkin katto ratkaisee. Pelkkä yksikkökohtainen katto (esim. 2,50 € kaikille "
                            "litrahintaisille) koskee kaikkia tuotteita, joille ei ole omaa kattoa.",
    "product.first_time": "Tuote, jota ei ole ennen tilattu tällä valtakirjalla eikä ole ostajan "
                          "usual_products-listalla, saa ostajan valitseman tason (ilmoitus, hyväksyntä tai esto). "
                          "Uusi tuote päätyy keittiöön vain, kun ihminen niin haluaa.",
    "product.category": "Tuotteen tuoteryhmä ei kuulu valtakirjan sallimiin.",
    "origin.restricted": "Tuotteen alkuperämaa ei kuulu valtakirjan sallimiin tai on maa, jota valtakirja välttää. "
                         "Alkuperämaa on toimittajan ilmoittama tieto Valio Aimon tuotetiedoissa, eikä sitä ole "
                         "tarkistettu erikseen. Ostaja valitsee, päättääkö ihminen vai estetäänkö tilaus.",
    "origin.unknown": "Tuotetiedoissa ei ole käyttökelpoista alkuperämaata (tyhjä tai täytekoodi), vaikka "
                      "valtakirja rajaa alkuperää: tuntematonta ei koskaan pidetä sallittuna.",
    "product.claims": "Tuotteelta puuttuu merkintä, jota valtakirja vaatii (esim. laktoositon).",
    "allergen.contains": "Tuote sisältää allergeenia, jota valtakirja välttää.",
    "allergen.may_contain": "Tuote voi sisältää (jäämiä) vältettävää allergeenia, eikä valtakirja salli sitä.",
    "allergen.likely": "Tuote ei ilmoita allergeeneja, mutta Gemini Decisions API (gemini-decision-1) arvioi "
                       "tuotteen nimien, merkin ja pääraaka-aineiden perusteella jonkin vältettävän allergeenin "
                       "todennäköisyydeksi vähintään 80 % (yksi kyllä/ei-kysymys allergeenia kohden): tilaus "
                       "estetään, eikä ihminenkään voi hyväksyä sitä. Alle 80 %:n todennäköisyydellä tuote jää "
                       "tilaan allergen.unknown; mallin vastaus ei koskaan tee tuotteesta turvallista.",
    "allergen.unknown": "Tuotteelta puuttuvat allergeenitiedot, joten ei voida varmistaa, ettei siinä ole "
                        "vältettävää allergeenia. Tuntematonta ei koskaan pidetä turvallisena.",
    "allergen.cleared": "Tuotteelta puuttuvat allergeenitiedot, mutta ostajan hyväksyjä on jo aiemman tilauksen "
                        "hyväksynnässä vahvistanut sen vapaaksi kaikesta, mitä valtakirja välttää, ja pyytänyt "
                        "Sousia muistamaan sen (sous/clearance.py: yksi valtakirja ja tuote; raukeaa, jos "
                        "tuotetiedot tai vältettävät allergeenit muuttuvat; vanhenee 90 päivässä; voidaan perua). "
                        "Tuote tilataan, ja ostajalle kerrotaan, kenen vahvistusta käytettiin.",
    "remembered.substitution": "Rivillä on sama korvaava tuote saman alkuperäisen tilalla, jonka ostajan hyväksyjä on "
                               "aiemmin hyväksynyt ja pyytänyt Sousia muistamaan: ilmoitus hyväksynnän sijaan. Koskee "
                               "yhtä valtakirjaa ja tuoteparia; raukeaa, jos jommankumman tuotteen tiedot muuttuvat, "
                               "valinnainen määrä- tai summaraja ylittyy, se vanhenee (oletus 30 päivää, enintään 90) "
                               "tai se perutaan (DELETE /mandates/…/remembered/…). Jos valtakirja estää "
                               "korvaavat tuotteet, mitään ei pehmennetä.",
    "remembered.first_time": "Tuote, jota ei ole ennen tilattu tällä valtakirjalla, mutta jonka ostajan hyväksyjä on jo "
                             "hyväksynyt ja pyytänyt Sousia muistamaan: ilmoitus hyväksynnän sijaan, samoin rajoin kuin "
                             "remembered.substitution. Estoa (first_time_products: block) ei koskaan pehmennetä.",
    "remembered.unit_interpreted": "Sous tulkitsi tarpeen yksikön samalla tavalla (20 ST laatikoina) samalle tuotteelle, "
                                   "jonka ostajan hyväksyjä on hyväksynyt ja pyytänyt muistamaan: ilmoitus hyväksynnän "
                                   "sijaan. Eri tulkinta, muuttuneet tuotteen yksiköt, rajan ylittävä määrä tai summa, "
                                   "vanheneminen tai peruminen palauttavat hyväksynnän.",
    "remembered.type_mismatch": "Sama toisenlainen tuote (kauramaito maidon tilalla), jonka ostajan hyväksyjä on "
                                "hyväksynyt ja pyytänyt muistamaan: ilmoitus hyväksynnän sijaan, samoin rajoin kuin "
                                "remembered.unit_interpreted.",
    "earned.substitution": "Korvaava tuote pyydetyn tilalla tuotetyypille, jonka ostaja on delegoinut (ansaittu autonomia, "
                 "sous/ramp.py): kun riittävän moni pidätetty tilaus hyväksyttiin muuttamatta, ostaja hyväksyi "
                 "Sousin ehdotuksen, joten siitä tulee ilmoitus hyväksynnän sijaan. Myöhempi hylkäys tai muutos "
                 "saa Sousin ehdottamaan sen perumista.",
    "earned.price_changed": "Yksikköhinnan muutos yli max_price_change_pct-rajan tuotetyypille, jonka ostaja on delegoinut (ansaittu autonomia, "
                 "sous/ramp.py): kun riittävän moni pidätetty tilaus hyväksyttiin muuttamatta, ostaja hyväksyi "
                 "Sousin ehdotuksen, joten siitä tulee ilmoitus hyväksynnän sijaan. Myöhempi hylkäys tai muutos "
                 "saa Sousin ehdottamaan sen perumista.",
    "earned.price_comparison_changed": "Samanlaisen tuotteen kalliimpi litra- tai kilohinta tuotetyypille, jonka ostaja on delegoinut (ansaittu autonomia, "
                 "sous/ramp.py): kun riittävän moni pidätetty tilaus hyväksyttiin muuttamatta, ostaja hyväksyi "
                 "Sousin ehdotuksen, joten siitä tulee ilmoitus hyväksynnän sijaan. Myöhempi hylkäys tai muutos "
                 "saa Sousin ehdottamaan sen perumista.",
    "earned.quantity_unusual": "Tavallisesta poikkeava määrä tuotetyypille, jonka ostaja on delegoinut (ansaittu autonomia, "
                 "sous/ramp.py): kun riittävän moni pidätetty tilaus hyväksyttiin muuttamatta, ostaja hyväksyi "
                 "Sousin ehdotuksen, joten siitä tulee ilmoitus hyväksynnän sijaan. Myöhempi hylkäys tai muutos "
                 "saa Sousin ehdottamaan sen perumista.",
    "earned.supply_reliability": "Tuote alle min_delivery_reliability-tason tuotetyypille, jonka ostaja on delegoinut (ansaittu autonomia, "
                 "sous/ramp.py): kun riittävän moni pidätetty tilaus hyväksyttiin muuttamatta, ostaja hyväksyi "
                 "Sousin ehdotuksen, joten siitä tulee ilmoitus hyväksynnän sijaan. Myöhempi hylkäys tai muutos "
                 "saa Sousin ehdottamaan sen perumista.",
    "earned.delivery_later_date": "Myöhempi toimituspäivä tuotetyypille, jonka ostaja on delegoinut (ansaittu autonomia, "
                 "sous/ramp.py): kun riittävän moni pidätetty tilaus hyväksyttiin muuttamatta, ostaja hyväksyi "
                 "Sousin ehdotuksen, joten siitä tulee ilmoitus hyväksynnän sijaan. Myöhempi hylkäys tai muutos "
                 "saa Sousin ehdottamaan sen perumista.",
    "substitution": "Rivillä on korvaava tuote pyydetyn tilalla; valtakirja määrää, kuinka paljon ihminen saa siihen sanoa.",
    "purpose.exact": "Korvaava tuote tuotteelle, jota ostaja käyttää niin, että vain juuri se tuote käy (esim. "
                     "allergiaresepti): estetään, eikä Valio Aimo ehdota sille vaihtoehtoa. Asetetaan valtakirjassa "
                     "tuotekohtaisesti tai rivillä; agentti voi vain tiukentaa.",
    "purpose.ask": "Korvaava tuote tuotteelle, jota ostaja käyttää reseptissä (kuohukerma ei välttämättä vaahdotu "
                   "samoin): ostaja päättää aina. Kahvikerma taas voidaan merkitä vapaasti korvattavaksi.",
    "needs.changed": "Ostajan tarpeet muuttuivat kesken sopimuksen: valtakirjan päivitys tiukensi allergeeni- tai "
                     "ruokavaliovaatimuksia (uusi vältettävä allergeeni, uusi vaadittu merkintä, 'voi sisältää' tai "
                     "puuttuvat allergeenitiedot eivät enää kelpaa). Kaikki valtakirjan vakiotilaukset, odottavat ja "
                     "tulevat tilaukset sekä muistetut päätökset tarkistetaan uudelleen: ristiriitainen vakiotilaus "
                     "pysäytetään, odottava tilaus saa tämän havainnon, jo lähetetty tilaus merkitään "
                     "tarkistettavaksi, ja nyt vaarallinen muistettu hyväksyntä tai allergeenivarmistus perutaan. "
                     "Mitään ei muuteta tai pudoteta hiljaa; lievennys ei koskaan laajenna mitään itsestään.",
    "substitution.premium": "Korvaava tuote, joka maksaa vertailuyksikköä kohden (Valion vertailuhinta litralta, "
                            "kilolta tai kappaleelta) enemmän kuin valtakirjan prosenttiosuus yli korvattavan "
                            "tuotteen hinnan, saa ostajan valitseman tason (oletuksena hyväksyntä): ostaja hyväksyy "
                            "korvaavan, ei kalliimpaa. Jos korvattavan tuotteen hintaa ei saada tai se on eri "
                            "yksikössä, tilaus odottaa hyväksyntää ja kertoo syyn. Absoluuttiset katot "
                            "(price.comparison_cap) ja muutos viimeksi maksettuun (price.changed) ovat erikseen; tämä "
                            "vertaa korvattavaan tuotteeseen.",
    "agent.declared_risk": "Agentti itse ilmoitti epävarmuuden pyynnöstä (declared_risks tarjous- tai tilauskutsussa, "
                           "enintään 5 lyhyttä lausetta): tilaus odottaa ihmistä, joka näkee agentin omat sanat. "
                           "Agentin sanat voivat vain nostaa tasoa, eivät koskaan laskea; ne allekirjoitetaan "
                           "tarjoukseen ja tilauskuittiin.",
    "separate_approver_required": "Neljän silmän periaate: valtakirja vaatii erillisen hyväksyjän, ja hyväksyntä "
                                  "tuli samalta tunnisteelta, jonka pyynnöstä tilaus tehtiin (order.requested_by: "
                                  "agentin tunnisteen luoja, muuten valtakirjan myöntäjä), tai ilman nimeä. Jonkun "
                                  "muun on hyväksyttävä; hylätä saa yhä. Vertailu koskee tunnisteita (hyväksyjän "
                                  "nimi tai pääsyavaimen nimi), ei ihmisiä.",
    "substitution.use_sensitive": "Korvaava tuote ei käy siihen, mihin tuotetta käytetään (rivin käyttötarkoitus: "
                                  "pyynnöstä, reseptistä tai ostajan historiasta): toisen merkin kuohukerma "
                                  "jälkiruokaan (stabilointiaineet muuttavat vaahtoutumista), voi kerrostaikinaan, "
                                  "juotava maito eri rasvaprosentilla. Ihminen vahvistaa, vaikka valtakirja sallisi "
                                  "korvaavat tuotteet itsestään. Tuotetyyppien ja käyttötarkoitusten taulukko on "
                                  "tiedostossa sous/use_sensitivity.py; tuntemattomille käytöille ja tyypeille pätee "
                                  "tavallinen korvaavuussääntö. Muistettu korvaus ei koskaan pehmennä tätä.",
    "product.use_fit": "Riville valittu tuote ei sovi siihen, mihin se tulee (rivin käyttötarkoitus: pyynnöstä, "
                       "tarkoituksesta tai reseptistä): rasvaton maito jälkiruokaan, kahvi- tai ruokakerma "
                       "vaahdotukseen, alle 30 % kerma vaahdotukseen, kahvikerma kastikkeeseen, levite "
                       "kerrostaikinaan. Viesti kertoo syyn ja nimeää samasta hausta sopivan tuotteen, jos sellainen "
                       "on. Tuotteesta, jonka tietoja nimi ei kerro, ei huomauteta. Vaatimukset ovat tiedostossa "
                       "sous/use_fit.py.",
    "product.held": "Valio Aimo on pysäyttänyt tuotteen (takaisinveto, laatupidätys tai myyntikielto; GET "
                    "/holds): sitä ei voi tarjota tilattavana, tilata eikä hyväksyä, ei edes ennen pysäytystä "
                    "annetulla tarjouksella.",
    "amendment.short_pick": "Valio Aimo keräsi vähemmän kuin tilattiin: muutos tehdään heti, summa pienenee ja "
                            "uudelleen allekirjoitetussa kuitissa on hyvitysrivi. Ostajalle kerrotaan.",
    "amendment.line_cancelled": "Valio Aimo ei pysty toimittamaan riviä lainkaan: se perutaan hyvitysrivillä. "
                                "Ostajalle kerrotaan.",
    "amendment.delay": "Valio Aimo siirtää toimituksen myöhemmäksi, ja valtakirjan later_delivery on notify: "
                       "muutos tehdään, ostajalle kerrotaan.",
    "amendment.delay_beyond_mandate": "Valio Aimo siirtää toimituksen myöhemmäksi, ja valtakirjan later_delivery "
                                      "on ask tai block: ostajan hyväksyjän on hyväksyttävä muutos (block: kukaan ei "
                                      "voi hyväksyä, ennen kuin valtakirja sallii sen). Hylkääminen peruu rivit "
                                      "hyvityksellä.",
    "amendment.substitution": "Valio Aimo keräsi toisen tuotteen, ja valtakirja (tarkistettuna korvaavalle "
                              "riville) päästää sen läpi: muutos tehdään, ostajalle kerrotaan.",
    "amendment.substitution_not_allowed": "Valio Aimo keräsi toisen tuotteen, jota valtakirja ei päästä läpi "
                                          "itsestään (korvaavien taso, allergeenit, merkinnät, tuoteryhmä, "
                                          "pysäytykset ... tarkistettuna korvaavalle riville): se odottaa ostajaa. "
                                          "Agentti ei voi hyväksyä sitä, eikä estettyä voi hyväksyä kukaan. "
                                          "Hylkääminen peruu rivin hyvityksellä.",
    "amendment.price_increase": "Valio Aimon keräämä korvaava tuote maksaisi enemmän kuin tilattu rivi: ihminen "
                                "päättää.",
    "supply.terminal": "Tuote toimitetaan toimittajan varastosta (Terminal), joten sen varastosaldoa ei tiedetä.",
    "supply.reliability": "Tuotteen täysimääräisten toimitusten osuus on valtakirjan vähimmäistason alla.",
    "delivery.later_date": "Valio Aimo pystyy toimittamaan vasta pyydetyn päivän jälkeen.",
    "need.partial": "Vain osa tarpeesta voidaan kattaa (varasto tai pakkauskoot): vajaata toimitusta ei koskaan "
                    "tehdä hiljaa.",
    "availability.partial_stock": "Valio Aimolla on varastossa vähemmän kuin tarvitaan.",
    "availability.allocated": "Valio Aimon varasto tälle tuotteelle ja toimituspäivälle on jo varattu Sousin kautta "
                              "tehdyille tai odottaville tilauksille (sous/reservations.py): tilaus pyytää enemmän "
                              "kuin on jäljellä, joten se estetään, ja sen vaihtoehdot tarjoavat jäljellä olevan "
                              "määrän. Kun SOUS_ALLOCATION=pro_rata (Valion asetus, ei valtakirjan), ja kysyntä "
                              "ylittää varaston, kukin ostaja saa osuuden viimeisen 4 viikon tilaustensa "
                              "suhteessa.",
    "price.expires": "Valio Aimon hinta vanhenee ennen toimituspäivää.",
    "availability.data_stale": "Valio Aimoon ei saatu yhteyttä, joten tarjous käyttää viimeisintä tiedossa olevaa "
                               "hintaa tai saldoa: ihminen päättää.",
    "product.type_mismatch": "Saatavilla on vain toisenlaista tuotetta kuin pyydettiin (kahvimaitoa maidon "
                             "sijaan): se tarjotaan, mutta ihminen vahvistaa sen.",
    "unit.interpreted": "Tarpeen yksikkö ei ole tuotteen yksiköitä (20 ST tuotteesta, jota myydään laatikoittain "
                        "ilman yksittäiskappaleen yksikköä), joten se luettiin myyntiyksiköiksi: rivin unit_note "
                        "kertoo sen, ja ihminen vahvistaa määrän. Tuotteen oma yksikkö, tai ST sen ainoalle "
                        "kappaleyksikölle (PRK, BOT, ...), ei laukaise sääntöä. Laukeaa myös, kun nesteen kilot "
                        "(40 kg kermaa) luetaan litroiksi tai toisin päin tuotetyypin tyypillisellä tiheydellä.",
    "quantity.pack_oversize": "Suunnitellut pakkaukset ylittävät litra- tai kilotarpeen yli {over} (12 kg -> "
                              "yksi 20 kg:n säkki, +67 %), eikä pienemmän ylijäämän suunnitelmaa saa korkeintaan "
                              "{price} kalliimmalla: tilaus tehdään, ja ostajalle kerrotaan.",
    "quantity.request_mismatch": "Ostajan omissa sanoissa (buyer_request) mainittua määrää ei vastaa mikään "
                                 "tarjouksen tuote tai rivi (2 % alle tai pakkausylityksen verran yli, litrat, kilot ja "
                                 "pakkaukset erikseen): agentti on voinut lukea pyynnön väärin (\"1 539 litraa\" "
                                 "luettu 539 litraksi), joten ihminen tarkistaa. Monitulkintainen luku (\"1,539 l\") "
                                 "luetaan molemmin tavoin: sääntö laukeaa vain, jos kumpikaan ei vastaa tarjousta.",
    "standing.quantity_creep": "Kestotilauksen tuotteen määrä on yli {creep} suurempi kuin viimeisten {n} "
                               "uusinnan mediaani.",
    "standing.quantity_trend": "Kestotilauksen tuotteen määrä on kasvanut {run} uusinnassa peräkkäin.",
    "standing.price_drift": "Tuotteen yksikköhinta on yli {drift} korkeampi kuin viimeisten {n} uusinnan "
                            "mediaani.",
    "standing.cost_drift": "Uusinnan kokonaissumma on yli {drift} suurempi kuin viimeisten {n} uusinnan mediaani.",
    "standing.product_switched": "Sous valitsi eri tuotteen kuin viime kerralla.",
    "standing.new_terminal": "Tuote toimitetaan nyt toimittajan varastosta (saldo tuntematon); viime kerralla ei.",
    "standing.new_reliability": "Tuotteen toimitusvarmuus on nyt heikko; viime kerralla ei ollut.",
    "agent_auth_required": "Valtakirja vaatii agenttia todistamaan, kuka se on (Authorization: Bearer "
                           "<agentin tunniste>). Kutsu ilman tunnistetta estetään ja kirjataan "
                           "(agent.auth_failed).",
    "agent_auth_invalid": "Agentin tunniste on väärä, peruttu tai toisen valtakirjan; kutsu estetään ja kirjataan "
                          "(agent.auth_failed).",
    "agent_cannot_approve": "Tilausta yritettiin hyväksyä tai hylätä agentin tunnisteella. Odottavista "
                            "tilauksista päättää vain ihminen; yritys estetään ja kirjataan "
                            "(agent.approval_attempt_refused). MCP-palvelimessa ei ole hyväksyntätyökalua lainkaan.",
    "passkey_required": "Valtakirja vaatii hyväksynnän pääsyavaimella: odottavan tilauksen voi hyväksyä vain "
                        "rekisteröidyn hyväksyjän pääsyavaimella, ja allekirjoitus koskee juuri tätä tilausta "
                        "(tunnus, tarjous, summa, rivit, hyväksynnän takaraja). Hyväksyjän yhteisellä koodilla "
                        "tilauksen voi yhä hylätä.",
    "passkey_invalid": "Pääsyavaimella tehty hyväksyntä ei täsmää: väärä allekirjoitus, toiselle tilaukselle tai "
                       "toiminnolle annettu haaste, jo käytetty haaste (toisto), vanhentunut haaste, tilaus on "
                       "muuttunut haasteen jälkeen, tai pääsyavainta ei ole rekisteröity tämän tilauksen "
                       "valtakirjalle tai ostajalle.",
    "passkey_not_registered": "Hyväksyntää pyydettiin pääsyavaimella, mutta tilauksen valtakirjalle ja ostajalle "
                              "ei ole rekisteröity pääsyavainta. Rekisteröi ensin (POST "
                              "/approvers/register/options hyväksyjän koodilla tai testiympäristön "
                              "hyväksyjän tunnisteella).",
    "agent_cannot_accept": "Ostajan agentti yritti hyväksyä Valion muutoksen, jota valtakirja (tarkistettuna "
                           "sillä hetkellä) ei päästäisi läpi ilman ihmistä (ask tai block). Estetään ja kirjataan "
                           "(agent.amendment_accept_refused); agentti saa silti hylätä sen.",
    "amendment_blocked": "Yritettiin hyväksyä Valion muutos, jonka valtakirja nykyisellään estää (esim. korvaava "
                         "tuote, jossa on vältettävä allergeeni): kukaan ei voi hyväksyä sitä. Hylkää se, tai muuta "
                         "ensin valtakirjaa.",
    "quote_used": "Tarjouksella voi tilata vain kerran: toinen tilaus samalla tarjouksella (uusi yritys uudella "
                  "avaimella, tai monta agenttia kilpasilla) estetään. Samanaikaiset tilaukset käsitellään "
                  "yksi kerrallaan.",
    "quote_expired": "Tarjouksen hinta- ja saatavuustiedot ovat liian vanhoja tilattaviksi; pyydä uusi tarjous.",
    "quote_mismatch": "Tarjous on annettu toiselle agentille tai valtakirjalle.",
    "quote_signature": "Tallennettu tarjous ei täsmää allekirjoitukseensa (muutettu tai vioittunut).",
    "mandate_signature": "Valtakirja (tai sen takana oleva ostajan todiste) ei täsmää allekirjoitukseensa.",
    "idempotency_key_reused": "Agentti on jo käyttänyt tätä idempotenssiavainta toiseen tilaukseen; uusi yritys "
                              "samalla avaimella palauttaa alkuperäisen tilauksen. Avaimet ovat agenttikohtaisia.",
    "offer.applied": "Rivin hinta perustuu Valio Aimon erikoistarjoukseen (request_quote ja offer_id): agentti "
                     "käytti myyjän kaupallista tarjousta, joten ostajalle kerrotaan. Kaikki muut säännöt pätevät "
                     "yhä; erikoistarjous ei koskaan ohita rajoja, allergeeneja, tuoteryhmiä eikä uusien tuotteiden "
                     "sääntöä.",
    "offer.expired": "Valio Aimon erikoistarjous, jonka hinnalla tarjous laskettiin, on vanhentunut tai peruttu "
                     "ennen kuin tilaus tehtiin tai hyväksyttiin: pyydä uusi tarjous.",
    "offer.over_cap": "Valio Aimon erikoistarjouksella tehdyt tilaukset ylittäisivät sen määräkaton (kaikki ostajat "
                      "yhteensä; tehdyt ja hyväksyntää odottavat tilaukset lasketaan).",
    "offer_not_found": "Tälle valtakirjalle ei ole tällaista erikoistarjousta (toiselle ostajalle tehty "
                       "erikoistarjous vastaa samoin).",
    "offer_signature_invalid": "Tallennettu erikoistarjous poikkeaa siitä, minkä Valio Aimo allekirjoitti (hinnat, "
                               "määräkatto, voimassaolo, syy tai kohteet): sillä ei voi hinnoitella. Ehdot otetaan "
                               "aina vain tarkistetusta JWS:stä.",
    "offer.signature_invalid": "Tallennettu Valio Aimon erikoistarjous, jonka hinnalla tarjous laskettiin, ei enää "
                               "vastaa allekirjoitettua JWS:ää (muutettu tai vioittunut): tilaus estetään.",
    "offer_expired":"Erikoistarjous on vanhentunut, peruttu tai loppunut.",
    "offer_not_applicable": "Mikään erikoistarjouksen tuotteista ei ole tarjouksen tilattava rivi.",
    "offer_over_cap": "Tarjous pyytää erikoistarjouksen tuotetta enemmän kuin sen määräkatosta on jäljellä.",
    "offer_requires_mandate": "Erikoistarjouksen hinnan saa vain sillä valtakirjalla, jolle se on tehty (anna "
                              "mandate_id).",
    "offer_above_list": "Valion puoli: erikoistarjous, jonka hinta on yli Valion nykyisen hinnan, hylätään; "
                        "erikoistarjous saa vain laskea hintaa.",
    "offer_no_targets": "Valion puoli: suojausten jälkeen ei jäänyt yhtään valtakirjaa, jolle erikoistarjouksen voisi "
                        "tehdä (valtakirjan säännöt, kuten vältettävät allergeenit, tai ostajakohtainen "
                        "aktiivisten erikoistarjousten raja).",
    "too_many_candidates": "Enintään {max} ehdokastuotetta tarjousta kohden.",
    "sandbox_limit": "Tuomariston testiympäristö on käyttänyt osuutensa Valion oikean rajapinta-avaimen kutsuista.",
    "sandbox_expired": "Tuomariston testiympäristö on vanhentunut tai suljettu.",
    "approval_deadline_passed": "Odottavan tilauksen voi hyväksyä vain approve_by-aikaan asti: rivien aikaisin "
                                "tilausaika miinus {margin} minuuttia (tai toimituspäivän alku). Sen jälkeen tila "
                                "on expired_cutoff, mitään ei tilata, ja ostajalle kerrotaan seuraava toimituspäivä, "
                                "jota voi pyytää.",
}
for _c, _t in AVAILABILITY.items():
    RULES[f"availability.{_c.lower()}"] = _t


def rule_text(rule_id: str) -> str | None:
    """The Finnish text of one rule, with the same numbers as its English text."""
    t = RULES.get(rule_id)
    if t is None or "{" not in t:
        return t
    from . import config, deadline, standing
    from .mandate import UNUSUAL_MIN_HISTORY
    return t.format(n=standing.HISTORY_WEEKS if rule_id.startswith("standing.") else UNUSUAL_MIN_HISTORY,
                    creep=ratio(standing.QTY_CREEP), drift=ratio(standing.COST_DRIFT), run=standing.TREND_RUN,
                    over=pct(config.PACK_OVERSIZE_PCT), price=pct(config.PACK_OVERSIZE_PRICE_PCT),
                    max=config.MAX_CANDIDATES_PER_QUOTE, margin=num(deadline.MARGIN_MIN))


# --- findings: one Finnish sentence per finding -----------------------------------------------------------------

def _name(pid: str | None) -> str | None:
    if not pid:
        return None
    from . import catalog
    from .mandate import readable_name
    p = catalog.get(pid) or {}
    return readable_name(p.get("name") or pid)


def fallback(rule: str, product_id: str | None = None) -> str:
    """A finding without its own Finnish sentence: the rule's Finnish text, after the product's name."""
    text = rule_text(rule) or rule
    name = _name(product_id)
    return f"{name}: {text}" if name else text


def finding(f) -> str:
    """Finnish for one Finding (its own message_fi, else the rule's text)."""
    return getattr(f, "message_fi", None) or fallback(f.rule, f.product_id)


def issue(code: str, line: dict, requested: date | None) -> str:
    """Finnish for a quote line issue (sous/quote.py Issue), from the line's own values."""
    su = unit(line.get("sales_unit"))
    if code == "LATER_DATE":
        later = line.get("delivery_date") or line.get("available_date")
        return (f"toimitus {day(requested)} ei onnistu; aikaisin Valio Aimon ilmoittama päivä on {day(later)}."
                if requested and later else "Valio Aimo pystyy toimittamaan vasta pyydetyn päivän jälkeen.")
    if code == "PARTIAL_STOCK":
        # never the stock number: the stock policy (sous/exposure.py) may withhold it from the English message
        qty = line.get("quantity")
        return (f"varasto ei riitä tarvittavaan {num(qty)} {su}." if qty is not None
                else "Valio Aimolla on varastossa vähemmän kuin tarvitaan.")
    if code == "PRICE_EXPIRES":
        return (f"hinta vanhenee ennen toimituspäivää {day(requested)}."
                if requested else RULES["price.expires"])
    if code == "DATA_STALE":
        return ("Valio Aimon tuoreita tietoja ei saatu, joten käytetään viimeisintä tiedossa olevaa hintaa tai "
                "saldoa. Tilausaika tarkistetaan silti nykyhetkeä vasten.")
    if code == "UNIT_INTERPRETED":
        return (unit_note(line, None) or "yksikkö tulkittiin") + ". Ihminen vahvistaa määrän."
    if code == "TYPE_MISMATCH":
        return "tuote on eri tyyppiä kuin pyydettiin. Se tarjotaan, mutta ihminen vahvistaa sen."
    if code == "PACK_OVERSIZE":
        cov = line.get("covers") or {}
        if cov.get("amount") is not None:
            return (f"{num(line.get('quantity') or 0)} {su} kattaa {num(cov['amount'])} {unit(cov.get('unit'))}, "
                    "selvästi enemmän kuin tarvitaan; pienempää ylijäämää ei saa kohtuuhinnalla. Tilaus tehdään, "
                    "ja ostajalle kerrotaan.")
        return rule_text("quantity.pack_oversize")
    if code == "CUTOFF_PASSED" and line.get("order_cutoff"):
        return f"tilausaika umpeutui {when(line['order_cutoff'])}, joten toimitusta {day(requested)} ei voi tilata."
    if code == "ALLOCATED":
        return "Valio Aimon varasto tälle päivälle on jo varattu Sousin kautta tehdyille tilauksille."
    t = AVAILABILITY.get(code)
    return t if t else (rule_text(f"availability.{code.lower()}") or code)


def unit_note(line: dict, asked: float | None) -> str | None:
    """Finnish for a line's unit_note, from the line's quantity, covers and density (and the asked amount when the
    item has one line)."""
    if not line.get("unit_note"):
        return None
    qty, su = line.get("quantity"), unit(line.get("sales_unit"))
    cov = line.get("covers") or {}
    if qty is None:
        return None
    if d := line.get("density"):
        kind = f" ({d['type']})" if d.get("type") else ""
        return (f"kilot ja litrat muunnettiin tuotetyypin{kind} tyypillisellä tiheydellä {num(d['kg_per_l'])} kg/l "
                f"(oletus): {num(qty)} {su} kattaa noin {num(cov.get('amount') or 0)} {unit(cov.get('unit'))}")
    got = f" ({num(cov['amount'])} {unit(cov.get('unit'))})" if cov.get("amount") is not None else ""
    ask = f"pyydettiin {num(asked)} {unit(cov.get('unit'))}, " if asked is not None else ""
    rounded = (", pyöristetty ylöspäin kokonaisiin myyntieriin" if asked is not None and cov.get("amount") is not None
               and cov["amount"] > asked + 1e-9 else "")
    return f"{ask}tilataan {num(qty)} {su}{got}{rounded}"


def unit_notes(lines: list[dict], items: list[dict] | None = None) -> dict[str, str]:
    """{line index: Finnish unit note} for the lines that have a unit_note."""
    per_item: dict[int, int] = {}
    for l in lines:
        if l.get("item_index") is not None:
            per_item[l["item_index"]] = per_item.get(l["item_index"], 0) + 1
    out = {}
    for i, l in enumerate(lines):
        ix = l.get("item_index")
        asked = ((items or [])[ix].get("amount") if items and ix is not None and ix < len(items)
                 and per_item.get(ix) == 1 else None)
        if n := unit_note(l, asked):
            out[str(i)] = n[:1].upper() + n[1:]
    return out


def explanations(findings, q: dict | None = None) -> dict:
    """The unsigned explanations_fi block for a quote response or an order: findings by index (and unit notes by
    line index when the quote is given)."""
    out: dict = {"findings": {str(i): finding(f) for i, f in enumerate(findings)}}
    if q and (notes := unit_notes(q.get("lines") or [], q.get("items"))):
        out["unit_notes"] = notes
    return out


# --- other gateway sentences ------------------------------------------------------------------------------------

HOLD_KIND = {"recall": "vetänyt tuotteen takaisin", "quality_hold": "asettanut tuotteen laatupidätykseen",
             "sales_stop": "asettanut tuotteen myyntikieltoon"}


def hold(h: dict) -> str:
    until = f" {when(h['until'])} asti" if h.get("until") else ""
    return f"Valio Aimo on {HOLD_KIND.get(h['kind'], h['kind'])}{until}: {h['reason']}"


def pause(p: dict) -> str:
    from .pause import ALL
    who = "kaikilta ostajilta" if p["scope"] == ALL else f"ostajalta {p['scope']}"
    return (f"Valio Aimo on keskeyttänyt agenttien tilaukset {who} {when(p['since'])} alkaen: {p['reason']} "
            "Mitään ei voi tilata eikä hyväksyä, ennen kuin Valio Aimo jatkaa tilauksia.")


def allocation(s: dict) -> str:
    left = f"{num(s['left'])} {unit(s.get('sales_unit'))}".strip()
    d = day(s.get("delivery_date"))
    if s["why"] == "share":
        return (f"Varastoa päivälle {d} ei riitä kaikille, ja Valio jakaa sen ostajien viimeisen 4 viikon tilausten "
                f"suhteessa (SOUS_ALLOCATION=pro_rata): sinun osuutesi jättää {left}"
                + (". Katso tilauksen vaihtoehdot." if s["left"] > 0 else "."))
    if s["left"] > 0:
        return (f"Valion varasto päivälle {d} on jo osin varattu Sousin kautta tehdyille tilauksille: vielä voi "
                f"tilata vain {left}. Katso tilauksen vaihtoehdot.")
    return f"Valion varasto päivälle {d} on kokonaan varattu Sousin kautta tehdyille tilauksille; mitään ei ole jäljellä."


def remembered(r: dict) -> str:
    return (f"{r['by']} hyväksyi tämän aiemmin {short_day(r['created_at'])} (tilaus {r['order_id']}) ja pyysi "
            f"muistamaan sen {short_day(r['expires_at'])} asti ({r['id']}; voidaan perua): tilataan kysymättä uudelleen.")


def clearance(name: str, c: dict, avoid_codes) -> str:
    return (f"{name}: allergeenitiedot puuttuvat, mutta {c['by']} vahvisti sen tälle ostajalle vapaaksi "
            f"allergeeneista {allergens(avoid_codes)} {short_day(c['created_at'])} (tilaus {c['order_id']}). "
            f"Vahvistus on voimassa {short_day(c['expires_at'])} asti, ja sen voi perua milloin vain.")


# --- the mandate summary ----------------------------------------------------------------------------------------

TIER_SAYS = {"silent": "menevät läpi", "notify": "menevät läpi ja saat ilmoituksen", "ask": "vaativat hyväksynnän",
             "block": "estetään"}


def _per(u: str) -> str:
    return {"L": "litrahintaiset", "KG": "kilohintaiset"}.get(u, f"{u}-hintaiset")


def summary_text(m, rule: str | None, idx: int = 0) -> str | None:
    """Finnish for one rules.mandate_summary line (rule id and position among lines with the same rule)."""
    from . import catalog  # noqa: F401  (allergen names come from ALLERGENS above)
    e = lambda x: eur(x, whole=True)  # noqa: E731
    if rule == "spend.notify":
        return f"ilmoitus, kun tilaus on yli {e(m.notify_above_eur)}"
    if rule == "spend.auto_limit":
        return f"agentti tilaa itse {e(m.auto_approve_limit_eur)} asti, isommat tilaukset vaativat hyväksynnän"
    if rule == "spend.hard_limit":
        return f"yli {e(m.hard_limit_eur)} tilaukset estetään"
    if rule == "spend.daily_limit":
        return f"yli {e(m.daily_limit_eur)} päivässä vaatii hyväksynnän"
    if rule == "spend.weekly_limit":
        return f"yli {e(m.weekly_limit_eur)} viikossa vaatii hyväksynnän"
    if rule == "spend.split_orders":
        return (f"{m.split_window_minutes} minuutin sisällä tehdyt pienet tilaukset lasketaan yhteen agentin omaa "
                "rajaa vastaan")
    if rule == "price.comparison_cap":
        caps = [f"{k} enintään {e(v)}/l tai kg" for k, v in m.comparison_price_caps.items()]
        caps += [(f"muut {_per(u)} enintään {e(v)}/{unit(u)}" if m.comparison_price_caps
                  else f"kaikki {_per(u)} enintään {e(v)}/{unit(u)}") for u, v in m.max_comparison_price_eur.items()]
        return "hintakatto: " + ", ".join(caps) + "; kalliimmat vaativat hyväksynnän"
    if rule == "price.changed":
        return f"yli {pct(m.max_price_change_pct)} muutos viimeksi maksettuun hintaan vaatii hyväksynnän"
    if rule == "quantity.unusual":
        return f"yli {num(m.unusual_quantity_factor)}-kertainen määrä tavalliseen verrattuna vaatii hyväksynnän"
    if rule == "allergen.contains":
        return (f"vältettävät allergeenit: {allergens(m.avoid_allergens)}"
                + ("" if m.allow_may_contain else " (myös \"voi sisältää\")")
                + f"; ilman allergeenitietoja {'estetään' if m.unknown_allergens == 'block' else 'ihminen päättää'}")
    if rule == "product.claims":
        return "vain tuotteet, joissa merkintä: " + claims(m.required_claims)
    if rule == "origin.restricted" and idx == 0 and m.allowed_origins:
        return (f"vain alkuperämaat {origins(m.allowed_origins)}; muut: "
                f"{'estetään' if m.origin_violation == 'block' else 'ihminen päättää'} (toimittajan ilmoittama)")
    if rule == "origin.restricted":
        return f"ei tuotteita alkuperämaista {origins(m.avoid_origins)}"
    if rule == "origin.unknown":
        return f"tuotteet, joiden alkuperämaata ei tiedetä, {TIER_SAYS[m.origin_unknown]}"
    if rule == "product.category":
        return "vain tuoteryhmät " + ", ".join(m.allowed_categories)
    if rule == "product.first_time":
        known = f" ({len(m.usual_products)} tavallisen tuotteen lisäksi)" if m.usual_products else ""
        return f"uudet tuotteet{known} {TIER_SAYS[m.first_time_products]}"
    if rule == "substitution":
        return f"korvaavat tuotteet {TIER_SAYS[m.substitutions]}"
    if rule in ("purpose.exact", "purpose.ask"):
        from . import catalog as _cat, purpose as _p
        lvl = rule.split(".")[1]
        hits = [(pid, pp) for pid, pp in (m.product_purposes or {}).items() if _p.resolve(pp.purpose, pp.substitution) == lvl]
        if idx < len(hits):
            pid, pp = hits[idx]
            return (f"{(_cat.get(pid) or {}).get('name') or pid} ({pp.purpose}): "
                    + ("vain juuri tämä tuote" if lvl == "exact" else "korvaava vaatii ostajan hyväksynnän"))
    if rule == "substitution.use_sensitive":
        lv = {"exact": "vain sama tuote", "same_spec": "sama tyyppi, rasva ja merkinnät", "type": "sama tyyppi"}
        from .use_sensitivity import USES
        return "korvaavat käyttötarkoituksen mukaan: " + "; ".join(
            f"{k} {USES[u][1]}: {lv[x]}" for k, per in m.substitution_by_use.items() for u, x in per.items())
    if rule == "substitution.premium":
        return (f"korvaavat tuotteet, jotka ovat yli {pct(m.max_substitute_premium_pct)} kalliimpia litralta, "
                f"kilolta tai kappaleelta kuin korvattava tuote, {TIER_SAYS[m.substitute_premium]}")
    if rule == "separate_approver_required":
        return "neljän silmän periaate: agentin käyttöön ottanut ei voi hyväksyä sen odottavia tilauksia"
    if rule == "product.use_fit":
        return f"tuotteet, jotka eivät sovi ilmoitettuun käyttöön, {TIER_SAYS[m.use_fit]}"
    if rule == "supply.terminal":
        return f"toimittajan varastosta tulevat tuotteet {TIER_SAYS[m.terminal_products]}"
    if rule == "supply.reliability":
        return (f"tuotteet, jotka toimitetaan täysimääräisinä alle {ratio(m.min_delivery_reliability)} kerroista, "
                "vaativat hyväksynnän")
    if rule == "delivery.later_date":
        return f"pyydettyä myöhemmät toimitukset {TIER_SAYS[m.later_delivery]}"
    if rule == "agent_auth_required":
        return "agentin pitää esittää oma tunnisteensa"
    if rule == "mandate.expired":
        return f"voimassa {when(m.valid_until)} asti"
    if rule is None and (days := getattr(m, "delivery_days", None)):
        return "toimitus vain " + ", ".join(map(str, days))
    return None
