"""
discover_blanks — helper to fill blanks.yaml + PRINTIFY_SHOP_ID (Printify).

Requires PRINTIFY_API_TOKEN in .env.

  python merch/discover_blanks.py stores
      List Printify shops -> pick the ETSY one's id for PRINTIFY_SHOP_ID.

  python merch/discover_blanks.py search "garden flag"
      Find a blueprint_id by keyword (tee / mug / garden flag / hoodie / ...).

  python merch/discover_blanks.py providers 12
      List print providers for a blueprint (pick one for quality/price).

  python merch/discover_blanks.py variants 12 29 --colors Black White --sizes S M L XL 2XL 3XL
      Print variant ids for a blueprint+provider, filtered to colors/sizes.
"""
from __future__ import annotations
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as cfgmod

BASE = "https://api.printify.com/v1"


def _headers():
    s = cfgmod.load_settings()
    if not s.secrets.printify_api_token:
        print("Missing PRINTIFY_API_TOKEN in .env"); raise SystemExit(2)
    return {"Authorization": f"Bearer {s.secrets.printify_api_token}",
            "User-Agent": "CrappyRV-Merch-Autoposter"}


def _get(path):
    return requests.get(BASE + path, headers=_headers(), timeout=60).json()


def cmd_stores():
    for st in _get("/shops.json"):
        print(f"  id={st['id']:<12} {st['sales_channel']:<10} {st['title']}")
    print("\n-> Put the ETSY shop's id into PRINTIFY_SHOP_ID (.env + GitHub secret).")


def cmd_search(kw):
    kw = kw.lower()
    bps = _get("/catalog/blueprints.json")
    hits = [b for b in bps if kw in b["title"].lower() or kw in str(b.get("brand", "")).lower()]
    for b in hits[:25]:
        print(f"  blueprint_id={b['id']:<5} {b['title']}  [{b.get('brand','')}]")
    if not hits:
        print(f"no blueprints match '{kw}'")


def cmd_providers(bid):
    for p in _get(f"/catalog/blueprints/{bid}/print_providers.json"):
        print(f"  print_provider_id={p['id']:<5} {p['title']}")


def cmd_variants(bid, pid, colors, sizes):
    data = _get(f"/catalog/blueprints/{bid}/print_providers/{pid}/variants.json")
    vs = data.get("variants", [])
    cset = set(colors) if colors else None
    zset = set(sizes) if sizes else None
    picked = []
    for v in vs:
        o = v.get("options", {})
        c, z = str(o.get("color", "")), str(o.get("size", ""))
        if cset and c not in cset:
            continue
        if zset and z not in zset:
            continue
        picked.append(v)
        print(f"  {v['id']:<8} color={c:<16} size={z}")
    # placeholders (print positions) from the first variant
    if vs:
        print("\n  placements:", [ph.get("position") for ph in vs[0].get("placeholders", [])])
    print("\n  variant_ids:", [v["id"] for v in picked])


def main():
    if len(sys.argv) < 2:
        print(__doc__); return 1
    cmd = sys.argv[1]
    if cmd == "stores":
        cmd_stores()
    elif cmd == "search":
        cmd_search(" ".join(sys.argv[2:]))
    elif cmd == "providers":
        cmd_providers(int(sys.argv[2]))
    elif cmd == "variants":
        colors, sizes, bucket = [], [], None
        for a in sys.argv[4:]:
            if a == "--colors": bucket = colors; continue
            if a == "--sizes": bucket = sizes; continue
            if bucket is not None: bucket.append(a)
        cmd_variants(int(sys.argv[2]), int(sys.argv[3]), colors, sizes)
    else:
        print(__doc__); return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
