"""Validate Sous's Peppol BIS Ordering 3.0 documents with the official tooling: UBL 2.1 XSD + Peppol schematron.

    uv run --with lxml --with saxonche python -m scripts.peppol_validate fetch
    uv run --with lxml --with saxonche python -m scripts.peppol_validate check FILE.xml [FILE.xml ...]
    uv run --with lxml --with saxonche python -m scripts.peppol_validate selftest   # official examples must pass

Artifacts (downloaded once into $SOUS_DATA_DIR/peppol-validation, sha256-pinned, never from anywhere else):
  - OpenPeppol's published BIS (poacc upgrade-3) schematron bundle from docs.peppol.eu (PEPPOLBIS-T01 Order rules 3.7,
    PEPPOLBIS-T76 Order Response) and its examples bundle; the URL is unversioned, so a new Peppol release shows up
    here as a sha256 mismatch to review, never as silently changed rules
  - OASIS UBL 2.1 (the XSDs, from docs.oasis-open.org)
  - SchXslt 1.10.1 from Maven Central (compiles ISO schematron to XSLT; run with SaxonC-HE)

A document passes when the XSD has no error and the schematron has no failed assert flagged `fatal`. Warnings are
reported, not failed. lxml and saxonche are not gateway dependencies: CI and this script add them with `uv run --with`.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

from sous import config

DIR = config.DATA_DIR / "peppol-validation"
ARTIFACTS = {
    "PEPPOLBIS-Upgrade-Schematron.zip": (
        "https://docs.peppol.eu/poacc/upgrade-3/files/PEPPOLBIS-Upgrade-Schematron.zip",
        "10d8f2af8564acada635493335481d1e4a5aae42cfbf8b401569f47441bc1d48"),
    "PEPPOLBIS-Examples.zip": (
        "https://docs.peppol.eu/poacc/upgrade-3/files/PEPPOLBIS-Examples.zip",
        "50d5ece0d9b4be96cbf254f2e5558fb16380b44adcc91e360515e1b5b1093493"),
    "UBL-2.1.zip": (
        "https://docs.oasis-open.org/ubl/os-UBL-2.1/UBL-2.1.zip",
        "60b80d76394a8a2add90723ecb8e0e2e9d826775de9749df37a72d60703f86ed"),
    "schxslt-1.10.1.jar": (
        "https://repo1.maven.org/maven2/name/dmaus/schxslt/schxslt/1.10.1/schxslt-1.10.1.jar",
        "4f4f21edab7b37f96ad59ae12a344d3510f1092ac46b6d81a4efa0120b73cb58"),
}
UBL_NS = {"Order": "urn:oasis:names:specification:ubl:schema:xsd:Order-2",
          "OrderResponse": "urn:oasis:names:specification:ubl:schema:xsd:OrderResponse-2"}
SCHEMATRON = {"Order": "PEPPOLBIS-T01.sch", "OrderResponse": "PEPPOLBIS-T76.sch"}
SVRL = "{http://purl.oclc.org/dsdl/svrl}"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def fetch() -> Path:
    """Download (if missing) and unpack the pinned artifacts; refuse any whose sha256 differs."""
    DIR.mkdir(parents=True, exist_ok=True)
    for name, (url, sha) in ARTIFACTS.items():
        p = DIR / name
        if not p.exists() or _sha(p) != sha:
            with urllib.request.urlopen(url, timeout=300) as r:
                p.write_bytes(r.read())
        if _sha(p) != sha:
            p.unlink()
            raise SystemExit(f"{name}: sha256 mismatch, refusing to use it ({url})")
    for zname, prefix, dest in (("PEPPOLBIS-Upgrade-Schematron.zip", "schematron/", "schematron"),
                                ("PEPPOLBIS-Examples.zip", "rules/examples/", "examples")):
        if not (DIR / dest).exists():
            with zipfile.ZipFile(DIR / zname) as z:
                for n in z.namelist():
                    if n.startswith(prefix) and n.endswith((".sch", ".xml")):
                        out = DIR / dest / n[len(prefix):]
                        out.parent.mkdir(parents=True, exist_ok=True)
                        out.write_bytes(z.read(n))
    if not (DIR / "ubl").exists():
        with zipfile.ZipFile(DIR / "UBL-2.1.zip") as z:
            for n in z.namelist():
                if n.startswith("xsd/") and not n.endswith("/"):
                    out = DIR / "ubl" / n
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(z.read(n))
    if not (DIR / "schxslt").exists():
        with zipfile.ZipFile(DIR / "schxslt-1.10.1.jar") as z:
            for n in z.namelist():
                if n.startswith("xslt/") and not n.endswith("/"):
                    out = DIR / "schxslt" / n
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(z.read(n))
    return DIR


_compiled: dict[str, object] = {}
_schemas: dict[str, object] = {}
_proc = None


def _saxon():
    global _proc
    if _proc is None:
        from saxonche import PySaxonProcessor
        _proc = PySaxonProcessor(license=False)
    return _proc


def _schematron(kind: str):
    """The compiled schematron XSLT for 'Order' or 'OrderResponse' (compiled once per process)."""
    if kind not in _compiled:
        proc = _saxon()
        xp = proc.new_xslt30_processor()
        pipeline = xp.compile_stylesheet(stylesheet_file=str(DIR / "schxslt/xslt/2.0/pipeline-for-svrl.xsl"))
        sch = DIR / "schematron" / SCHEMATRON[kind]
        xslt = pipeline.transform_to_string(source_file=str(sch))
        out = DIR / f"compiled-{kind}.xsl"
        out.write_text(xslt, encoding="utf-8")
        _compiled[kind] = proc.new_xslt30_processor().compile_stylesheet(stylesheet_file=str(out))
    return _compiled[kind]


def _xsd(kind: str):
    if kind not in _schemas:
        from lxml import etree
        _schemas[kind] = etree.XMLSchema(etree.parse(str(DIR / f"ubl/xsd/maindoc/UBL-{kind}-2.1.xsd")))
    return _schemas[kind]


def kind_of(xml: bytes) -> str:
    from lxml import etree
    ns = etree.QName(etree.fromstring(xml)).namespace
    for k, v in UBL_NS.items():
        if v == ns:
            return k
    raise ValueError(f"not a UBL Order or OrderResponse ({ns})")


def validate(xml: bytes) -> dict:
    """{kind, xsd_errors[], fatal[], warnings[], ok}: XSD first, then the Peppol schematron."""
    from lxml import etree
    fetch()
    kind = kind_of(xml)
    doc = etree.fromstring(xml)
    schema = _xsd(kind)
    schema.validate(doc)
    xsd_errors = [f"line {e.line}: {e.message}" for e in schema.error_log]
    proc = _saxon()
    node = proc.parse_xml(xml_text=xml.decode("utf-8"))
    svrl = etree.fromstring(_schematron(kind).transform_to_string(xdm_node=node).encode("utf-8"))
    fatal, warnings = [], []
    for fa in svrl.iter(f"{SVRL}failed-assert"):
        msg = {"id": fa.get("id"), "flag": fa.get("flag"), "location": fa.get("location"),
               "text": " ".join("".join(fa.itertext()).split())}
        (warnings if fa.get("flag") == "warning" else fatal).append(msg)
    return {"kind": kind, "xsd_errors": xsd_errors, "fatal": fatal, "warnings": warnings,
            "ok": not xsd_errors and not fatal}


def selftest() -> bool:
    """The validator itself: official examples pass, and a broken copy of one fails (no silent pass-all)."""
    fetch()
    ex = DIR / "examples"
    ok = True
    for name in ("Order_Example.xml", "OrderResponse_Example.xml"):
        r = validate((ex / name).read_bytes())
        print(f"{name}: {'ok' if r['ok'] else 'FAIL'} ({len(r['warnings'])} warnings)")
        ok &= r["ok"]
    broken = (ex / "Order_Example.xml").read_bytes().replace(
        b"urn:fdc:peppol.eu:poacc:trns:order:3", b"urn:example:not-peppol", 1)
    r = validate(broken)
    print(f"broken Order (wrong CustomizationID): {'caught' if not r['ok'] else 'NOT CAUGHT'}")
    return ok and not r["ok"]


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("fetch", "check", "selftest"):
        print(__doc__)
        return 2
    if argv[0] == "fetch":
        print(fetch())
        return 0
    if argv[0] == "selftest":
        return 0 if selftest() else 1
    bad = 0
    for f in argv[1:]:
        r = validate(Path(f).read_bytes())
        print(json.dumps({"file": f, **r}, ensure_ascii=False, indent=1))
        bad += not r["ok"]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
