# Merch Auto-Poster — drop a PNG, get an Etsy draft

Turns a ChatGPT design into a real Printify product + Etsy listing with **zero
tools to open**. You drop a PNG in a Dropbox folder; within ~30 minutes a
finished Etsy **draft** appears and your phone buzzes with a link to publish.

It reuses the video auto-poster's backbone (GitHub Actions cron + Dropbox +
Anthropic), so it costs nothing to run.

> **Why Printify, not Printful?** Printful's API cannot create Etsy listings —
> only the web UI can. Printify's API creates the product AND publishes it to
> Etsy as a draft, which is what makes "drop a file, get a listing" possible.

---

## Your daily workflow (this is the whole thing)

1. In ChatGPT, make your design. **Ask for a transparent-background PNG at the
   biggest size it'll give you** ("transparent background, highest resolution").
2. Name the file the slogan — e.g. `good luck out there.png`,
   `whatcha broke this week.png`. The name becomes the listing's headline.
3. Drop it in the matching Dropbox folder:
   - `CrappyRV Merch Drop/Shirts`
   - `CrappyRV Merch Drop/Mugs`
   - `CrappyRV Merch Drop/Garden Flags`
4. Within ~30 min your phone pings: **"New Etsy draft ready."** Tap it → you land
   on your Etsy Drafts. Glance at the mockup, tap **Publish** (Etsy's $0.20 fee).

That's it. No Printify, no Photoshop, no resizing, no writing descriptions.

The system automatically: strips the background, upscales the art to print size,
sharpens it, **auto-picks the shirt color from the design** (light-ink art → black
tees, dark-ink art → white tees), writes an on-brand title + description + 13 Etsy
tags, builds the Printify product across sizes, and pushes it to Etsy as a draft.

**Why draft, not live?** Printify always creates Etsy listings as drafts (Etsy
charges to publish). That's the safety net: nothing goes public until you glance
at it. On a "truth about quality" brand, that 10-second look matters.

---

## Print-quality reality (read once)

ChatGPT exports ~1024–1536px. A full shirt front wants ~3300px+. The system
upscales, and **bold text / simple designs come out clean**. Detailed or
photographic art can look soft — you'll get a **"[low-res]"** note in the ping.
That's your cue to order a sample before promoting it. Bigger source in = crisper
shirt out; the software can't invent detail that isn't there.

---

## Setup status (what needs you vs. what's automated)

| Step | Who | Status |
|------|-----|--------|
| Dropbox drop folders | automated | ✅ created |
| Pipeline code + Printify blanks (tee/mug/flag) | automated | ✅ done |
| Printify API token + shop id | automated | ✅ done (in secrets) |
| **Reconnect Printify ↔ Etsy** (expired) | **you (log into Etsy, Allow)** | ⏳ |
| GitHub secrets + push code | automated | ⏳ |
| **Install ntfy app + subscribe** | **you (phone)** | ⏳ topic: `crv-merch-54ad311f4f` |

### Reconnect Printify to Etsy (one time)
Printify → store switcher (top-left) → **My Etsy Store → Renew token** (or
Add/Manage stores → reconnect Etsy) → log into Etsy → **Allow Access**. Until this
is done, products are created in Printify but can't reach Etsy.

### Get the phone pings
Install **ntfy** (App Store / Play Store, free, no account). Tap **+**, subscribe
to **`crv-merch-54ad311f4f`**. That's how "draft ready" / "upload failed" alerts
reach you.

---

## Blanks in use (Printify, Etsy store 11022470)

| Type | Blueprint | Provider | Variants | Price |
|------|-----------|----------|----------|-------|
| Shirts | 12 Bella+Canvas 3001 | 29 Monster Digital | Black + White, S–3XL | $26.99 |
| Mugs | 68 Mug 11oz | 1 SPOKE | 11oz | $16.99 |
| Garden Flags | 917 Garden Banner | 14 ArtsAdd | 12"×18" | $24.99 |

## Adding a new product type later (hats, stickers, totes)

1. `python merch/discover_blanks.py search "sticker"` → blueprint id.
2. `python merch/discover_blanks.py providers <blueprint_id>` → pick a provider.
3. `python merch/discover_blanks.py variants <blueprint_id> <provider_id>` → ids.
4. Add a block to `merch/blanks.yaml`, create the matching Dropbox folder. Done.

## Running / testing by hand

```bash
python merch/config.py                 # check secrets load
python merch/discover_blanks.py stores # list Printify shops
python merch/main.py --dry-run         # process folders WITHOUT creating products
python merch/main.py                    # real run (also the GitHub Actions command)
```

GitHub Actions runs `python merch/main.py` every 30 min from ~10am–10pm ET, plus
a manual **Run workflow** button for instant posting.
