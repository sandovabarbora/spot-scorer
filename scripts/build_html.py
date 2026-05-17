"""Render data/spots.json into outputs/index.html via Jinja2.

The HTML template embeds the JSON inline as window.SPOT_DATA so the
page is fully self-contained. Vanilla JavaScript in the template body
reads that object and re-renders the spot detail on dropdown change.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

HERE = Path(__file__).resolve().parent.parent
DATA_PATH = HERE / "data" / "spots.json"
TEMPLATES_DIR = HERE / "templates"
OUT_PATH = HERE / "outputs" / "index.html"
# Optional: path to a one-line file containing the Modal endpoint URL.
# Gitignored so each developer / deploy can have their own.
ENDPOINT_FILE = HERE / "config" / "endpoint.txt"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _resolve_endpoint() -> str:
    """Find the Modal endpoint URL from env var or config/endpoint.txt.

    Returns empty string if neither is set. The frontend uses that to
    render a 'backend not deployed' message instead of a broken form.
    """
    env_val = (os.environ.get("MODAL_ENDPOINT") or "").strip()
    if env_val:
        return env_val
    if ENDPOINT_FILE.exists():
        return ENDPOINT_FILE.read_text(encoding="utf-8").strip()
    return ""


def main() -> None:
    data_text = DATA_PATH.read_text(encoding="utf-8")
    payload = json.loads(data_text)
    endpoint = _resolve_endpoint()
    if endpoint:
        logger.info("Embedding Modal endpoint: %s", endpoint)
    else:
        logger.info("No Modal endpoint configured (frontend will hide the URL form)")

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    css = env.get_template("style.css.j2").render()
    html = env.get_template("index.html.j2").render(
        inline_css=css,
        inline_data=data_text,
        n_spots=payload["n_spots"],
        n_brands=payload["n_brands"],
        sectors=payload["sectors"],
        modal_endpoint=endpoint,
    )
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8")
    logger.info("Wrote %s (%.1f KB)", OUT_PATH, OUT_PATH.stat().st_size / 1024)


if __name__ == "__main__":
    main()
