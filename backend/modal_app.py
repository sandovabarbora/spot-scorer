"""Modal serverless backend for arbitrary URL scoring.

Single endpoint: POST /score with {url, sector} → full metrics + brief
targets in the same JSON shape the frontend already renders.

Deploy:
    modal deploy backend/modal_app.py

The first call after deploy is a cold start (image build + warm-up,
roughly 30 to 60 seconds). Subsequent calls are 10 to 25 seconds for a
30 to 60 second TV spot.

Supported source platforms (via yt-dlp): YouTube, Facebook public posts,
Instagram public posts. Reels and stories are unreliable; the endpoint
returns a graceful error if the download fails.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path

import modal

# ---- Modal app + image ---------------------------------------------------

app = modal.App("spot-scorer")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install(
        "yt-dlp==2026.3.17",
        "ffmpeg-python==0.2.0",
        "opencv-python-headless>=4.10.0",
        "scikit-learn>=1.5.0",
        "numpy>=1.26.0",
        "librosa>=0.10.2",
        "soundfile>=0.12.1",
        "silero-vad>=5.1.0",
        "torch>=2.2.0",
        "torchaudio>=2.2.0",
        "fastapi[standard]",
        "Pillow>=10.0.0",
    )
    .add_local_file(
        Path(__file__).parent / "_sector_benchmarks.json",
        remote_path="/root/_sector_benchmarks.json",
    )
)


# ---- Constants (mirrored from the two sister projects) ------------------

KMEANS_K = 5
RANDOM_STATE = 42
TARGET_SR = 22050
VAD_SR = 16000
INTRO_SKIP_S = 0.3
OUTRO_SKIP_S = 0.3
DOWNSAMPLE_MAX_EDGE = 200
MIN_CANONICAL_L = 7.84
BRAND_ANCHOR_TOLERANCE_DEG = 15.0
DOMINANT_HUE_SAT_MIN = 0.15

# Brief-target direction (mirrored from scripts/refresh_data.py)
DIRECTION = {
    "color_gap_deg": "extreme",
    "brand_anchor": "extreme",
    "diversity": "extreme",
    "sat_std": "extreme",
    "voice_fraction": "low",
    "music_fraction": "high",
    "tempo_bpm": "extreme",
    "rms_std": "high",
    "spectral_centroid_mean": "extreme",
}

KS_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
KS_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
PITCH_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


logger = logging.getLogger("spot-scorer-backend")
logger.setLevel(logging.INFO)


# ---- Download ------------------------------------------------------------


def download_video(url: str, target_dir: Path) -> Path:
    """yt-dlp the video to target_dir; return path to the file.

    Raises RuntimeError on download failure.
    """
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError, ExtractorError

    out_template = str(target_dir / "spot.%(ext)s")
    opts = {
        "outtmpl": out_template,
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 3,
        "socket_timeout": 30,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except (DownloadError, ExtractorError) as exc:
        raise RuntimeError(f"Could not download URL: {exc}") from exc

    if "entries" in (info or {}):
        info = (info["entries"] or [None])[0]
    if not info:
        raise RuntimeError("Empty result from yt-dlp")

    duration = float(info.get("duration") or 0)
    if duration < 5:
        raise RuntimeError(f"Clip too short ({duration:.1f}s); need at least 5s of content")
    if duration > 600:
        raise RuntimeError(f"Clip too long ({duration:.1f}s); cap is 10 minutes")

    files = sorted(target_dir.glob("spot.*"))
    if not files:
        raise RuntimeError("Download appeared to succeed but no file was written")
    return files[0]


# ---- Frame extraction ----------------------------------------------------


def extract_frames(video_path: Path, frames_dir: Path) -> list[Path]:
    """1 fps frames via ffmpeg subprocess (no ffmpeg-python on cold path)."""
    import ffmpeg

    frames_dir.mkdir(parents=True, exist_ok=True)
    duration = float(ffmpeg.probe(str(video_path))["format"]["duration"])
    window = duration - INTRO_SKIP_S - OUTRO_SKIP_S
    if window <= 0:
        return []
    pattern = str(frames_dir / "f_%04d.jpg")
    (
        ffmpeg.input(str(video_path), ss=INTRO_SKIP_S, t=window)
        .filter("fps", fps=1)
        .output(pattern, **{"qscale:v": 5, "start_number": 0})
        .overwrite_output()
        .run(quiet=True, capture_stdout=True, capture_stderr=True)
    )
    return sorted(frames_dir.glob("f_*.jpg"))


def extract_audio(video_path: Path, audio_path: Path) -> Path:
    """Mono 22.05 kHz wav via ffmpeg."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(video_path),
            "-ac", "1", "-ar", str(TARGET_SR), "-vn",
            str(audio_path),
        ],
        capture_output=True, check=True,
    )
    return audio_path


# ---- Color pipeline (mirrors src/color.py from color project) -----------


def _opencv_lab_to_canonical(lab_opencv):
    import numpy as np
    out = lab_opencv.astype(np.float32)
    out[..., 0] *= 100.0 / 255.0
    out[..., 1] -= 128.0
    out[..., 2] -= 128.0
    return out


def _canonical_lab_to_rgb(lab_canonical):
    import cv2
    import numpy as np
    lab_opencv = lab_canonical.copy()
    lab_opencv[..., 0] *= 255.0 / 100.0
    lab_opencv[..., 1] += 128.0
    lab_opencv[..., 2] += 128.0
    lab_opencv = np.clip(lab_opencv, 0, 255).astype(np.uint8)
    as_image = lab_opencv.reshape(1, -1, 3)
    bgr = cv2.cvtColor(as_image, cv2.COLOR_LAB2BGR).reshape(-1, 3)
    return bgr[:, ::-1].copy()


def extract_palette(frame_path: Path):
    import cv2
    import numpy as np
    from sklearn.cluster import KMeans

    bgr = cv2.imread(str(frame_path))
    if bgr is None:
        return None
    h, w = bgr.shape[:2]
    longest = max(h, w)
    if longest > DOWNSAMPLE_MAX_EDGE:
        scale = DOWNSAMPLE_MAX_EDGE / longest
        bgr = cv2.resize(bgr, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    lab_opencv = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    lab_canonical = _opencv_lab_to_canonical(lab_opencv)
    pixels = lab_canonical.reshape(-1, 3)
    if pixels[:, 0].mean() < MIN_CANONICAL_L:
        return None
    km = KMeans(n_clusters=KMEANS_K, random_state=RANDOM_STATE, n_init=10).fit(pixels)
    labels = km.labels_
    centroids = km.cluster_centers_
    counts = np.bincount(labels, minlength=KMEANS_K).astype(np.float64)
    weights = counts / counts.sum()
    order = np.argsort(weights)[::-1]
    centroids = centroids[order]
    weights = weights[order]
    rgb = _canonical_lab_to_rgb(centroids)
    return {"centroids": centroids, "weights": weights, "rgb": rgb}


def compute_color_metrics(frames: list[Path], anchor_hex: str | None):
    import colorsys

    import numpy as np

    palettes = [extract_palette(fp) for fp in frames]
    palettes = [p for p in palettes if p is not None]
    if not palettes:
        return None

    per_frame_div = []
    per_frame_sat = []
    all_centroids = []
    all_weights = []
    all_rgb = []
    for p in palettes:
        c, w = p["centroids"], p["weights"]
        # diversity for this frame
        diff = c[:, None, :] - c[None, :, :]
        d = np.linalg.norm(diff, axis=-1)
        w_outer = w[:, None] * w[None, :]
        per_frame_div.append(float((w_outer * d).sum()))
        # saturation
        rgb = p["rgb"]
        sats = np.array(
            [colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)[1] for r, g, b in rgb],
            dtype=np.float64,
        )
        per_frame_sat.append(float((w * sats).sum()))
        all_centroids.append(c)
        all_weights.append(w / len(palettes))
        all_rgb.append(rgb)

    all_centroids = np.vstack(all_centroids)
    all_weights = np.concatenate(all_weights)
    all_rgb = np.vstack(all_rgb)

    diversity = float(np.mean(per_frame_div))
    sat_mean = float(np.mean(per_frame_sat))
    sat_std = float(np.std(per_frame_sat))

    # Anchor share + color gap
    brand_anchor = 0.0
    color_gap = None
    if anchor_hex:
        anchor_rgb = tuple(int(anchor_hex.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4))
        anchor_hue = colorsys.rgb_to_hsv(*anchor_rgb)[0] * 360.0
        hues = np.array(
            [colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)[0] * 360.0 for r, g, b in all_rgb],
            dtype=np.float64,
        )
        sats = np.array(
            [colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)[1] for r, g, b in all_rgb],
            dtype=np.float64,
        )
        # Brand anchor (±15° hue)
        d_h = np.minimum(np.abs(hues - anchor_hue), 360 - np.abs(hues - anchor_hue))
        mask = d_h <= BRAND_ANCHOR_TOLERANCE_DEG
        total = float(all_weights.sum())
        brand_anchor = float(all_weights[mask].sum() / total) if total > 0 else 0.0
        # Dominant chromatic hue (saturation-weighted circular mean)
        chromatic = sats >= DOMINANT_HUE_SAT_MIN
        if chromatic.any():
            w_chrom = all_weights[chromatic] * sats[chromatic]
            ang = np.deg2rad(hues[chromatic])
            x = float((w_chrom * np.cos(ang)).sum())
            y = float((w_chrom * np.sin(ang)).sum())
            dom = float(np.rad2deg(np.arctan2(y, x)) % 360.0)
            color_gap = float(min(abs(dom - anchor_hue), 360 - abs(dom - anchor_hue)))

    # Arc shape
    arc = "flat"
    if len(per_frame_sat) >= 3:
        s_arr = np.array(per_frame_sat)
        if s_arr.max() - s_arr.min() >= 0.10:
            t = np.linspace(0, 1, len(s_arr))
            a, b, _c = np.polyfit(t, s_arr, 2)
            if abs(a) >= 0.4 and 0.2 <= -b / (2 * a) <= 0.8:
                arc = "peak" if a < 0 else "valley"
            else:
                arc = "rising" if np.polyfit(t, s_arr, 1)[0] > 0 else "falling"

    return {
        "diversity": diversity,
        "sat_mean": sat_mean,
        "sat_std": sat_std,
        "brand_anchor": brand_anchor,
        "color_gap_deg": color_gap if color_gap is not None else float("nan"),
        "arc_shape": arc,
        "n_frames": len(palettes),
    }


# ---- Audio pipeline ------------------------------------------------------


def compute_audio_metrics(audio_path: Path):
    import librosa
    import numpy as np
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad

    y, sr = librosa.load(str(audio_path), sr=TARGET_SR, mono=True)
    n_intro = int(INTRO_SKIP_S * sr)
    n_outro = int(OUTRO_SKIP_S * sr)
    if len(y) > n_intro + n_outro + sr:
        y = y[n_intro : len(y) - n_outro]
    duration = float(len(y) / sr)

    tempo_arr, _ = librosa.beat.beat_track(y=y, sr=sr)
    tempo = float(np.atleast_1d(tempo_arr)[0])

    # Key via Krumhansl-Schmuckler
    chroma = librosa.feature.chroma_stft(y=y, sr=sr).mean(axis=1)
    chroma = chroma / (chroma.sum() + 1e-9)
    best_corr, best = -np.inf, (0, "major")
    for rot in range(12):
        rolled = np.roll(chroma, -rot)
        for mode, profile in (("major", KS_MAJOR), ("minor", KS_MINOR)):
            r = float(np.corrcoef(rolled, profile)[0, 1])
            if r > best_corr:
                best_corr = r
                best = (rot, mode)
    key = PITCH_NAMES[best[0]]
    mode = best[1]

    cent = librosa.feature.spectral_centroid(y=y, sr=sr).squeeze()
    rms = librosa.feature.rms(y=y).squeeze()
    onsets = librosa.onset.onset_detect(y=y, sr=sr, units="time")

    # VAD
    y16 = librosa.resample(y, orig_sr=sr, target_sr=VAD_SR) if sr != VAD_SR else y
    model = load_silero_vad()
    ts = get_speech_timestamps(
        torch.from_numpy(y16.astype(np.float32)),
        model,
        sampling_rate=VAD_SR,
        threshold=0.5,
        min_speech_duration_ms=250,
    )
    speech_samples = sum(t["end"] - t["start"] for t in ts)
    voice_frac = float(speech_samples / max(len(y16), 1))

    rms_norm = rms / (rms.max() + 1e-9)
    content_frac = float((rms_norm >= 0.01).mean())
    music_frac = max(content_frac - voice_frac, 0.0)

    return {
        "duration_s": duration,
        "tempo_bpm": tempo,
        "key": key,
        "mode": mode,
        "voice_fraction": voice_frac,
        "music_fraction": music_frac,
        "rms_mean": float(rms.mean()),
        "rms_std": float(rms.std()),
        "spectral_centroid_mean": float(cent.mean()),
        "onset_rate": float(len(onsets) / max(duration, 1e-6)),
    }


# ---- Brief targets ------------------------------------------------------


def brief_targets(spot: dict, sector_bench: dict, sector_name: str):
    out = []
    for metric, direction in DIRECTION.items():
        if metric not in sector_bench or metric not in spot:
            continue
        bench = sector_bench[metric]
        val = float(spot[metric])
        if direction == "low" and val > bench["p25"]:
            target = bench["p25"]
            out.append({
                "metric": metric, "current": val, "target": target,
                "direction": "lower",
                "rationale": f"Push below {target:.2f} to leave the {sector_name} default (the sector's lower edge).",
            })
        elif direction == "high" and val < bench["p75"]:
            target = bench["p75"]
            out.append({
                "metric": metric, "current": val, "target": target,
                "direction": "higher",
                "rationale": f"Push above {target:.2f} to leave the {sector_name} default (the sector's upper edge).",
            })
        elif direction == "extreme":
            d_low = val - bench["p25"]
            d_high = bench["p75"] - val
            if abs(d_low) < abs(d_high) and val > bench["p25"]:
                target = bench["p25"]
                out.append({
                    "metric": metric, "current": val, "target": target,
                    "direction": "lower",
                    "rationale": f"Closer to the low side; push below {target:.2f} (the sector's lower edge).",
                })
            elif val < bench["p75"]:
                target = bench["p75"]
                out.append({
                    "metric": metric, "current": val, "target": target,
                    "direction": "higher",
                    "rationale": f"Closer to the high side; push above {target:.2f} (the sector's upper edge).",
                })
    return out[:5]


# ---- Endpoint ------------------------------------------------------------


_URL_RE = re.compile(r"^https?://")


@app.function(
    image=image,
    timeout=300,
    cpu=2.0,
    memory=4096,
)
@modal.fastapi_endpoint(method="POST")
def score(payload: dict):
    """POST {url: str, sector: str, anchor_hex?: str} → metrics + brief targets."""
    from fastapi.responses import JSONResponse

    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }

    url = (payload.get("url") or "").strip()
    if not _URL_RE.match(url):
        return JSONResponse({"error": "url must start with http:// or https://"}, status_code=400, headers=headers)
    sector = (payload.get("sector") or "All").strip()
    anchor_hex = (payload.get("anchor_hex") or None)

    # Load embedded benchmarks
    sb_path = Path("/root/_sector_benchmarks.json")
    sector_bench_all = json.loads(sb_path.read_text())
    sector_bench = sector_bench_all.get(sector, sector_bench_all.get("All", {}))

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        try:
            video_path = download_video(url, td_path)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400, headers=headers)

        try:
            frames = extract_frames(video_path, td_path / "frames")
            color = compute_color_metrics(frames, anchor_hex) if frames else None
        except Exception as exc:
            color = None
            logger.warning("color pipeline failed: %s", exc)

        audio_path = td_path / "audio.wav"
        try:
            extract_audio(video_path, audio_path)
            audio = compute_audio_metrics(audio_path)
        except Exception as exc:
            audio = None
            logger.warning("audio pipeline failed: %s", exc)

    if not color and not audio:
        return JSONResponse({"error": "Both color and audio pipelines failed"}, status_code=500, headers=headers)

    spot = {"url": url, "sector": sector, "anchor_hex": anchor_hex}
    if color:
        spot.update(color)
    if audio:
        spot.update(audio)
    spot["brief_targets"] = brief_targets(spot, sector_bench, sector)
    return JSONResponse(spot, headers=headers)


@app.function(image=image)
@modal.fastapi_endpoint(method="GET")
def health():
    from fastapi.responses import JSONResponse
    return JSONResponse({"status": "ok"}, headers={"Access-Control-Allow-Origin": "*"})


# Silence unused-import warning from optional imports above
_ = base64, io
