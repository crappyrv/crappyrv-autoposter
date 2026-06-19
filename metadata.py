"""
metadata.py — the ONLY LLM call in the pipeline.

Given a video's context (mainly its filename, plus optional human notes), calls
the Anthropic API ONCE to produce posting metadata as STRICT JSON, validates it
against a schema + the configured limits, and returns a typed VideoMetadata.

Failure policy (per spec):
  * parse or validation failure  -> retry EXACTLY ONCE with a stricter prompt
    that names the specific problems
  * still bad                    -> raise MetadataError (fail loud). We NEVER
    return unvalidated metadata.

Honest limitation: with only a filename to go on (e.g. "Video Jan 21 2024.mov"),
the model is guessing. The `notes` field is the lever for good output — pass a
one-line description of what the clip actually shows. The two-stage approval gate
(main.py writes a pending file you review/edit) is the backstop.

Manual test entry point:
    python metadata.py --filename "Slide-out motor failure walkthrough.mov"
    python metadata.py --filename "clip.mov" --notes "awning ripped off on day one"
    python metadata.py --filename "clip.mov" --show-prompt   # print prompt, no API call
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

import anthropic
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config import Settings, load_config
from notify import setup_logging

logger = logging.getLogger(__name__)

# YouTube platform hard limits (fixed by YouTube, not configurable).
YT_TITLE_MAX = 100
YT_DESCRIPTION_MAX = 5000
YT_TAGS_TOTAL_CHARS_MAX = 480  # YouTube caps total tag chars near 500; stay under


class MetadataError(RuntimeError):
    """Raised when validated metadata could not be produced (fail loud)."""


@dataclass
class VideoContext:
    """What we know about a piece of media before posting."""

    filename: str
    size_bytes: Optional[int] = None
    notes: Optional[str] = None  # optional human hint — the lever for quality
    media_type: str = "video"    # "video" | "photo"


class VideoMetadata(BaseModel):
    """Validated metadata contract returned to the pipeline."""

    # Reject unexpected keys so a malformed response (e.g. "youtube_title")
    # fails validation and triggers the retry rather than slipping through.
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    tags: List[str] = Field(default_factory=list)
    # A short, snarky-but-legal jab at Alliance RV that goes on EVERY post (David's
    # standing ask). It MUST name "Alliance" and stay opinion/satire — never a
    # false factual claim. Guaranteed present by validation; woven into the
    # description deterministically after validation.
    alliance_jab: str = Field(min_length=1)
    # Short bold hook for the branded YouTube thumbnail (2-4 words). Optional in
    # the schema (older records lack it); a fallback derived from the title is
    # applied deterministically after validation so it is never blank at render.
    thumbnail_hook: str = ""
    thumbnail_sub: str = ""


# --- Prompt construction -----------------------------------------------------
def build_system_prompt(cfg: Settings) -> str:
    b = cfg.brand
    rules = "\n".join(f"  - {r}" for r in b.rules) if b.rules else "  - (none)"
    return (
        f"You write YouTube posting metadata for the brand \"{b.name}\".\n\n"
        f"Brand: {b.description.strip()}\n\n"
        f"Voice: {b.voice.strip()}\n\n"
        f"Hard rules you MUST obey:\n{rules}\n\n"
        "GOAL: maximize views, watch time, subscribers, and comments on YouTube "
        "(including Shorts). Write titles that earn the click with curiosity, "
        "stakes, or a relatable pain point — never clickbait you can't back up. "
        "Descriptions should hook in the first line, then invite the viewer to "
        "comment and subscribe.\n\n"
        "ALLIANCE DIRECTIVE: every post must take a snarky, funny, smart shot at "
        "Alliance RV (the 'alliance_jab' field). Keep it OPINION and SATIRE about "
        "Alliance's documented quality reputation — sharp and quotable, but NEVER "
        "a false statement of fact, a fabricated incident, or anything "
        "defamatory. Riff on the brand's reputation, not invented specifics.\n\n"
        "You generate metadata for ONE video at a time. You will be given what is "
        "known about the video (often just a filename, sometimes a human note). "
        "If information is thin, write plausible, on-brand metadata that a human "
        "will review before publishing — do not invent specific false facts "
        "(exact dates, model numbers, dollar amounts) that you were not given.\n\n"
        "Respond with a SINGLE JSON object and nothing else — no prose, no "
        "markdown fences."
    )


def build_user_prompt(cfg: Settings, ctx: VideoContext) -> str:
    m = cfg.metadata
    known = [f"- media type: {ctx.media_type}", f"- filename: {ctx.filename}"]
    if ctx.size_bytes is not None:
        known.append(f"- size_bytes: {ctx.size_bytes}")
    if ctx.notes:
        known.append(f"- human note: {ctx.notes}")
    known_block = "\n".join(known)

    hashtag_rule = (
        "- Do NOT put hashtags in the description yourself — the brand hashtags "
        f"({' '.join(m.required_hashtags)}) plus the subscribe/website links are "
        "appended automatically AFTER your text, so keep the description clean.\n"
        if m.required_hashtags
        else ""
    )

    return (
        "Known information about the media:\n"
        f"{known_block}\n\n"
        "Produce a JSON object with EXACTLY these keys:\n"
        '  "title"         : YouTube title, 1 compelling line, '
        f"<= {m.title_max_length} characters. Lead with the hook (curiosity, "
        "stakes, or a relatable RV pain point); front-load the keywords people "
        "actually search.\n"
        '  "description"   : YouTube description, '
        f"<= {m.description_max_length} characters. First line is a strong hook "
        "(it shows above the fold). Then 2-4 sentences of context, then a "
        "question that invites a comment (e.g. 'Has this happened to your rig?'). "
        "Do NOT add a subscribe link or website link yourself — those are "
        "appended automatically.\n"
        '  "tags"          : array of '
        f"up to {m.tags_max_count} short lower-case keyword strings (no '#'); "
        "mix broad ('rv', 'rv life', 'fifth wheel') with specific, searchable "
        "terms relevant to the clip.\n"
        '  "alliance_jab"  : one short, snarky, FUNNY, SMART line about Alliance '
        "RV that names \"Alliance\". Opinion/satire about their quality "
        "reputation only — NEVER a false statement of fact or a made-up "
        "incident. 1 sentence, quotable.\n"
        '  "thumbnail_hook": a punchy ALL-CAPS hook for the video thumbnail, '
        "2-4 words, <= 18 characters, that would make someone stop scrolling "
        "(no hashtags, no period).\n"
        '  "thumbnail_sub" : an optional shorter supporting line for the '
        "thumbnail (<= 30 chars), or an empty string.\n\n"
        "Constraints:\n"
        f"{hashtag_rule}"
        "- The thumbnail hook and the Alliance jab must be true to the brand — do "
        "NOT invent a specific claim you were not given; when unsure, use the "
        "brand stance, not a fabricated fact.\n"
        "- Output ONLY the JSON object. No markdown, no commentary.\n"
    )


def _stricter_suffix(errors: List[str]) -> str:
    bullets = "\n".join(f"  - {e}" for e in errors)
    return (
        "\n\nYour previous response was REJECTED for these reasons:\n"
        f"{bullets}\n\n"
        "Return a corrected SINGLE JSON object with exactly the keys "
        '"title", "description", "tags", "alliance_jab", "thumbnail_hook", '
        '"thumbnail_sub" and nothing else. No markdown fences, no commentary.'
    )


# --- Anthropic call ----------------------------------------------------------
def _call_anthropic(cfg: Settings, system: str, user: str) -> str:
    """One Anthropic call. Returns the raw text; JSON is extracted downstream.

    Note: this model does not support assistant-message prefill, so we rely on a
    strong JSON-only instruction plus robust extraction in _extract_json().
    """
    client = anthropic.Anthropic(
        api_key=cfg.secrets.anthropic_api_key.get_secret_value()
    )
    try:
        resp = client.messages.create(
            model=cfg.anthropic.model,
            max_tokens=cfg.anthropic.max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system,
                    # Cache the brand context across videos in a multi-video run.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError as exc:
        raise MetadataError(f"Anthropic API call failed: {exc}") from exc

    return "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    )


# --- Parse + validate --------------------------------------------------------
def _extract_json(raw: str) -> str:
    """Normalize a model response down to its JSON object.

    Handles ```json fences and any stray prose around the object by slicing to
    the outermost { ... }. This is normalization, not error-hiding — the result
    still has to parse and pass schema validation.
    """
    s = raw.strip()
    if s.startswith("```"):
        s = s[3:]
        if s[:4].lower() == "json":
            s = s[4:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end != -1 and end > start:
        return s[start : end + 1]
    return s


def _validate_against_config(meta: VideoMetadata, cfg: Settings) -> List[str]:
    """Config-driven + platform checks. Returns a list of human-readable errors."""
    m = cfg.metadata
    errors: List[str] = []

    title_cap = min(m.title_max_length, YT_TITLE_MAX)
    if len(meta.title) > title_cap:
        errors.append(f"title is {len(meta.title)} chars; max is {title_cap}.")

    desc_cap = min(m.description_max_length, YT_DESCRIPTION_MAX)
    if len(meta.description) > desc_cap:
        errors.append(
            f"description is {len(meta.description)} chars; max is {desc_cap}."
        )

    if len(meta.tags) > m.tags_max_count:
        errors.append(f"{len(meta.tags)} tags; max is {m.tags_max_count}.")
    for t in meta.tags:
        if not t.strip():
            errors.append("tags must not contain empty strings.")
            break
        if "#" in t:
            errors.append(f"tag {t!r} must not contain '#'.")
            break
    total_tag_chars = sum(len(t) for t in meta.tags)
    if total_tag_chars > YT_TAGS_TOTAL_CHARS_MAX:
        errors.append(
            f"tags total {total_tag_chars} chars; keep under {YT_TAGS_TOTAL_CHARS_MAX}."
        )

    # The Alliance jab must actually name Alliance (David's "every single time"
    # rule). Funniness can't be validated, but presence + the brand can.
    if "alliance" not in meta.alliance_jab.lower():
        errors.append("alliance_jab must mention \"Alliance\" by name.")

    # Required hashtags are not checked here — they are GUARANTEED deterministically
    # by _apply_required_hashtags() after validation, so a missing one never fails
    # a post in full-auto mode.

    return errors


def _apply_required_hashtags(meta: VideoMetadata, cfg: Settings) -> VideoMetadata:
    """
    Deterministically assemble the final YouTube description and guarantee the
    required content (not LLM output, so safe to apply after validation):

      1. start with the AI description
      2. append the snarky Alliance jab (if it isn't already in the text)
      3. append the configured subscribe/website footer (cfg.youtube.description_footer)
      4. append any required hashtags that are missing (clickable in descriptions)
      5. YouTube keyword tags: required hashtags' keyword forms first (so the cap
         can't drop them), then the model's tags, de-duplicated and capped
      6. never render a blank thumbnail hook (derive one from the title)
    """
    req = cfg.metadata.required_hashtags or []

    desc = meta.description.rstrip()

    # 2. Alliance jab — guaranteed on every post. Skip if the model already wove
    #    the exact line into the description.
    jab = (meta.alliance_jab or "").strip()
    if jab and jab.lower() not in desc.lower():
        desc = (desc + "\n\n" + jab).rstrip()

    # 3. Subscribe + website footer.
    footer = (cfg.youtube.description_footer or "").strip()
    if footer and footer not in desc:
        desc = (desc + "\n\n" + footer).rstrip()

    # 4. Required hashtags.
    if req:
        miss_desc = [h for h in req if h.lower() not in desc.lower()]
        if miss_desc:
            desc = (desc + "\n\n" + " ".join(miss_desc)).strip()

    # 5. YouTube keyword tags: required first (so the cap can't drop them), then
    #    the model's tags, de-duplicated, capped.
    keywords = [h.lstrip("#").lower() for h in req if h.lstrip("#")]
    merged: List[str] = []
    for t in keywords + list(meta.tags):
        if t and t.lower() not in (x.lower() for x in merged):
            merged.append(t)
    merged = merged[: cfg.metadata.tags_max_count]

    # 6. Thumbnail hook fallback: never render a blank hook. Derive a short
    #    ALL-CAPS hook from the title (first few words, stop at punctuation).
    hook = (meta.thumbnail_hook or "").strip()
    if not hook:
        base = re.split(r"[:\-–—|]", meta.title)[0].strip()
        hook = " ".join(base.split()[:3]).upper()
    sub = (meta.thumbnail_sub or "").strip()

    return meta.model_copy(
        update={
            "description": desc,
            "tags": merged,
            "thumbnail_hook": hook,
            "thumbnail_sub": sub,
        }
    )


def _parse_and_validate(
    raw: str, cfg: Settings
) -> Tuple[Optional[VideoMetadata], List[str]]:
    """Return (metadata, errors). metadata is None if it could not be built."""
    try:
        data = json.loads(_extract_json(raw))
    except json.JSONDecodeError as exc:
        return None, [f"response was not valid JSON: {exc}"]
    if not isinstance(data, dict):
        return None, ["response JSON was not an object."]
    try:
        meta = VideoMetadata(**data)
    except ValidationError as exc:
        msgs = [f"{'/'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        return None, msgs
    return meta, _validate_against_config(meta, cfg)


# --- Public API --------------------------------------------------------------
def validate_metadata(cfg: Settings, data: dict) -> VideoMetadata:
    """
    Validate a metadata dict (e.g. a human-edited pending record) against the
    schema + configured limits. Raises MetadataError if invalid.

    publish.py calls this so we NEVER publish unvalidated metadata, even after a
    human edits the pending file.
    """
    try:
        meta = VideoMetadata(**data)
    except ValidationError as exc:
        msgs = [f"{'/'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise MetadataError("metadata failed schema validation: " + "; ".join(msgs)) from exc
    errors = _validate_against_config(meta, cfg)
    if errors:
        raise MetadataError("metadata failed validation: " + "; ".join(errors))
    return _apply_required_hashtags(meta, cfg)


def generate_metadata(cfg: Settings, ctx: VideoContext) -> VideoMetadata:
    """
    Generate validated posting metadata for one video.

    One call; on parse/validation failure, exactly one stricter retry; then
    raise MetadataError. Never returns unvalidated metadata.
    """
    system = build_system_prompt(cfg)
    user = build_user_prompt(cfg, ctx)

    logger.info("Generating metadata for %s", ctx.filename)
    raw = _call_anthropic(cfg, system, user)
    meta, errors = _parse_and_validate(raw, cfg)
    if meta is not None and not errors:
        logger.info("Metadata generated and validated on first attempt.")
        return _apply_required_hashtags(meta, cfg)

    logger.warning("Metadata attempt 1 rejected: %s", "; ".join(errors))
    raw2 = _call_anthropic(cfg, system, user + _stricter_suffix(errors))
    meta2, errors2 = _parse_and_validate(raw2, cfg)
    if meta2 is not None and not errors2:
        logger.info("Metadata generated and validated on retry.")
        return _apply_required_hashtags(meta2, cfg)

    raise MetadataError(
        "Could not produce valid metadata after one retry. "
        f"Last errors: {'; '.join(errors2) or 'unknown'}"
    )


# --- CLI / manual test entry point -------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate posting metadata (manual test).")
    parser.add_argument("--filename", required=True, help="video filename to base metadata on")
    parser.add_argument("--notes", help="optional human note about what the clip shows")
    parser.add_argument("--size", type=int, help="optional file size in bytes")
    parser.add_argument(
        "--show-prompt", action="store_true",
        help="print the prompt that WOULD be sent and exit (no API call)",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
        setup_logging(cfg)
        ctx = VideoContext(filename=args.filename, size_bytes=args.size, notes=args.notes)

        if args.show_prompt:
            print("===== SYSTEM =====\n" + build_system_prompt(cfg))
            print("\n===== USER =====\n" + build_user_prompt(cfg, ctx))
            return 0

        meta = generate_metadata(cfg, ctx)
        print("\n===== VALIDATED METADATA =====")
        print(json.dumps(meta.model_dump(), indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:  # fail loud, non-zero exit
        logger.error("metadata generation failed: %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
