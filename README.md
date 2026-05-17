# Spot Scorer

Interactive scoring tool for Czech TV advertising. Companion piece to
[color-fingerprint-cz](https://github.com/sandovabarbora/color-fingerprint-cz)
and [sound-fingerprint-cz](https://github.com/sandovabarbora/sound-fingerprint-cz):
the analytical methodology from those two studies, turned into a tool
you can hand to a strategist.

Pick a spot from the dropdown. The page shows its color and audio
metrics, the median for its sector, and four to five numerical targets
the spot would have to hit to leave its category default and become
visually or audially distinctive.

**Live:** https://sandovabarbora.github.io/spot-scorer/

## How it's built

- Static HTML + vanilla JavaScript. No framework, no build step, no
  backend. The data is baked into the page as inline JSON.
- `scripts/refresh_data.py` reads `spots.parquet` (color study) and
  `spots_audio.parquet` (sound study), joins them by spot id, computes
  per-sector median / p25 / p75 for each metric, and writes
  `data/spots.json`. The JSON is committed so the repo is self-contained.
- `scripts/build_html.py` renders `data/spots.json` into
  `outputs/index.html` using a Jinja2 template.
- The page is then deployed to GitHub Pages via the same workflow as
  the sister projects.

## Run locally

```bash
make install
make refresh     # only if sister projects' parquets have changed
make build       # renders outputs/index.html
open outputs/index.html
```

## Score any URL (Modal backend)

The static page also has an optional "Score any video" section that
accepts a YouTube / Facebook / Instagram public URL and runs the full
pipeline on demand. This requires deploying `backend/modal_app.py` to
[Modal](https://modal.com) (free tier is plenty).

```bash
# One-time: install modal client + authenticate
uv add modal
uv run modal token new

# Deploy. Modal prints the endpoint URL on success.
uv run modal deploy backend/modal_app.py

# Save the printed endpoint into the build config and rebuild
echo "https://<your-username>--spot-scorer-score.modal.run" > config/endpoint.txt
make build
```

The frontend hides the URL form when `config/endpoint.txt` is absent,
so committing without the endpoint is safe.

**Platform support:** YouTube is the most reliable. Facebook public
posts work for most URLs. Instagram Reels frequently fail (extractor
breaks every few weeks). The graceful error message tells the user
when a download or analysis stage fails.

## License

- Code: MIT
- Derived data in `data/spots.json`: CC-BY-4.0

Built by [Barbora Šandová](https://datasimply.eu).
