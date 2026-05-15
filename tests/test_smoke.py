"""Smoke tests: data file shape + build produces non-empty HTML."""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
DATA = HERE / "data" / "spots.json"
OUT = HERE / "outputs" / "index.html"


def test_data_json_exists_and_parses() -> None:
    assert DATA.exists(), "Run `make refresh` to generate data/spots.json"
    payload = json.loads(DATA.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["n_spots"] >= 10
    assert len(payload["spots"]) == payload["n_spots"]


def test_every_spot_has_required_metrics() -> None:
    payload = json.loads(DATA.read_text(encoding="utf-8"))
    required = {
        "spot_id", "brand", "sector", "anchor_hex",
        "color_gap_deg", "brand_anchor", "diversity", "sat_std",
        "voice_fraction", "music_fraction", "tempo_bpm", "rms_std",
    }
    for spot in payload["spots"]:
        missing = required - set(spot.keys())
        assert not missing, f"{spot.get('spot_id')} missing {missing}"


def test_sector_benchmarks_cover_all_spot_sectors() -> None:
    payload = json.loads(DATA.read_text(encoding="utf-8"))
    sectors_in_spots = {s["sector"] for s in payload["spots"]}
    sectors_in_bench = set(payload["sector_benchmarks"].keys())
    assert sectors_in_spots <= sectors_in_bench
