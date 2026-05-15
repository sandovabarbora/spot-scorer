"""Render data/spots.json into outputs/index.html via Jinja2.

The HTML template embeds the JSON inline as window.SPOT_DATA so the
page is fully self-contained. Vanilla JavaScript in the template body
reads that object and re-renders the spot detail on dropdown change.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

HERE = Path(__file__).resolve().parent.parent
DATA_PATH = HERE / "data" / "spots.json"
TEMPLATES_DIR = HERE / "templates"
OUT_PATH = HERE / "outputs" / "index.html"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    data_text = DATA_PATH.read_text(encoding="utf-8")
    payload = json.loads(data_text)

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
    )
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8")
    logger.info("Wrote %s (%.1f KB)", OUT_PATH, OUT_PATH.stat().st_size / 1024)


if __name__ == "__main__":
    main()
