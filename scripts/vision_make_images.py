"""Render the vision eval set: kitchen paper (order lists, menus, delivery notes) as photos/scans, with truth.

    uv run python scripts/vision_make_images.py      # writes evals/vision/images/*.{jpg,png} + evals/vision/truth.json

Handwriting uses open (OFL) Google Fonts, downloaded once to ~/.cache/sous-fonts (Caveat, Indie Flower, Kalam,
Patrick Hand); printed pages use DejaVu Sans. Each page gets paper colour, ruled lines, per-line jitter,
slight rotation, noise, blur and JPEG compression; one photo gets uneven light and a perspective skew.
Deliberate traps: a crossed-out line (must be skipped), a smudged line (must be flagged, not guessed), a
Finnish decimal comma, Finnish pack abbreviations (ltk, plo, pkt), Swedish and English pages, a delivery
note with a handwritten correction. Deterministic (fixed seeds), so the images only change with this file.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "evals" / "vision"
FONT_DIR = Path.home() / ".cache" / "sous-fonts"
FONTS = {
    "caveat": "caveat/Caveat%5Bwght%5D.ttf",
    "indie": "indieflower/IndieFlower-Regular.ttf",
    "kalam": "kalam/Kalam-Regular.ttf",
    "patrick": "patrickhand/PatrickHand-Regular.ttf",
}
PRINT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
PRINT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
INK = [(20, 30, 90), (25, 25, 30), (10, 40, 110)]


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    if name in ("print", "bold"):
        return ImageFont.truetype(PRINT if name == "print" else PRINT_BOLD, size)
    path = FONT_DIR / Path(FONTS[name]).name.replace("%5B", "[").replace("%5D", "]")
    if not path.exists():
        FONT_DIR.mkdir(parents=True, exist_ok=True)
        r = httpx.get("https://raw.githubusercontent.com/google/fonts/main/ofl/" + FONTS[name],
                      follow_redirects=True, timeout=30)
        r.raise_for_status()
        path.write_bytes(r.content)
    return ImageFont.truetype(str(path), size)


def paper(w: int, h: int, rng: random.Random, ruled: bool, color=(250, 248, 238)) -> Image.Image:
    img = Image.new("RGB", (w, h), color)
    d = ImageDraw.Draw(img)
    if ruled:
        for y in range(160, h - 40, 64):
            d.line([(40, y), (w - 40, y)], fill=(170, 195, 225), width=2)
        d.line([(110, 0), (110, h)], fill=(225, 150, 150), width=2)
    return img


def hand(img: Image.Image, rng: random.Random, x: int, y: int, text: str, f: ImageFont.FreeTypeFont,
         ink=INK[0], strike=False, smudge=False) -> None:
    """One handwritten line: words with a little jitter in position and baseline."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx = x + rng.randint(-6, 6)
    for word in text.split(" "):
        dy = rng.randint(-3, 3)
        d.text((cx, y + dy), word, font=f, fill=ink + (235,))
        cx += d.textlength(word + " ", font=f) + rng.randint(-2, 5)
    if strike:
        d.line([(x - 5, y + f.size * 0.55), (cx, y + f.size * 0.5 + rng.randint(-4, 4))], fill=ink + (255,), width=4)
    layer = layer.rotate(rng.uniform(-0.8, 0.8), center=(x, y), resample=Image.Resampling.BICUBIC)
    if smudge:
        box = (x - 10, y - 5, int(cx) + 10, y + f.size + 15)
        part = layer.crop(box).filter(ImageFilter.GaussianBlur(9))
        layer.paste(part, box)
        blob = ImageDraw.Draw(layer)
        for _ in range(14):
            bx, by = rng.randint(box[0] + 30, box[2] - 30), rng.randint(box[1], box[3])
            blob.ellipse([bx - 22, by - 12, bx + 22, by + 12], fill=(90, 80, 70, 70))
    img.alpha_composite(layer) if img.mode == "RGBA" else img.paste(layer, (0, 0), layer)


def printed(img: Image.Image, x: int, y: int, text: str, f: ImageFont.FreeTypeFont, fill=(15, 15, 15)) -> None:
    ImageDraw.Draw(img).text((x, y), text, font=f, fill=fill)


def finish(img: Image.Image, rng: random.Random, path: Path, rotate: float, noise: int = 10, blur: float = 0.6,
           light: bool = False, skew: bool = False, quality: int = 82) -> None:
    bg = (70, 66, 60)
    if skew:  # a photo taken at a slight angle
        w, h = img.size
        img = img.transform(img.size, Image.Transform.QUAD,
                            (rng.randint(0, 40), rng.randint(0, 30), rng.randint(0, 25), h - rng.randint(0, 30),
                             w - rng.randint(0, 25), h - rng.randint(0, 40), w - rng.randint(0, 45), rng.randint(0, 25)),
                            resample=Image.Resampling.BICUBIC, fillcolor=bg)
    img = img.rotate(rotate, expand=True, resample=Image.Resampling.BICUBIC, fillcolor=bg)
    if light:  # uneven light: darker towards one corner
        w, h = img.size
        grad = Image.linear_gradient("L").rotate(90).resize((w, h))
        shade = Image.new("RGB", (w, h), (40, 35, 25))
        img = Image.composite(img, shade, grad.point(lambda v: 150 + v * 105 // 255))
    if noise:
        n = Image.effect_noise(img.size, noise).convert("RGB")
        img = Image.blend(img, n, 0.06)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".png":
        img.save(path, optimize=True)
    else:
        img.save(path, "JPEG", quality=quality)


# --- the pages --------------------------------------------------------------------------------------

def order_handwritten_fi(path: Path):
    rng = random.Random(1)
    img = paper(1200, 1100, rng, ruled=True)
    f = font("caveat", 58)
    hand(img, rng, 140, 90, "Tilaus  -  toimitus ma 16.11.", font("caveat", 64), INK[1])
    lines = ["maito laktoositon 40 l", "voi 5 kg", "kerma 10 l", "kananmunat 360 kpl", "perunat 25 kg",
             "jauheliha 12 kg", "tomaattimurska 6 kg"]
    for i, t in enumerate(lines):
        hand(img, rng, 140, 168 + i * 64, t, f)
    hand(img, rng, 700, 1010, "- Ville", font("caveat", 50), INK[1])
    finish(img, rng, path, rotate=1.8, noise=12)
    return {"kind": "order_list", "delivery_date": "2026-11-16", "items": [
        {"name_fi": "maito laktoositon", "terms": ["maito", "milk"], "quantity": 40, "unit": "l"},
        {"name_fi": "voi", "terms": ["voi", "butter"], "quantity": 5, "unit": "kg"},
        {"name_fi": "kerma", "terms": ["kerma", "cream"], "quantity": 10, "unit": "l"},
        {"name_fi": "kananmuna", "terms": ["kananmun", "muna", "egg"], "quantity": 360, "unit": "kpl"},
        {"name_fi": "peruna", "terms": ["peruna", "potato"], "quantity": 25, "unit": "kg"},
        {"name_fi": "jauheliha", "terms": ["jauheliha", "mince", "minced", "ground"], "quantity": 12, "unit": "kg"},
        {"name_fi": "tomaattimurska", "terms": ["tomaatti", "tomato"], "quantity": 6, "unit": "kg"}]}


def order_abbrev_fi(path: Path):
    rng = random.Random(2)
    img = paper(1200, 1150, rng, ruled=True, color=(252, 250, 244))
    f = font("indie", 50)
    hand(img, rng, 140, 92, "Kylmiö / tilattavat", font("indie", 58), INK[2])
    rows = [("kevytmaito 6 ltk", {}), ("appelsiinimehu 12 plo", {}), ("kahvi 8 pkt", {}),
            ("jogurtti 4 kg", {"strike": True}), ("ruokakerma 2,5 l", {}), ("ruisleipä 20 kpl", {}),
            ("juusto 5 kg", {"smudge": True}), ("sipuli 10 kg", {})]
    for i, (t, kw) in enumerate(rows):
        hand(img, rng, 140, 168 + i * 64, t, f, INK[2], **kw)
    finish(img, rng, path, rotate=-2.5, noise=14, blur=0.8)
    return {"kind": "order_list", "delivery_date": None, "items": [
        {"name_fi": "kevytmaito", "terms": ["maito", "milk"], "quantity": 6, "unit": "ltk"},
        {"name_fi": "appelsiinimehu", "terms": ["appelsiin", "orange"], "quantity": 12, "unit": "plo"},
        {"name_fi": "kahvi", "terms": ["kahvi", "coffee"], "quantity": 8, "unit": "pkt"},
        {"name_fi": "ruokakerma", "terms": ["kerma", "cream"], "quantity": 2.5, "unit": "l"},
        {"name_fi": "ruisleipä", "terms": ["ruisleip", "rye"], "quantity": 20, "unit": "kpl"},
        {"name_fi": "sipuli", "terms": ["sipuli", "onion"], "quantity": 10, "unit": "kg"}],
        "must_skip": [["jogurt", "yogh", "yogurt"]], "must_flag": [["juust", "chees"]]}


def order_printed_sv(path: Path):
    rng = random.Random(3)
    img = paper(1240, 1000, rng, ruled=False, color=(255, 255, 255))
    printed(img, 90, 70, "Restaurang Sjöbris — Beställning", font("bold", 40))
    printed(img, 90, 130, "Leverans: tisdag 17.11.2026", font("print", 30))
    d = ImageDraw.Draw(img)
    d.line([(90, 190), (1150, 190)], fill=(0, 0, 0), width=2)
    printed(img, 90, 205, "Vara", font("bold", 30))
    printed(img, 800, 205, "Mängd", font("bold", 30))
    rows = [("Mjölk laktosfri", "30 l"), ("Smör", "4 kg"), ("Vispgrädde", "5 l"), ("Ägg", "180 st"),
            ("Potatis, fast", "20 kg")]
    for i, (a, b) in enumerate(rows):
        printed(img, 90, 270 + i * 70, a, font("print", 32))
        printed(img, 800, 270 + i * 70, b, font("print", 32))
    finish(img, rng, path, rotate=0.6, noise=6, blur=0.4)
    return {"kind": "order_list", "delivery_date": "2026-11-17", "items": [
        {"name_fi": "maito laktoositon", "terms": ["maito", "mjölk", "milk"], "quantity": 30, "unit": "l"},
        {"name_fi": "voi", "terms": ["voi", "smör", "butter"], "quantity": 4, "unit": "kg"},
        {"name_fi": "kuohukerma", "terms": ["kerma", "grädde", "cream"], "quantity": 5, "unit": "l"},
        {"name_fi": "kananmuna", "terms": ["muna", "ägg", "egg"], "quantity": 180, "unit": "kpl"},
        {"name_fi": "peruna", "terms": ["peruna", "potatis", "potato"], "quantity": 20, "unit": "kg"}]}


def order_handwritten_en(path: Path):
    rng = random.Random(4)
    img = paper(1150, 900, rng, ruled=True, color=(248, 246, 232))
    f = font("kalam", 46)
    hand(img, rng, 140, 92, "Order for Monday (Hotel Aurora)", font("kalam", 50), INK[1])
    for i, t in enumerate(["lactose-free milk 1000 l", "butter 2.5 kg", "eggs 90 pcs", "whipping cream 6 l"]):
        hand(img, rng, 140, 168 + i * 64, t, f, INK[1])
    finish(img, rng, path, rotate=-1.2, noise=10)
    return {"kind": "order_list", "delivery_date": "2026-11-16", "items": [
        {"name_fi": "maito laktoositon", "terms": ["maito", "milk"], "quantity": 1000, "unit": "l"},
        {"name_fi": "voi", "terms": ["voi", "butter"], "quantity": 2.5, "unit": "kg"},
        {"name_fi": "kananmuna", "terms": ["muna", "egg"], "quantity": 90, "unit": "kpl"},
        {"name_fi": "kuohukerma", "terms": ["kerma", "cream"], "quantity": 6, "unit": "l"}]}


def menu_printed_fi(path: Path):
    rng = random.Random(5)
    img = paper(1240, 1100, rng, ruled=False, color=(255, 254, 250))
    printed(img, 90, 70, "Ruokalista viikko 47", font("bold", 46))
    printed(img, 90, 140, "Henkilöstöravintola Kaarna  ·  raaka-aineet toimitettava ma 16.11.", font("print", 26))
    rows = [("Ma 16.11.", "Lohikeitto", "80 annosta"), ("Ti 17.11.", "Lasagne", "120 annosta"),
            ("Ke 18.11.", "Karjalanpaisti", "100 annosta")]
    for i, (a, b, c) in enumerate(rows):
        y = 240 + i * 110
        printed(img, 90, y, a, font("bold", 32))
        printed(img, 330, y, b, font("print", 36))
        printed(img, 870, y, c, font("print", 32))
    printed(img, 90, 640, "Kaikki ruoat laktoosittomia. Lisäkkeet: salaatti, leipä.", font("print", 26), (60, 60, 60))
    finish(img, rng, path, rotate=0.4, noise=5, blur=0.3)
    return {"kind": "menu", "delivery_date": "2026-11-16", "items": [], "dishes": [
        {"name": "Lohikeitto", "terms": ["lohikeit", "salmon"], "portions": 80},
        {"name": "Lasagne", "terms": ["lasagn"], "portions": 120},
        {"name": "Karjalanpaisti", "terms": ["karjalan", "karelian"], "portions": 100}]}


def menu_handwritten_fi(path: Path):
    rng = random.Random(6)
    img = paper(1100, 950, rng, ruled=True)
    f = font("patrick", 52)
    hand(img, rng, 140, 92, "Pikkujoulut la 21.11. - 60 hlö", font("patrick", 58), INK[0])
    for i, t in enumerate(["Kasvissosekeitto", "Joulukinkku + sinappi", "Rosolli"]):
        hand(img, rng, 170, 168 + i * 128, t, f)
    finish(img, rng, path, rotate=2.8, noise=12, blur=0.7)
    return {"kind": "menu", "delivery_date": "2026-11-21", "items": [], "dishes": [
        {"name": "Kasvissosekeitto", "terms": ["kasvissose", "vegetable"], "portions": 60},
        {"name": "Joulukinkku", "terms": ["kinkku", "ham"], "portions": 60},
        {"name": "Rosolli", "terms": ["rosol", "beet"], "portions": 60}]}


def delivery_note_printed(path: Path):
    rng = random.Random(7)
    img = paper(1240, 1100, rng, ruled=False, color=(255, 255, 255))
    printed(img, 90, 60, "LÄHETE  /  FÖLJESEDEL", font("bold", 42))
    printed(img, 90, 120, "Tukku Oy  ·  Asiakas: Hotel Aurora  ·  Toimituspvm 13.11.2026  ·  Tilaus 88213", font("print", 24))
    d = ImageDraw.Draw(img)
    cols = [90, 640, 820, 1010]
    d.line([(90, 180), (1160, 180)], fill=(0, 0, 0), width=2)
    for x, h in zip(cols, ["Tuote", "Tilattu", "Toimitettu", "Yks."]):
        printed(img, x, 195, h, font("bold", 28))
    d.line([(90, 240), (1160, 240)], fill=(0, 0, 0), width=1)
    rows = [("Maito laktoositon 1 l", "40", "24", "l"), ("Voi 500 g", "5", "5", "kg"), ("Kuohukerma 1 l", "10", "6", "l"),
            ("Kananmuna M", "360", "360", "kpl"), ("Peruna kiinteä", "25", "0", "kg")]
    for i, r in enumerate(rows):
        y = 260 + i * 62
        for x, v in zip(cols, r):
            printed(img, x, y, v, font("print", 28))
    printed(img, 90, 600, "Puuttuvat tuotteet toimitetaan erikseen sovittaessa.", font("print", 24), (70, 70, 70))
    finish(img, rng, path, rotate=-0.7, noise=6, blur=0.4)
    return {"kind": "delivery_note", "delivery_date": "2026-11-13", "items": [
        {"name_fi": "maito laktoositon", "terms": ["maito", "milk"], "quantity": 40, "unit": "l", "delivered_quantity": 24},
        {"name_fi": "voi", "terms": ["voi", "butter"], "quantity": 5, "unit": "kg", "delivered_quantity": 5},
        {"name_fi": "kuohukerma", "terms": ["kerma", "cream"], "quantity": 10, "unit": "l", "delivered_quantity": 6},
        {"name_fi": "kananmuna", "terms": ["muna", "egg"], "quantity": 360, "unit": "kpl", "delivered_quantity": 360},
        {"name_fi": "peruna", "terms": ["peruna", "potato"], "quantity": 25, "unit": "kg", "delivered_quantity": 0}]}


def delivery_note_photo(path: Path):
    rng = random.Random(8)
    img = paper(1240, 1000, rng, ruled=False, color=(246, 244, 236))
    printed(img, 90, 60, "LÄHETE", font("bold", 42))
    printed(img, 90, 120, "Tukku Oy  ·  Ravintola Kaarna  ·  16.11.2026", font("print", 24))
    d = ImageDraw.Draw(img)
    cols = [90, 640, 820, 1010]
    for x, h in zip(cols, ["Tuote", "Tilattu", "Toimitettu", "Yks."]):
        printed(img, x, 190, h, font("bold", 28))
    d.line([(90, 235), (1160, 235)], fill=(0, 0, 0), width=1)
    rows = [("Jauheliha naudan 2 kg", "12", "8", "kg"), ("Tomaattimurska", "6", "6", "kg"), ("Sipuli kuorittu", "10", "10", "kg"),
            ("Ruokakerma 1 l", "5", "5", "l")]
    for i, r in enumerate(rows):
        y = 255 + i * 62
        for x, v in zip(cols, r):
            printed(img, x, y, v, font("print", 28))
    # the cook corrects a line by hand: only 2 l of cream came
    d.line([(815, 452), (880, 440)], fill=(180, 20, 20), width=3)
    hand(img, rng, 800, 492, "puuttuu 3 l !", font("caveat", 44), (180, 20, 20))
    finish(img, rng, path, rotate=3.5, noise=16, blur=0.9, light=True, skew=True, quality=70)
    return {"kind": "delivery_note", "delivery_date": "2026-11-16", "items": [
        {"name_fi": "jauheliha", "terms": ["jauheliha", "mince", "minced"], "quantity": 12, "unit": "kg", "delivered_quantity": 8},
        {"name_fi": "tomaattimurska", "terms": ["tomaatti", "tomato"], "quantity": 6, "unit": "kg", "delivered_quantity": 6},
        {"name_fi": "sipuli", "terms": ["sipuli", "onion"], "quantity": 10, "unit": "kg", "delivered_quantity": 10},
        {"name_fi": "ruokakerma", "terms": ["kerma", "cream"], "quantity": 5, "unit": "l", "delivered_quantity": 2}]}


PAGES = [("01_order_handwritten_fi.jpg", order_handwritten_fi), ("02_order_abbrev_fi.jpg", order_abbrev_fi),
         ("03_order_printed_sv.jpg", order_printed_sv), ("04_order_handwritten_en.jpg", order_handwritten_en),
         ("05_menu_printed_fi.jpg", menu_printed_fi), ("06_menu_handwritten_fi.jpg", menu_handwritten_fi),
         ("07_delivery_note_printed_fi.jpg", delivery_note_printed), ("08_delivery_note_photo.jpg", delivery_note_photo)]
TODAY = "2026-11-13"


def main() -> int:
    truth = {"today": TODAY, "cases": []}
    for name, fn in PAGES:
        t = fn(OUT / "images" / name)
        truth["cases"].append({"file": f"images/{name}", **{"dishes": [], "must_skip": [], "must_flag": []}, **t})
        print("wrote", name)
    (OUT / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
