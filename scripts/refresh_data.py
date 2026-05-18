"""Refresh data/spots.json from the sister projects' parquet outputs.

Reads the per-spot metrics from color-fingerprint-cz and sound-
fingerprint-cz, joins them by brand+title (or by re-mapping spot ids),
computes per-sector benchmarks, and writes a single JSON file that the
static HTML tool consumes.

Run this whenever either sister project's data changes. The JSON file
is committed so the spot-scorer repo is self-contained.
"""

from __future__ import annotations

import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

# Sister project locations (relative to this repo's parent dir)
HERE = Path(__file__).resolve().parent.parent
PARENT = HERE.parent
COLOR_PROJECT = PARENT / "bubble-color-fingerprint"
SOUND_PROJECT = PARENT / "sound-fingerprint-cz"

OUT_PATH = HERE / "data" / "spots.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _read_color_spots() -> pd.DataFrame:
    path = COLOR_PROJECT / "data" / "processed" / "spots.parquet"
    if not path.exists():
        logger.error("color spots parquet not found at %s", path)
        sys.exit(2)
    df = pd.read_parquet(path)
    keep = [
        "brand_id",
        "brand",
        "sector",
        "anchor_hex",
        "n_frames",
        "diversity",
        "sat_mean",
        "sat_std",
        "brand_anchor",
        "color_gap_deg",
        "arc_shape",
    ]
    return df[keep].rename(columns={"brand_id": "color_spot_id"})


def _read_sound_spots() -> pd.DataFrame:
    path = SOUND_PROJECT / "data" / "processed" / "spots_audio.parquet"
    if not path.exists():
        logger.error("sound spots parquet not found at %s", path)
        sys.exit(2)
    df = pd.read_parquet(path)
    keep = [
        "spot_id",
        "brand",
        "sector",
        "anchor_hex",
        "duration_s",
        "tempo_bpm",
        "key",
        "mode",
        "voice_fraction",
        "music_fraction",
        "rms_mean",
        "rms_std",
        "spectral_centroid_mean",
        "onset_rate",
    ]
    return df[keep].rename(columns={"spot_id": "sound_spot_id"})


def _join_by_spot_id(color: pd.DataFrame, sound: pd.DataFrame) -> pd.DataFrame:
    """color uses the same spot ids as sound (both pipelines read the same
    corpus.yaml from color-fingerprint-cz). Join on spot id directly.
    """
    color = color.rename(columns={"color_spot_id": "spot_id"})
    sound = sound.rename(columns={"sound_spot_id": "spot_id"})
    # Sound and color carry the same brand/sector/anchor — drop the dups in sound
    sound = sound.drop(columns=["brand", "sector", "anchor_hex"])
    merged = color.merge(sound, on="spot_id", how="inner")
    return merged


# --- Sector benchmarks ----------------------------------------------------


def _sector_summary(df: pd.DataFrame, metrics: list[str]) -> dict[str, dict[str, dict[str, float]]]:
    out: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
    for sector, group in df.groupby("sector"):
        for m in metrics:
            vals = group[m].dropna().to_numpy(dtype=np.float64)
            if vals.size == 0:
                continue
            out[sector][m] = {
                "n": int(vals.size),
                "median": float(np.median(vals)),
                "p25": float(np.quantile(vals, 0.25)),
                "p75": float(np.quantile(vals, 0.75)),
                "min": float(vals.min()),
                "max": float(vals.max()),
            }
    return dict(out)


# --- Brief targets logic --------------------------------------------------


# For each metric: direction the spot must move to be DISTINCTIVE.
# 'low' = lower is distinctive (e.g. brand-color gap: low = commit to brand)
# 'high' = higher is distinctive (e.g. anchor_share)
# 'extreme' = away from category median in either direction
DIRECTION: dict[str, str] = {
    "color_gap_deg": "extreme",       # extreme either way is distinctive
    "brand_anchor": "extreme",
    "diversity": "extreme",
    "sat_std": "extreme",
    "voice_fraction": "low",           # most are voice-led; low voice is distinctive
    "music_fraction": "high",          # symmetric
    "tempo_bpm": "extreme",
    "rms_std": "high",
    "spectral_centroid_mean": "extreme",
}


def _spot_brief_targets(
    spot: dict, sector_bench: dict[str, dict[str, float]]
) -> list[dict[str, str]]:
    """Generate 3-4 specific numerical recommendations for the spot.

    Looks at the spot's current values vs sector benchmarks; suggests
    moves that would take the spot OUT of the category default into the
    distinctive zone.
    """
    suggestions: list[dict[str, str]] = []
    for metric, direction in DIRECTION.items():
        if metric not in sector_bench or metric not in spot:
            continue
        bench = sector_bench[metric]
        val = float(spot[metric])
        if direction == "low" and val > bench["p25"]:
            target = bench["p25"]
            suggestions.append(
                {
                    "metric": metric,
                    "current": val,
                    "target": target,
                    "direction": "lower",
                    "rationale": f"Push below {target:.2f} to leave the {sector_bench['_sector']} default (the sector's lower edge).",
                }
            )
        elif direction == "high" and val < bench["p75"]:
            target = bench["p75"]
            suggestions.append(
                {
                    "metric": metric,
                    "current": val,
                    "target": target,
                    "direction": "higher",
                    "rationale": f"Push above {target:.2f} to leave the {sector_bench['_sector']} default (the sector's upper edge).",
                }
            )
        elif direction == "extreme":
            d_low = val - bench["p25"]
            d_high = bench["p75"] - val
            if abs(d_low) < abs(d_high) and val > bench["p25"]:
                target = bench["p25"]
                suggestions.append(
                    {
                        "metric": metric,
                        "current": val,
                        "target": target,
                        "direction": "lower",
                        "rationale": f"Closer to the low side; push below {target:.2f} (the sector's lower edge).",
                    }
                )
            elif val < bench["p75"]:
                target = bench["p75"]
                suggestions.append(
                    {
                        "metric": metric,
                        "current": val,
                        "target": target,
                        "direction": "higher",
                        "rationale": f"Closer to the high side; push above {target:.2f} (the sector's upper edge).",
                    }
                )
    # Limit to 5
    return suggestions[:5]


# --- Main -----------------------------------------------------------------


def _load_titles() -> dict[str, str]:
    """Load spot_id -> readable title (with diacritics) if available."""
    path = HERE / "data" / "titles.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    color = _read_color_spots()
    sound = _read_sound_spots()
    merged = _join_by_spot_id(color, sound)
    titles = _load_titles()
    merged["title"] = merged["spot_id"].map(titles).fillna(merged["spot_id"])
    logger.info("Joined: %d spots across %d brands (%d with titles)", len(merged), merged["brand"].nunique(), int(merged["spot_id"].isin(titles).sum()))

    metrics_for_bench = [
        "color_gap_deg",
        "brand_anchor",
        "diversity",
        "sat_std",
        "voice_fraction",
        "music_fraction",
        "tempo_bpm",
        "rms_std",
        "spectral_centroid_mean",
    ]
    sector_bench = _sector_summary(merged, metrics_for_bench)

    # Build per-spot brief targets, requires sector context attached
    spots: list[dict[str, object]] = []
    for _, row in merged.iterrows():
        spot_dict = row.to_dict()
        sb = dict(sector_bench.get(row["sector"], {}))
        sb["_sector"] = row["sector"]
        spot_dict["brief_targets"] = _spot_brief_targets(spot_dict, sb)
        spots.append(spot_dict)

    payload = {
        "version": 1,
        "n_spots": len(spots),
        "n_brands": int(merged["brand"].nunique()),
        "sectors": sorted(merged["sector"].unique().tolist()),
        "spots": spots,
        "sector_benchmarks": sector_bench,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    # JSON: ensure all numpy types serialize
    def _enc(o: object) -> object:
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return float(o)
        if isinstance(o, (np.ndarray,)):
            return o.tolist()
        raise TypeError(type(o))

    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_enc), encoding="utf-8")
    logger.info("Wrote %d spots -> %s (%.1f KB)", len(spots), OUT_PATH, OUT_PATH.stat().st_size / 1024)


if __name__ == "__main__":
    main()
