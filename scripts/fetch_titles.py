"""Fetch original YouTube titles (with diacritics) for each spot.

The corpus stores ASCII-only slugs as spot IDs. The picker label needs
the readable title with full Czech diacritics. yt-dlp metadata-only
query is cheap and only needs running when the corpus changes.

Writes data/titles.json: {spot_id: title}.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml
from yt_dlp import YoutubeDL  # type: ignore[import-untyped]
from yt_dlp.utils import DownloadError, ExtractorError  # type: ignore[import-untyped]

HERE = Path(__file__).resolve().parent.parent
COLOR_CORPUS = HERE.parent / "bubble-color-fingerprint" / "config" / "corpus.yaml"
OUT = HERE / "data" / "titles.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "extract_flat": False,
    "noplaylist": True,
    "socket_timeout": 30,
}


def fetch_title(url: str) -> str | None:
    try:
        with YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
    except (DownloadError, ExtractorError) as exc:
        logger.warning("title fetch failed for %s: %s", url, exc)
        return None
    if not info:
        return None
    return info.get("title")


def main() -> None:
    corpus = yaml.safe_load(COLOR_CORPUS.read_text(encoding="utf-8"))
    spots = corpus["brands"]
    existing: dict[str, str] = {}
    if OUT.exists():
        existing = json.loads(OUT.read_text(encoding="utf-8"))

    out: dict[str, str] = dict(existing)
    for spot in spots:
        sid = spot["id"]
        if sid in out:
            continue
        url = spot.get("search_query")
        if not url or not url.startswith(("http://", "https://")):
            logger.info("[%s] no direct URL, skip", sid)
            continue
        title = fetch_title(url)
        if title:
            out[sid] = title
            logger.info("[%s] %s", sid, title)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Wrote %d titles -> %s", len(out), OUT)


if __name__ == "__main__":
    main()
