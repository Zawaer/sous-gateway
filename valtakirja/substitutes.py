"""Substitutes within Valio Aimo's own assortment, ranked good / better / best.

Valio Aimo's official substitution map covers only 196 products, so we build our
own: same category, same unit of measure (L or KG), same temperature class, and
everything the original promised (its nutritional claims and the mandate's
allergen rules).

A category is broad (milk, sour milk and milk drinks share one), so each product
also gets a *type* read from its name: the head of the Finnish compound
('kevytmaito' -> maito, 'kermaviili' -> viili-type sour cream, 'piimä' -> sour
milk), plus fat %, flavour, species, cut form and preparation. The type decides
whether a candidate is recipe-compatible:

  same type:        no compromise
  compatible type:  a minor compromise said in plain words ('milk drink instead of
                    milk', 'higher fat: 3.5% vs 1.5%', 'blueberry instead of strawberry')
  different type:   left out (sour milk for milk, spread for butter, strawberry
                    yogurt for plain); offered as 'good' with a major compromise
                    only when nothing compatible can be delivered

Each candidate lists its compromises against the original in plain language; the
tier follows from them:

  best:   no compromises
  better: only minor ones (somewhat dearer, a certification label lost, different
          pack size, compatible type)
  good:   still usable, but with a real trade-off (much dearer, later delivery,
          low reliability, allergen data missing -> the mandate will ask a human,
          type not recognised, different type as a last resort)

Static filtering first; live price and availability only for the top ~25.
"""

from __future__ import annotations

import json
import re
from datetime import date

from . import catalog, quote as Q
from .mandate import Mandate

SHORTLIST = 25
FALLBACK = 5  # different-type candidates priced in the same batch, used only if nothing compatible remains
_WORD = re.compile(r"[a-zåäö]{3,}")
_STOP = {"the", "and", "with", "ltd", "free", "laktoositon", "lactose", "uht", "pack", "pcs"}


# --- product type -------------------------------------------------------------
# Finnish compounds put the head last ('kevytmaitojuoma' is a drink, 'maitosuklaa'
# chocolate), so a name's type is the longest known head a word ends in. Entries:
# (key, family, English label, Finnish heads, English phrases). Same key = same
# type; same family = compatible (a minor compromise); otherwise different.
# Weak heads (fruit, vegetables, 'kokonainen') name a type only when no strong
# head is in the name: in 'jogurtti 1 kg metsämustikka' the berry is the flavour.
_TYPES: list[tuple[str, str, str, str, str]] = [
    # milk and fermented milk
    ("maito", "milk", "milk", "maito", "milk"),
    ("maitojuoma", "milk", "milk drink", "maitojuoma milkdrink", "milk drink, milkdrink"),
    ("kahvimaito", "milk", "coffee milk", "kahvimaito kahvimaitojuoma", "coffee milk, coffee milk drink"),
    ("kahvijuoma", "coffee drink", "milk coffee drink", "kahvijuoma maitokahvijuoma", "coffee drink, latte"),
    ("piimä", "sour milk", "sour milk", "piimä", "sour milk, sourmilk, buttermilk"),
    ("viili", "viili", "viili", "viili", "viili"),
    ("kefiiri", "kefir", "kefir", "kefiiri", "kefir"),
    ("jogurtti", "yogurt", "yogurt", "jogurtti jugurtti gurtti gurt yoghurt", "yogurt, yoghurt, soygurt"),
    ("rahka", "quark", "quark", "rahka", "quark"),
    ("jogurttijuoma", "yogurt drink", "yogurt drink", "jogurttijuoma smoothie", "yogurt drink, drinking yogurt"),
    ("välipala", "snack", "snack", "välipala", "snack"),
    # cream
    ("kuohukerma", "cream", "whipping cream", "kuohukerma vispikerma", "whipping cream"),
    ("ruokakerma", "cream", "cooking cream", "ruokakerma ruoanvalmistukseen", "cooking cream"),
    ("kahvikerma", "cream", "coffee cream", "kahvikerma", "coffee cream"),
    ("kerma", "cream", "cream", "kerma", "cream"),
    ("smetana", "sour cream", "smetana", "smetana", "smetana"),
    ("ranskankerma", "sour cream", "crème fraîche", "ranskankerma fraiche", "creme fraiche, crème fraîche"),
    ("hapankerma", "sour cream", "sour cream", "hapankerma", "sour cream"),
    ("kermaviili", "sour cream", "kermaviili", "kermaviili", "kermaviili"),
    # cheese
    ("juusto", "cheese", "cheese", "juusto mozzarella cheddar emmental edam gouda parmesan grana halloumi",
     "cheese"),
    ("fetajuusto", "cheese", "feta-type cheese", "feta fetajuusto salaattijuusto", "feta, salad cheese"),
    ("tuorejuusto", "cream cheese", "cream cheese", "tuorejuusto", "cream cheese"),
    ("raejuusto", "cottage cheese", "cottage cheese", "raejuusto", "cottage cheese"),
    ("sulatejuusto", "processed cheese", "processed cheese", "sulatejuusto", "processed cheese"),
    ("leipäjuusto", "bread cheese", "bread cheese", "leipäjuusto", "bread cheese"),
    # fats
    ("voi", "butter", "butter", "voi kirnuvoi", "butter"),
    ("levite", "spread", "spread", "levite rasvalevite rasvaseos öljyseos kasvivoi oivariini",
     "spread, butter blend, fat spread"),
    ("margariini", "spread", "margarine", "margariini", "margarine"),
    ("pähkinävoi", "nut butter", "nut butter", "pähkinävoi", "peanut butter, nut butter"),
    ("öljy", "oil", "oil", "öljy", "oil"),
    # eggs
    ("muna", "egg", "eggs", "muna kananmuna", "egg, eggs"),
    ("munamassa", "egg", "liquid egg", "munamassa", "liquid egg"),
    ("valkuainen", "egg white", "egg white", "valkuainen munanvalkuainen", "egg white"),
    ("keltuainen", "egg yolk", "egg yolk", "keltuainen", "egg yolk"),
    # ice cream
    ("jäätelö", "ice cream", "ice cream", "jäätelö kermajäätelö irtojäätelö", "ice cream"),
    ("jäätelöannos", "ice cream", "ice cream portion",
     "tuutti jäätelötuutti puikko jäätelöpuikko pikari jäätelöpikari", "ice cream cone, ice cream stick"),
    ("sorbetti", "ice cream", "sorbet", "sorbetti", "sorbet"),
    # plant drinks
    ("kasvijuoma", "plant drink", "plant drink", "kaurajuoma soijajuoma mantelijuoma riisijuoma kookosjuoma",
     "oat drink, soy drink, almond drink, rice drink, oat milk"),
    # meat cuts (the species is separate)
    ("filee", "boneless cut", "fillet", "filee file fileet rintafilee sisäfilee ulkofilee",
     "fillet, fillets, breast fillet"),
    ("rinta", "boneless cut", "breast", "rinta", "breast"),
    ("paisti", "boneless cut", "roast", "paisti sisäpaisti ulkopaisti kulmapaisti paahtopaisti", "roast, roast beef"),
    ("leike", "boneless cut", "cutlet", "leike paistileike", "cutlet, escalope, schnitzel"),
    ("pihvi", "boneless cut", "steak", "pihvi", "steak"),
    ("lapa", "boneless cut", "shoulder", "lapa", "shoulder"),
    ("niska", "boneless cut", "neck", "niska", "neck"),
    ("selkä", "boneless cut", "loin", "selkä etuselkä", "loin"),
    ("poski", "boneless cut", "cheek", "poski poskiliha", "cheek"),
    ("potka", "shank", "shank", "potka potkaliha", "shank"),
    ("sisäelin", "offal", "offal", "kieli munuainen sydän", "tongue, kidney, heart"),
    ("hampurilaispihvi", "patty", "burger patty",
     "hampurilaispihvi burgeripihvi täyslihahampurilaispihvi murekepihvi", "burger patty, patty, patties"),
    ("jauheliha", "mince", "minced meat", "jauheliha jauhis", "minced meat, mince, ground beef"),
    ("kyljys", "chop", "chop", "kyljys", "chop, chops"),
    ("kylki", "ribs", "ribs", "kylki grillikylki ribsit", "ribs, belly"),
    ("koipireisi", "leg", "leg", "koipireisi reisi koipi reisifilee", "leg, thigh, drumstick"),
    ("siipi", "wing", "wings", "siipi siivet", "wing, wings"),
    ("kinkku", "ham", "ham", "kinkku", "ham"),
    ("pekoni", "bacon", "bacon", "pekoni", "bacon"),
    ("makkara", "sausage", "sausage", "makkara nakki nakit lenkki wursti bratwursti", "sausage, sausages, frankfurter"),
    ("leikkele", "cold cuts", "cold cuts", "leikkele", "cold cuts"),
    ("lihapulla", "meatballs", "meatballs", "lihapulla lihapullat pyörykkä pyörykät", "meatballs, meatball"),
    ("mureke", "meatloaf", "meatloaf", "mureke", "meatloaf"),
    ("maksa", "liver", "liver", "maksa", "liver"),
    ("kalapuikko", "fish fingers", "fish fingers", "kalapuikko kalapuikot", "fish fingers, fish sticks"),
    ("kokonainen", "whole", "whole", "kokonainen kokonaisena", "whole"),
    # bakery
    ("leipä", "bread", "bread", "leipä maalaisleipä vaalealeipä", "bread, loaf"),
    ("ruisleipä", "bread", "rye bread", "ruisleipä", "rye bread"),
    ("paahtoleipä", "bread", "toast bread", "paahtoleipä", "toast bread, toast"),
    ("näkkileipä", "crispbread", "crispbread", "näkkileipä", "crispbread"),
    ("pikkuleipä", "cookie", "cookies", "pikkuleipä keksi", "cookie, cookies, biscuit, biscuits"),
    ("korppu", "rusk", "rusks", "korppu", "rusk, rusks"),
    ("sämpylä", "roll", "bread roll", "sämpylä", "roll, rolls, bun, buns"),
    ("hampurilaissämpylä", "burger bun", "burger bun", "hampurilaissämpylä burgersämpylä", "burger bun"),
    ("patonki", "baguette", "baguette", "patonki", "baguette"),
    ("croissant", "croissant", "croissant", "croissant voicroissant", "croissant"),
    ("muffini", "muffin", "muffin", "muffini muffinssi muffin", "muffin, muffins"),
    ("munkki", "doughnut", "doughnut", "munkki donitsi donut", "doughnut, donut"),
    ("pulla", "sweet bun", "sweet bun", "pulla korvapuusti", "sweet bun, cinnamon bun"),
    ("piirakka", "pie", "pie / pasty", "piirakka piiras", "pie, pasty"),
    ("pasteija", "pasty", "pasty", "pasteija", "pasty"),
    ("kakku", "cake", "cake", "kakku cheesecake", "cake, cheesecake"),
    ("pizza", "pizza", "pizza", "pizza", "pizza"),
    ("tortilla", "tortilla", "tortilla", "tortilla", "tortilla, wrap"),
    # dry goods and sauces
    ("jauho", "flour", "flour", "jauho jauhot", "flour"),
    ("pasta", "pasta", "pasta", "pasta penne spagetti fusilli farfalle linguine", "pasta, spaghetti, penne"),
    ("makaroni", "pasta", "macaroni", "makaroni", "macaroni"),
    ("nuudeli", "noodles", "noodles", "nuudeli", "noodles, noodle"),
    ("riisi", "rice", "rice", "riisi", "rice"),
    ("sokeri", "sugar", "sugar", "sokeri", "sugar"),
    ("kastike", "sauce", "sauce", "kastike", "sauce"),
    ("majoneesi", "mayonnaise", "mayonnaise", "majoneesi", "mayonnaise"),
    ("liemi", "stock", "stock", "liemi fondi fond liemijauhe", "stock, bouillon, fond"),
    ("hillo", "jam", "jam", "hillo marmeladi", "jam, marmalade"),
    ("mehu", "juice", "juice", "mehu täysmehu", "juice"),
    ("mehujuoma", "juice", "juice drink", "mehujuoma", "juice drink"),
    ("virvoitusjuoma", "soft drink", "soft drink", "virvoitusjuoma limonadi", "soft drink, soda"),
    ("salaatti", "salad", "salad", "salaatti", "salad, lettuce"),
    ("laatikko", "casserole", "casserole", "laatikko", "casserole"),
    ("keitto", "soup", "soup", "keitto", "soup"),
    # produce (weak heads: a fruit in a yogurt's name is its flavour)
    ("tomaatti", "tomato", "tomatoes", "tomaatti", "tomato, tomatoes"),
    ("kirsikkatomaatti", "tomato", "cherry tomatoes", "kirsikkatomaatti", "cherry tomato, cherry tomatoes"),
    ("kurkku", "cucumber", "cucumber", "kurkku", "cucumber"),
    ("kurpitsa", "squash", "squash", "kurpitsa", "squash, pumpkin"),
    ("peruna", "potato", "potatoes", "peruna", "potato, potatoes"),
    ("bataatti", "sweet potato", "sweet potatoes", "bataatti", "sweet potato"),
    ("sipuli", "onion", "onions", "sipuli", "onion, onions"),
    ("punasipuli", "onion", "red onions", "punasipuli", "red onion"),
    ("valkosipuli", "garlic", "garlic", "valkosipuli", "garlic"),
    ("porkkana", "carrot", "carrots", "porkkana", "carrot, carrots"),
    ("paprika", "pepper", "peppers", "paprika", "pepper, peppers, bell pepper"),
    ("kaali", "cabbage", "cabbage", "kaali keräkaali", "cabbage"),
    ("kukkakaali", "cauliflower", "cauliflower", "kukkakaali", "cauliflower"),
    ("parsakaali", "broccoli", "broccoli", "parsakaali", "broccoli"),
    ("herne", "pea", "peas", "herne herneet", "pea, peas"),
    ("papu", "bean", "beans", "papu pavut", "bean, beans"),
    ("maissi", "corn", "corn", "maissi", "corn, sweetcorn"),
    ("sieni", "mushroom", "mushrooms", "sieni herkkusieni", "mushroom, mushrooms"),
    ("omena", "apple", "apples", "omena", "apple, apples"),
    ("päärynä", "pear", "pears", "päärynä", "pear, pears"),
    ("banaani", "banana", "bananas", "banaani", "banana, bananas"),
    ("appelsiini", "orange", "oranges", "appelsiini", "orange, oranges"),
    ("sitruuna", "lemon", "lemons", "sitruuna", "lemon, lemons"),
    ("mansikka", "strawberry", "strawberries", "mansikka", "strawberry, strawberries"),
    ("mustikka", "blueberry", "blueberries", "mustikka", "blueberry, blueberries"),
    ("vadelma", "raspberry", "raspberries", "vadelma", "raspberry, raspberries"),
]
_WEAK = {"kokonainen", "tomaatti", "kirsikkatomaatti", "kurkku", "kurpitsa", "peruna", "bataatti", "sipuli", "punasipuli",
         "valkosipuli", "porkkana", "paprika", "kaali", "kukkakaali", "parsakaali", "herne", "papu", "maissi",
         "sieni", "omena", "päärynä", "banaani", "appelsiini", "sitruuna", "mansikka", "mustikka", "vadelma"}
_TYPE_INFO = {k: (fam, label) for k, fam, label, _, _ in _TYPES}
_FI_HEADS = sorted(((h, k) for k, _, _, fi, _ in _TYPES for h in fi.split()), key=lambda x: -len(x[0]))
_EN_HEADS = sorted(((h.strip(), k) for k, _, _, _, en in _TYPES for h in en.split(",")), key=lambda x: -len(x[0]))
# Same family, but the candidate cannot do the original's job (cooking cream does not whip).
_ONE_WAY = {("kuohukerma", "ruokakerma"), ("kuohukerma", "kahvikerma"), ("kuohukerma", "kerma")}

# Families whose fat % and dairy-vs-plant origin matter.
_DAIRY = {"milk", "sour milk", "viili", "kefir", "yogurt", "quark", "cream", "sour cream", "cheese", "cream cheese",
          "cottage cheese", "processed cheese", "butter", "ice cream", "snack"}
_FAT_FAMILIES = _DAIRY | {"mince", "spread", "patty", "sausage"}
# Plant-based stand-ins: the head's own prefix ('kaurajuoma', 'kookoskerma') or a marker word. Not any
# 'kaura' in the name: 'jogurtti ja kauramuro' is dairy yogurt with oat crunch.
_PLANT_PREFIX = ("kaura", "soija", "soy", "kookos", "mantel", "herne", "kasvi", "riisi")
_PLANT_WORDS = ("kasvipohj", "kaurapohj", "soijapohj", "vegaan", "vegan", "kasvirasva", "oddlygood", "oatly",
                "alpro", "planti", "soygurt", "plant")
# Species: word stem -> (species, group). Same group = compatible.
_SPECIES: list[tuple[str, str, str]] = [
    ("kananpo", "chicken", "chicken"), ("kana", "chicken", "chicken"), ("broiler", "chicken", "chicken"),
    ("chicken", "chicken", "chicken"), ("kalkkun", "turkey", "turkey"), ("turkey", "turkey", "turkey"),
    ("naud", "beef", "beef"), ("nauta", "beef", "beef"), ("härkä", "beef", "beef"), ("beef", "beef", "beef"),
    ("vasik", "veal", "beef"), ("porsa", "pork", "pork"), ("possu", "pork", "pork"), ("sian", "pork", "pork"),
    ("sika", "pork", "pork"), ("pork", "pork", "pork"), ("lamma", "lamb", "lamb"), ("lampa", "lamb", "lamb"),
    ("karits", "lamb", "lamb"), ("poro", "reindeer", "reindeer"), ("hirv", "elk", "elk"), ("ankka", "duck", "duck"),
    ("ankan", "duck", "duck"), ("kirjolohi", "rainbow trout", "salmonid"), ("kirjolohe", "rainbow trout", "salmonid"),
    ("lohi", "salmon", "salmonid"), ("lohe", "salmon", "salmonid"), ("salmon", "salmon", "salmonid"),
    ("nieriä", "char", "salmonid"), ("silak", "herring", "herring"), ("silli", "herring", "herring"),
    ("muiku", "vendace", "vendace"), ("muikk", "vendace", "vendace"), ("ahven", "perch", "white fish"),
    ("kuha", "pike-perch", "white fish"), ("turs", "cod", "white fish"), ("seiti", "saithe", "white fish"),
    ("kolja", "haddock", "white fish"), ("pangasius", "pangasius", "white fish"), ("tilapia", "tilapia", "white fish"),
    ("hauki", "pike", "white fish"), ("hauen", "pike", "white fish"), ("siika", "whitefish", "white fish"),
    ("siian", "whitefish", "white fish"), ("tonnikal", "tuna", "tuna"), ("katkarap", "shrimp", "shrimp"),
    ("katkarav", "shrimp", "shrimp"),
]
_NOT_SPECIES = ("kanaton", "kanela", "sikuri", "porok", "lohko", "kanapee", "kanaa")
_PREFIX_ONLY = {"sika", "sian", "kuha", "poro", "turs", "hirv", "naud", "kana", "lohe", "lohi", "silli", "ankka"}
# Cut / shape: a minor difference ('strips instead of whole').
_FORMS = [("suikaleet", "strips"), ("suikale", "strips"), ("kuutiot", "cubes"), ("kuutio", "cubes"),
          ("viipaleet", "slices"), ("viipale", "slices"), ("viipaloitu", "slices"), ("viipaloidut", "slices"),
          ("raaste", "grated"), ("raastettu", "grated"), ("murska", "crushed"), ("rouhe", "crushed"),
          ("palat", "pieces"), ("pala", "pieces"), ("paloiteltu", "pieces"), ("lohkot", "wedges"),
          ("lohko", "wedges"), ("siivutettu", "slices"), ("siivu", "slices"),
          ("silppu", "shredded"), ("muru", "flakes"), ("nyhtö", "pulled"), ("bites", "pieces"), ("puolikas", "halves"), ("puolikkaat", "halves")]
# Preparation: a cooked, smoked, cured or breaded cut of meat or fish is a different product from a raw
# one (elsewhere, e.g. part-baked vs baked bread, a minor difference).
# Matched anywhere in a word ('maissipaneroitu', 'kylmäsavu', 'ylikypsä', 'esipaistettu').
_PREP = [("paneroi", "breaded"), ("paneerattu", "breaded"), ("leivitetty", "breaded"), ("savu", "smoked"),
         ("graavi", "cured"), ("kypsä", "cooked"), ("kypsennetty", "cooked"), ("paistettu", "cooked"),
         ("grillattu", "cooked"), ("paahdettu", "cooked"), ("sous", "cooked")]
_RAW_CUTS = {None, "boneless cut", "mince", "chop", "ribs", "leg", "wing", "whole"}
_SEASONED = ("maustettu", "maustetut", "marinoitu", "marinoidut", "marinadi", "suolattu")  # keep 'suolattu' last
_PLAIN = ("maustamat", "luonnon", "natural", "naturel", "plain", "unflavoured")
# Flavour stems (Finnish) -> English, matched anywhere in a word except the head itself.
_FLAVOURS: dict[str, str] = {
    "mansikka": "strawberry", "mustikka": "blueberry", "vadelma": "raspberry", "metsämarja": "forest berry",
    "kuningatar": "raspberry-blueberry", "kirsikka": "cherry", "persikka": "peach", "päärynä": "pear",
    "omena": "apple", "banaani": "banana", "ananas": "pineapple", "mango": "mango", "passion": "passion fruit",
    "appelsiini": "orange", "sitruuna": "lemon", "limetti": "lime", "greippi": "grapefruit", "vanilja": "vanilla",
    "suklaa": "chocolate", "kinuski": "caramel", "karamelli": "caramel", "toffee": "toffee", "kahvi": "coffee",
    "minttu": "mint", "lakritsi": "liquorice", "salmiakki": "salmiakki", "kookos": "coconut",
    "pähkinä": "nut", "nougat": "nougat", "mustaherukka": "blackcurrant", "herukka": "currant",
    "raparperi": "rhubarb", "puolukka": "lingonberry", "karpalo": "cranberry", "lakka": "cloudberry",
    "tyrni": "sea buckthorn", "aprikoosi": "apricot", "luumu": "plum", "hunaja": "honey", "kaneli": "cinnamon",
    "piparkakku": "gingerbread", "kardemumma": "cardamom", "hedelmä": "fruit", "marja": "berry",
    "trooppinen": "tropical", "valkosipuli": "garlic", "yrtti": "herb", "chili": "chili", "pippuri": "pepper",
    "curry": "curry", "tandoori": "tandoori", "fajita": "fajita", "bbq": "bbq", "teriyaki": "teriyaki",
    "sinappi": "mustard", "paprika": "paprika", "juusto": "cheese", "sipuli": "onion", "tomaatti": "tomato",
    "pesto": "pesto",
}
# Salt level (butter, meat): a minor difference.
_SALT = [("suolaamat", "unsalted"), ("vähemmän suola", "less salt"), ("vähäsuola", "less salt"),
         ("voimakassuola", "extra salted"), ("suolainen", "salted")]
_ACCENTS = str.maketrans("éèêëîïôûüáàâçñ", "eeeeiiouuaaacn")
_EN_FLAVOURS = set(_FLAVOURS.values())
_PCT = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
_LETTERS = re.compile(r"[a-zåäö]+")


def _depluralise(w: str) -> str:
    # nominative plural: 'sämpylät' -> 'sämpylä', 'munat' -> 'muna'
    return w[:-1] if len(w) > 4 and w.endswith("t") and w[-2] in "aeiouyäö" else w


def _head(word: str) -> tuple[str, str] | None:
    """(type key, compound prefix) for a word ending in a known head, else None."""
    for w in (word, _depluralise(word)):
        for h, k in _FI_HEADS:
            if w.endswith(h) and (len(w) == len(h) or len(h) >= 4 or len(w) - len(h) >= 3):
                return k, w[: len(w) - len(h)]
    return None


def _clean(name: str, brand: str) -> str:
    n = (name or "").lower().replace("®", " ").replace("™", " ").translate(_ACCENTS)
    b = (brand or "").lower().strip()
    if len(b) >= 3:
        n = n.replace(b, " ")
    return n


def product_type(p: dict) -> dict:
    """What the product is, read from its name (Finnish first, English as backup).

    {'key': 'maito', 'label': 'milk', 'family': 'milk', 'fat_pct': 1.5, 'flavour': None,
     'species': None, 'form': None, 'prep': None, 'plant_based': False}
    Unknown parts are None; key None means the name did not reveal a type.
    """
    names = p.get("names") or {}
    fi = _clean(names.get("fi") or "", p.get("brand") or "")
    en = _clean(names.get("en") or p.get("name") or "", p.get("brand") or "")
    words = _LETTERS.findall(fi)
    key, prefix, head_i, forms = None, "", -1, []
    strong, weak = None, None
    for i, w in enumerate(words):
        stem = w
        for f, label in _FORMS:
            if w.endswith(f):
                forms.append(label)
                stem = w[: len(w) - len(f)]
                break
        if len(stem) < 3:
            continue
        hit = _head(stem)
        if hit:
            if hit[0] in _WEAK:
                weak = (hit[0], hit[1], i)
            else:
                strong = (hit[0], hit[1], i)
    if strong or weak:
        key, prefix, head_i = strong or weak
    if key is None and en and not words:
        best = None
        for h, k in _EN_HEADS:
            for m in re.finditer(rf"\b{re.escape(h)}\b", en):
                if best is None or m.end() > best[0] or (m.end() == best[0] and len(h) > best[1]):
                    best = (m.end(), len(h), k)
        if best:
            key = best[2]
    family, label = _TYPE_INFO.get(key, (None, None))
    text = fi if words else en
    tokens = words if words else _LETTERS.findall(en)

    # flavour: in any word but the head, or in the head's compound prefix ('mansikkajogurtti')
    flavour = None
    plain = any(w.startswith(_PLAIN) for w in tokens)
    if not any(w.startswith(("maustamat", "unflavoured")) for w in tokens):
        parts = [prefix] + [w for i, w in enumerate(tokens) if i != head_i]
        for part in parts:
            if not part:
                continue
            for stem, en_name in _FLAVOURS.items():
                if stem in part and not (key and stem == key):
                    flavour = en_name
                    break
            if flavour is None and not words and part in _EN_FLAVOURS:
                flavour = part
            if flavour:
                break
        if flavour is None and any(s in w for w in tokens for s in _SEASONED):
            flavour = "lightly salted" if not any(s in w for w in tokens for s in _SEASONED[:-1]) else "seasoned"
    plain = plain and flavour is None

    species = None
    for w in tokens:
        if w.startswith(_NOT_SPECIES):
            continue
        for stem, sp, group in _SPECIES:
            # 'maalaiskanan', 'kylmäsavulohi': compounds name the species inside the word
            if w.startswith(stem) or (stem not in _PREFIX_ONLY and stem in w) or (
                    stem in ("kana", "lohi") and re.search(rf"[a-zåäö]{{3}}{stem}", w) and not w.endswith("kanaton")):
                species = (sp, group)
                break
        if species:
            break

    prep = next((label for s, label in _PREP for w in tokens if s in w), None)
    joined = " ".join(tokens)
    salt = next((label for s, label in _SALT if s in joined), None)

    fat = None
    if family in _FAT_FAMILIES:
        for m in _PCT.finditer(text):
            v = float(m.group(1).replace(",", "."))
            if v < 60:
                fat = v
                break
        if fat is None:
            if "rasvaton" in joined or "fat free" in joined or ("skimmed" in joined and "semi" not in joined):
                fat = 0.0
            elif family == "milk":  # Finnish milk grades
                for marks, v in (("ykkös", 1.0), ("kevyt semi", 1.5), ("täys whole", 3.5)):
                    if any(m in joined for m in marks.split()):
                        fat = v
                        break

    plant = family in _DAIRY and (prefix.startswith(_PLANT_PREFIX) or any(w.startswith(_PLANT_WORDS) for w in tokens))
    return {"key": key, "label": label, "family": family, "fat_pct": fat, "flavour": flavour,
            "species": species[0] if species else None, "species_group": species[1] if species else None,
            "form": forms[0] if forms else None, "prep": prep, "plant_based": plant, "plain": plain, "salt": salt}


def compare_types(o: dict, c: dict) -> tuple[str, list[str], list[str]]:
    """('same' | 'compatible' | 'different' | 'unknown', minor reasons, major reasons) for candidate c vs original o."""
    minor, different = [], []
    unknown = False
    if o["key"]:
        if not c["key"]:
            unknown = True
        elif c["key"] != o["key"]:
            if c["family"] == o["family"] and (o["key"], c["key"]) not in _ONE_WAY:
                minor.append(f"{c['label']} instead of {o['label']}")
            else:
                different.append(f"{c['label']} instead of {o['label']}")
    if o["plant_based"] != c["plant_based"] and (o["family"] in _DAIRY or c["family"] in _DAIRY):
        different.append("plant-based instead of dairy" if c["plant_based"] else "dairy instead of plant-based")
    if o["species"] and c["species"] and o["species"] != c["species"]:
        (minor if o["species_group"] == c["species_group"] else different).append(
            f"{c['species']} instead of {o['species']}")
    if o["flavour"] != c["flavour"]:
        if o["flavour"] is None:
            (minor if c["flavour"] == "lightly salted" else different).append(f"{c['flavour']} instead of plain")
        elif c["flavour"] is None:
            minor.append(f"plain instead of {o['flavour']}")
        else:
            minor.append(f"{c['flavour']} instead of {o['flavour']}")
    elif o["plain"] and not c["plain"]:
        minor.append("not labelled plain / unflavoured")
    if o["prep"] != c["prep"] and (o["species"] or c["species"]):
        what = f"{c['prep'] or 'raw'} instead of {o['prep'] or 'raw'}"
        if o["family"] in _RAW_CUTS:
            different.append(what)
        else:
            minor.append(what)
    if o["salt"] != c["salt"] and o["salt"] and c["salt"]:
        minor.append(f"{c['salt']} instead of {o['salt']}")
    if o["form"] != c["form"] and (o["form"] or c["form"]):
        minor.append(f"{c['form'] or 'whole'} instead of {o['form'] or 'whole'}")
    if o["fat_pct"] is not None and c["fat_pct"] is not None and abs(o["fat_pct"] - c["fat_pct"]) > 0.05:
        word = "higher" if c["fat_pct"] > o["fat_pct"] else "lower"
        minor.append(f"{word} fat: {c['fat_pct']:g}% vs {o['fat_pct']:g}%")
    if different:
        return "different", minor, different
    if unknown:
        return "unknown", minor, [f"could not confirm from its name that it is {o['label']}"]
    return ("compatible" if minor else "same"), minor, []


# --- pack size ------------------------------------------------------------------
_N = r"(\d+(?:[.,]\d+)?)"
_U = r"(kg|g|l|dl|cl|ml)(?![a-wyzåäö])"  # '350gx6' is a size followed by a count
_COUNT_SIZE = re.compile(rf"(\d+)\s*[x×]\s*{_N}\s*{_U}", re.I)
_SIZE_COUNT = re.compile(rf"{_N}\s*{_U}\s*[x×]\s*(\d+)\b", re.I)
_LEAD_COUNT = re.compile(r"^\s*(\d+)\s*[x×]\s*\D", re.I)
_SIZE = re.compile(rf"{_N}\s*{_U}", re.I)


def pack(name: str) -> dict | None:
    """Pack from a name: {'count': 10, 'size': '1 l', 'total': 10.0, 'measure': 'L', 'text': '10 x 1 l'}."""
    if not name:
        return None
    count, num, unit = 1, None, None
    if m := _COUNT_SIZE.search(name):
        count, num, unit = int(m.group(1)), m.group(2), m.group(3)
    elif m := _SIZE_COUNT.search(name):
        num, unit, count = m.group(1), m.group(2), int(m.group(3))
    else:
        sizes = list(_SIZE.finditer(name))
        if not sizes:
            return None
        # 'n3kg/n150g', '5 kg (á 150 g)': the pack is the largest size, the rest are pieces
        biggest = max(sizes, key=lambda m: float(m.group(1).replace(",", ".")) * catalog._TO_BASE[m.group(2).lower()][1])
        num, unit = biggest.group(1), biggest.group(2)
        if m := _LEAD_COUNT.match(name):
            count = int(m.group(1))
    unit = unit.lower()
    measure, factor = catalog._TO_BASE[unit]
    one = float(num.replace(",", ".")) * factor
    size = f"{num} {unit}"
    return {"count": count, "size": size, "total": round(count * one, 6), "unit_amount": round(one, 6),
            "measure": measure, "text": f"{count} x {size}" if count > 1 else size}


def _pack_of(p: dict) -> dict | None:
    names = p.get("names") or {}
    return pack(names.get("fi") or names.get("en") or p.get("name") or "")


def _pack_note(o: dict | None, c: dict | None) -> str | None:
    if not o or not c or o["measure"] != c["measure"]:
        return None
    if o["count"] == c["count"] and abs(o["unit_amount"] - c["unit_amount"]) < 1e-9:
        return None
    return f"{c['text']} instead of {o['text']}"


# --- ranking ------------------------------------------------------------------

def _words(p: dict) -> set[str]:
    text = " ".join(v for v in p["names"].values() if v).lower()
    return {w for w in _WORD.findall(text) if w not in _STOP}


def _same_category(category: str) -> list[dict]:
    rows = catalog._db().execute(
        "SELECT data FROM product WHERE json_extract(data, '$.category') = ?", (category,)).fetchall()
    return [json.loads(d) for (d,) in rows]


_TYPE_BONUS = {"same": 3.0, "compatible": 1.5, "unknown": 0.0, "different": -5.0}


def _static_score(orig: dict, cand: dict, ow: set[str], match: str = "unknown", minor: int = 0,
                  same_pack: bool = False) -> float:
    cw = _words(cand)
    sim = len(ow & cw) / max(1, len(ow | cw))
    score = 3 * sim
    score += 2 if cand["id"] in orig["official_substitutes"] else 0
    score += 0.5 * len(set(orig["labels"]) & set(cand["labels"]))
    score += 0.5 if cand["allergens"]["declared"] else 0
    score += 0.3 if cand["brand"] and cand["brand"] == orig["brand"] else 0
    score += _TYPE_BONUS[match] - 0.4 * minor
    score += 0.5 if same_pack else 0
    return score


def suggest(
    product_id: str,
    delivery_date: date,
    amount: float,
    unit: str | None,
    mandate: Mandate | None = None,
    avoid_allergens: list[str] | None = None,
    limit: int = 3,
) -> dict:
    orig = catalog.get(product_id)
    if not orig:
        raise KeyError(product_id)
    avoid = sorted(set(avoid_allergens or []) | set(mandate.avoid_allergens if mandate else []))
    required_claims = set(orig["claims"]) & {
        "FREE_FROM_LACTOSE", "FREE_FROM_GLUTEN", "VEGAN", "VEGETARIAN", "LOW_LACTOSE", "FREE_FROM_MILK",
        "FREE_FROM_EGGS", "FREE_FROM_NUTS"} | set(mandate.required_claims if mandate else [])
    measure = (orig["size_from_name"] or [None, None])[1]
    otype, opack = product_type(orig), _pack_of(orig)

    # 1. static filter: hard constraints never relaxed
    pool, held_back = [], []
    excluded = {"allergen": 0, "claims": 0, "measure_or_temperature": 0, "product_type": 0}
    category = [(c, product_type(c)) for c in _same_category(orig["category"])]
    # A name that names no type ('Valio Oltermanni') in a category that is nearly all the original's
    # kind (cheese) is very likely that kind too; elsewhere it stays unconfirmed.
    families = [t["family"] for _, t in category if t["family"]]
    dominant = otype["key"] not in _WEAK and bool(otype["family"]) and len(families) >= 5 and families.count(otype["family"]) >= 0.8 * len(families)
    ow = _words(orig)
    for c, ctype in category:
        if c["id"] == orig["id"] or c["deleted"]:
            continue
        if orig["temperature"] and c["temperature"] and c["temperature"] != orig["temperature"]:
            excluded["measure_or_temperature"] += 1
            continue
        if measure and c["size_from_name"] and c["size_from_name"][1] != measure:
            excluded["measure_or_temperature"] += 1
            continue
        if not required_claims <= set(c["claims"]):
            excluded["claims"] += 1
            continue
        if avoid and catalog.allergen_status(c, avoid, mandate.allow_may_contain if mandate else False)["status"] in (
                "contains", "may_contain"):
            excluded["allergen"] += 1
            continue
        cpack = _pack_of(c)
        match, minor, major = compare_types(otype, ctype)
        if match == "unknown" and dominant:
            match, major = "compatible", []
            minor = minor + [f"name does not say it is {otype['label']} (most of this category is)"]
        pack_note = _pack_note(opack, cpack)
        entry = {"p": c, "type": ctype, "match": match, "type_minor": minor, "type_major": major,
                 "pack_note": pack_note,
                 "pack": cpack and cpack["text"],
                 "score": _static_score(orig, c, ow, match, len(minor), bool(opack and not pack_note))}
        if match == "different":
            excluded["product_type"] += 1
            held_back.append(entry)
        else:
            pool.append(entry)
    pool.sort(key=lambda e: -e["score"])
    held_back.sort(key=lambda e: -e["score"])
    shortlist = pool[:SHORTLIST] + held_back[:FALLBACK]

    # 2. live price and availability for the original + shortlist (two batched calls)
    offers, _ = Q.offers([orig["id"]] + [e["p"]["id"] for e in shortlist], delivery_date, amount, unit)
    by_id = {o["product_id"]: o for o in offers}
    o_orig = by_id.get(orig["id"])
    ref_price = (o_orig or {}).get("comparison_price") or {}
    ref_cp = ref_price.get("ex_vat")

    # 3. compromises -> tier
    options = []
    for e in shortlist:
        c = e["p"]
        o = by_id.get(c["id"])
        if not o or not o["orderable"]:
            continue
        minor, major = list(e["type_minor"]), []
        if e["match"] == "different":
            major += ["different product type: " + m for m in e["type_major"]]
        else:
            major += e["type_major"]
        cp = (o.get("comparison_price") or {}).get("ex_vat")
        delta = None
        if cp is not None and ref_cp:
            delta = (cp - ref_cp) / ref_cp
            if delta > 0.25:
                major.append(f"{delta:+.0%} per {ref_price.get('per')} vs the original")
            elif delta > 0.05:
                minor.append(f"{delta:+.0%} per {ref_price.get('per')} vs the original")
        lost = sorted(set(orig["labels"]) - set(c["labels"]))
        if lost:
            minor.append("loses label " + ", ".join(lost))
        if e["pack_note"]:
            minor.append(e["pack_note"])
        if (o_orig or {}).get("sales_unit") and o["sales_unit"] != o_orig["sales_unit"]:
            minor.append(f"sold per {o['sales_unit']} instead of {o_orig['sales_unit']}")
        for i in o["issues"]:
            if i["code"] in ("LATER_DATE", "LOW_RELIABILITY", "PRICE_EXPIRES"):
                major.append(i["message"])
            elif i["code"] in ("STOCK_UNKNOWN", "PARTIAL_STOCK"):
                minor.append(i["message"])
        allergen = catalog.allergen_status(c, avoid, mandate.allow_may_contain if mandate else False) if avoid else {"status": "safe", "codes": []}
        if allergen["status"] == "unknown":
            major.append("no allergen data: a human must confirm it is free of "
                         + ", ".join(catalog.ALLERGENS.get(a, a) for a in avoid))
        tier = "best" if not minor and not major else ("better" if not major else "good")
        options.append({
            "tier": tier,
            "product_id": c["id"],
            "name": c["name"],
            "brand": c["brand"],
            "type": {**_public_type(e["type"]), "match": e["match"]},
            "pack": e["pack"],
            "official_substitute": c["id"] in orig["official_substitutes"],
            "match_score": round(e["score"], 2),
            "price_change": round(delta, 3) if delta is not None else None,
            "compromises": major + minor,
            "allergen_check": allergen,
            "offer": o,
        })
    # a different type only when nothing recipe-compatible can be delivered
    if any(x["type"]["match"] != "different" for x in options):
        options = [x for x in options if x["type"]["match"] != "different"]
    rank = {"best": 0, "better": 1, "good": 2}
    type_rank = {"same": 0, "compatible": 1, "unknown": 2, "different": 3}
    options.sort(key=lambda x: (rank[x["tier"]], type_rank[x["type"]["match"]], len(x["compromises"]), -x["match_score"],
                                (x["offer"]["comparison_price"] or {}).get("ex_vat") or 1e9))
    # one per tier first (good / better / best), then the rest
    picked, seen = [], set()
    for x in options:
        if x["tier"] not in seen:
            picked.append(x)
            seen.add(x["tier"])
    picked += [x for x in options if x not in picked]
    return {
        "original": {"product_id": orig["id"], "name": orig["name"],
                     "why_substitute": [i["message"] for i in (o_orig or {}).get("issues", [])
                                        if i["severity"] == "blocking"] or ["(original is orderable)"],
                     "comparison_price": ref_price, "pack": opack and opack["text"]},
        "constraints": {"required_claims": sorted(required_claims), "avoid_allergens": avoid,
                        "same_category": orig["category"], "measure": measure, "temperature": orig["temperature"],
                        "type": _public_type(otype)},
        "considered": {"in_category": len(pool) + sum(excluded.values()), "excluded": excluded,
                       "priced_live": len(shortlist)},
        "options": picked[:limit],
        "no_safe_substitute": None if picked else (
            "No product in this category meets every constraint and can be delivered. "
            "Nothing will be swapped automatically; a human needs to decide."),
        "next_step": "Request a quote with substitute_for set to the original product id; "
                     "the mandate decides whether a human must approve the substitution.",
    }


def _public_type(t: dict) -> dict:
    return {k: t[k] for k in ("key", "label", "family", "fat_pct", "flavour", "plain", "species", "form", "prep",
                              "salt", "plant_based")}
