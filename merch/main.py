"""
main.py — one pass of the merch auto-poster.

For each new PNG sitting in a drop folder (Shirts / Mugs / Garden Flags):
  download -> prep print file -> upload to Printify -> generate Etsy copy ->
  create Printify product -> publish to Etsy (draft) ->
  move source to _done -> push a phone notification with the review link.

On any per-file error the source moves to _failed and a failure push is sent; the
run continues with the next file. Runs headless on GitHub Actions cron.

Usage:
  python merch/main.py            # real run
  python merch/main.py --dry-run  # everything except Printify create/publish + move
"""
from __future__ import annotations
import sys
import time
import traceback
from pathlib import Path

# allow `python merch/main.py` from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfgmod
import dropbox_client as dbxc
import image_prep
import metadata as meta
import notify
from printify_client import PrintifyClient, build_product, PrintifyError

MAX_PER_RUN = 6
ETSY_DRAFTS_URL = "https://www.etsy.com/your/shops/me/tools/listings/drafts"
TMP = Path(__file__).resolve().parent / "tmp"


def _stamp(name: str) -> str:
    return f"{int(time.time())}-{name}"


def _has_variants(cfg: dict) -> bool:
    return bool(cfg.get("variant_ids")
                or cfg.get("variant_ids_for_light_art")
                or cfg.get("variant_ids_for_dark_art"))


def _pick_variants(cfg: dict, prep) -> tuple[list[int], str]:
    """Choose variant ids. If the blank defines garment-color-conditional lists,
    pick by the design's own brightness; else use the flat list."""
    if cfg.get("variant_ids_for_light_art") or cfg.get("variant_ids_for_dark_art"):
        if prep.art_is_light:
            return cfg.get("variant_ids_for_light_art", []), "light art → dark garment"
        return cfg.get("variant_ids_for_dark_art", []), "dark art → light garment"
    return cfg.get("variant_ids", []), ""


def process_one(dbx, pf, settings, item, dry_run: bool) -> tuple[bool, str]:
    cfg = settings.product_types[item.product_type]

    # Guard: blanks not configured yet (ids come from discover_blanks.py).
    if not _has_variants(cfg) or not cfg.get("blueprint_id") or not cfg.get("print_provider_id"):
        return False, (f"'{item.product_type}' blank not configured yet "
                       f"(run discover_blanks.py to fill ids). File left in place.")

    # 1. download
    local_src = TMP / _stamp(item.name)
    dbxc.download(dbx, item.path, local_src)

    # 2. prep print file (trim + bg-strip + upscale to fit the print area)
    placeholder = tuple(cfg["placeholder_px"])
    out_png = TMP / (Path(item.name).stem + "-printfile.png")
    prep = image_prep.prepare(
        local_src, out_png,
        target_px=placeholder,
        remove_background=bool(cfg["remove_background"]),
    )
    # Printify positions art centered; scale = fraction of the print-area width.
    scale = min(1.0, prep.out_px[0] / placeholder[0])

    # 3. upload print file -> temporary URL, then hand it to Printify
    pf_url = dbxc.upload_printfile(
        dbx, out_png, settings.printfiles_folder,
        _stamp(Path(item.name).stem + ".png"),
    )

    # 4. listing copy
    phrase = meta.phrase_from_filename(item.name)
    listing = meta.generate(settings.secrets.anthropic_api_key, phrase, cfg["blank_label"])

    variant_ids, color_note = _pick_variants(cfg, prep)

    warn = ""
    if prep.low_res:
        warn += " [low-res source]"
    if cfg["remove_background"] and not prep.background_confident:
        warn += " [check background]"

    if dry_run:
        cn = f", {color_note}" if color_note else ""
        return True, (f"[dry-run] would create '{listing.title}' "
                      f"({len(variant_ids)} variants{cn}, scale={scale:.2f}){warn}")

    # 5. upload image -> create Printify product -> publish to Etsy draft
    image_id = pf.upload_image(f"{Path(item.name).stem}.png", pf_url)
    product = build_product(
        title=listing.title, description=listing.description, tags=listing.tags,
        blueprint_id=cfg["blueprint_id"], print_provider_id=cfg["print_provider_id"],
        variant_ids=variant_ids, price_dollars=cfg["retail_price"],
        position=cfg["print_position"], image_id=image_id, scale=scale,
    )
    created = pf.create_product(product)
    pf.publish_product(created["id"])

    tail = f" ({color_note})" if color_note else ""
    return True, f"created Etsy draft: '{listing.title}'{tail}{warn}"


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    settings = cfgmod.load_settings()

    # Fail loud if core secrets missing.
    missing = [k for k, v in {
        "DROPBOX_REFRESH_TOKEN": settings.secrets.dropbox_refresh_token,
        "PRINTIFY_API_TOKEN": settings.secrets.printify_api_token,
        "PRINTIFY_SHOP_ID": settings.secrets.printify_shop_id,
        "ANTHROPIC_API_KEY": settings.secrets.anthropic_api_key,
    }.items() if not v]
    if missing:
        print("FATAL: missing secrets:", ", ".join(missing))
        return 2

    dbx = dbxc.get_client(settings.secrets)
    pf = PrintifyClient(settings.secrets.printify_api_token, settings.secrets.printify_shop_id)

    items = dbxc.list_incoming(dbx, settings)
    if not items:
        print("nothing new in the drop folders.")
        return 0

    items = items[:MAX_PER_RUN]
    print(f"found {len(items)} new file(s) to process.")
    ok_count = fail_count = 0

    for item in items:
        try:
            ok, msg = process_one(dbx, pf, settings, item, dry_run)
        except Exception as e:  # noqa: BLE001 — never let one file kill the run
            ok, msg = False, f"{type(e).__name__}: {e}"
            traceback.print_exc()

        if dry_run:
            print(f"  · {item.product_type}/{item.name}: {msg}")
            continue

        if ok:
            ok_count += 1
            dbxc.move_out(dbx, item.path, settings.done_folder, item.product_type, item.name)
            notify.push(
                settings.secrets.ntfy_topic,
                title="New Etsy draft ready",
                message=f"{msg}\nTap to review + publish ($0.20 Etsy fee).",
                click_url=ETSY_DRAFTS_URL, tags="tshirt", priority="4",
            )
            print(f"  ✓ {item.product_type}/{item.name}: {msg}")
        else:
            # "not configured yet" is a soft skip — leave the file in place.
            if "not configured yet" in msg:
                print(f"  · SKIP {item.product_type}/{item.name}: {msg}")
                continue
            fail_count += 1
            dbxc.move_out(dbx, item.path, settings.failed_folder, item.product_type, item.name)
            notify.push(
                settings.secrets.ntfy_topic,
                title="Merch upload failed",
                message=f"{item.name}: {msg}\nMoved to _failed. Re-drop after fixing.",
                tags="warning", priority="4",
            )
            print(f"  ✗ {item.product_type}/{item.name}: {msg}")

    print(f"done. created={ok_count} failed={fail_count}")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
