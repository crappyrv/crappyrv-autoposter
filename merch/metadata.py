"""
metadata — one Anthropic call turns a design's filename (David's slogan/idea)
into an Etsy-ready title, description, and tags, in CrappyRV's voice.

Etsy limits enforced here: title <= 140 chars, up to 13 tags, each tag <= 20 chars.
Brand rules enforced: no last names, no family names, no manufacturer names on the
PRODUCT (mocking a brand in content is fine; putting its mark on merch is not).
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
import yaml

_MODEL = "claude-sonnet-5"
_HERE = Path(__file__).resolve().parent


def phrase_from_filename(name: str) -> str:
    stem = Path(name).stem
    stem = re.sub(r"[-_]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem


def _brand_block() -> str:
    # Reuse the video-poster's brand voice so merch sounds like the channel.
    try:
        cfg = yaml.safe_load((_HERE.parent / "config.yaml").read_text())
        b = cfg["brand"]
        rules = "\n".join(f"- {r}" for r in b.get("rules", []))
        return f"{b['name']}: {b['description']}\nVoice: {b['voice']}\nRules:\n{rules}"
    except Exception:
        return ("CrappyRV: RV-industry watchdog brand. Wry, candid, pro-consumer, "
                "funny but credible. Never name people or manufacturers on products.")


@dataclass
class Listing:
    title: str
    description: str
    tags: list[str] = field(default_factory=list)


_PROMPT = """You are writing an Etsy listing for a print-on-demand {blank} from the \
brand below. The design printed on it says / is about: "{phrase}".

{brand}

Write listing copy that would sell to RV owners with a sense of humor (the core \
audience) and campers/road-trippers generally. It is funny merch, not a manifesto.

HARD RULES:
- Title: <= 140 characters. Lead with the phrase/joke, then the product, then a \
couple of high-intent keywords (e.g. "RV gift", "camping"). No ALL CAPS shouting.
- Exactly 13 tags. Each tag <= 20 characters, lowercase, buyer search terms \
(mix the joke, the product, gift occasions, and the niche). No '#'.
- Description: 2-4 short paragraphs. Funny, then practical (what it is, who it's \
for, gift angle). End with one line noting it's made-to-order print-on-demand.
- NEVER put a person's name or any RV manufacturer's name (Alliance, Winnebago, \
Grand Design, etc.) in the title, tags, or description.

Return ONLY valid JSON, no prose:
{{"title": "...", "description": "...", "tags": ["...", ... 13 total]}}"""


def _coerce(data: dict) -> Listing:
    title = str(data.get("title", "")).strip()[:140]
    desc = str(data.get("description", "")).strip()
    tags = [str(t).strip().lower()[:20] for t in data.get("tags", []) if str(t).strip()]
    # de-dupe, cap at 13
    seen, clean = set(), []
    for t in tags:
        if t and t not in seen:
            seen.add(t); clean.append(t)
    return Listing(title=title, description=desc, tags=clean[:13])


def generate(anthropic_key: str, phrase: str, blank_label: str) -> Listing:
    # max_retries so a transient API blip self-corrects instead of failing the run.
    client = anthropic.Anthropic(api_key=anthropic_key, max_retries=4)
    prompt = _PROMPT.format(blank=blank_label, phrase=phrase, brand=_brand_block())

    def _call(extra: str = "") -> Listing:
        msg = client.messages.create(
            model=_MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt + extra}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        # tolerate code fences
        text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        return _coerce(json.loads(text))

    try:
        out = _call()
    except (json.JSONDecodeError, KeyError):
        out = _call("\n\nReturn ONLY the JSON object. No markdown, no commentary.")

    if not out.title:
        out.title = f"{phrase} — {blank_label}"[:140]
    return out
