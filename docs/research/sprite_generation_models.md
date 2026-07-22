# Sprite Generation Model Research (July 2026)

_Research pass triggered by playtest feedback: walk frames misaligned/partial,
suspicion that `gemini-2.5-flash-image` is weak at character sprites. Compiled
from web research 2026-07-22; see source links throughout._

## TL;DR

- We are on the original **Nano Banana** (`gemini-2.5-flash-image`) and it
  **shuts down October 2, 2026** — migration is forced regardless of quality.
- **Nano Banana 2** (`gemini-3.1-flash-image`) is the recommended target:
  equal-or-better character consistency than Nano Banana Pro in hands-on
  community tests, half Pro's price, up to 14 reference images, 2K/4K output,
  same `generateContent` API.
- **Nano Banana Pro** (`gemini-3-pro-image`) is the per-strip fallback for
  cases NB2 repeatedly fails ($0.134/image vs $0.067).
- Half of our observed frame breakage is OUR pipeline (no horizontal
  re-centering, no per-frame validation, rembg eating low-contrast cells) and
  fixable with any model.
- If misalignment persists after the NB2 upgrade, the guaranteed fix is
  **image → video → frame extraction** (Kling/Seedance via fal.ai/Replicate, or
  Scenario's managed API): frames from one video clip are aligned by
  construction.

## Nickname → model ID map (per Google's official docs)

| Nickname | Model ID | Status | Max res | Ref images |
|---|---|---|---|---|
| Nano Banana | `gemini-2.5-flash-image` | **Deprecated — shutdown Oct 2, 2026** | 1024px | supported |
| Nano Banana Pro | `gemini-3-pro-image` (GA; `-preview` retired) | GA since June 2026 | 4K | up to 14, ~5-person consistency |
| Nano Banana 2 | `gemini-3.1-flash-image` (Feb 26, 2026) | GA | 4K | up to 14, up to 4 character refs |
| Nano Banana 2 Lite | `gemini-3.1-flash-lite-image` (June 30, 2026) | GA | 1K | up to 14 objects |

## Pricing (Gemini API, standard tier, per output image)

| Model | Price |
|---|---|
| `gemini-2.5-flash-image` (current) | $0.039 (1024px) |
| `gemini-3.1-flash-image` (NB2) | $0.045 (0.5K) / $0.067 (1K) / $0.101 (2K) / $0.151 (4K) |
| `gemini-3-pro-image` (NB Pro) | $0.134 (1K/2K) / $0.24 (4K); batch mode ~halves it |

Image input is near-free (≈$0.001/image). Full character today ≈ $0.27
(4 idles + 3 strips); on NB2 at 2K ≈ $0.46. Negligible either way.

## Model IDs that are dead or dying (do NOT use)

- `gemini-2.0-flash-preview-image-generation` — dead Nov 14, 2025
- `gemini-2.5-flash-image-preview` — dead Jan 15, 2026
- `gemini-3.1-flash-image-preview` — dead June 25, 2026 (use non-preview)
- `imagen-3.0-generate-002` — dead; all `imagen-4.0-*` die Aug 17, 2026
- `gemini-2.5-flash-image` — **dies Oct 2, 2026** (our current model)
- `gemini-2.5-flash` (text/vision) — dies Oct 16, 2026, already 404s for new
  users (we already migrated `vision_model` to `gemini-3.5-flash`)

## Community findings on sprite sheets from general image models

Our failure modes are universal, not Gemini-specific — one-shot "full sprite
sheet" prompts "often produce incomplete results with repeated poses"
(OpenAI forum, gpt-image-2). Techniques in increasing order of robustness:

1. Explicit layout + baseline prompts ("6 frames, same baseline, consistent
   lighting") — helps, doesn't eliminate drift.
2. Reference stacks — idle sprite + style refs with identity-preservation
   instructions; NB2/Pro are far stronger here than original Nano Banana.
3. LLM-rewritten per-frame choreography + grid + auto bg-removal — proven in
   production by [falsprite](https://github.com/lovisdotio/falsprite)
   (nano-banana-2 via fal.ai). We already do the choreography part.
4. Pose-skeleton conditioning (SD + ControlNet OpenPose + IP-Adapter) — hard
   pose control, self-hosted only; Gemini has no equivalent.
5. **Image → video → extract frames** (Kling 2.6 / Seedance 2.0, locked
   camera, green screen, "walking in place"; ffmpeg extracts 6 frames) —
   alignment guaranteed by temporal coherence. This is Scenario's official
   spritesheet workflow and the approach of
   [ai-game-spritesheets](https://github.com/chongdashu/ai-game-spritesheets).

Hands-on comparisons (Beebom, aitoolssme, Geeky Gadgets) found **NB2 ≥ NB Pro
at repeated-generation character consistency**; Pro wins on complex
composition/text. No rigorous sprite-sheet-specific benchmark exists.

## Dedicated tools

| Tool | Walk cycles | Python API | Style | Verdict |
|---|---|---|---|---|
| PixelLab | Yes (skeleton-based, 8-dir) | `pip install pixellab` | **Pixel art only** | Best mechanics, wrong style |
| Retro Diffusion | Yes (walk/idle/attack) | HTTP API | **Pixel art only** | Same |
| Scenario | Yes (via video models) | REST, ~$15/mo+ | Any, LoRA style-lock | Best managed option |
| Ludo.ai | Yes (≤64 frames) | Sales-gated | Any | API friction |
| Sprite-Sheet Diffusion (arXiv 2412.03685) | Yes (pose-guided) | Self-hosted PyTorch | Trained on tiny dataset | Research-grade |

## Decision & plan

1. **Pipeline fixes first** (model-independent, address ~half the breakage):
   per-frame validation (reject sliver/ghost cells vs strip median), horizontal
   re-centering on feet centroid, idle-cutout verification.
2. **Migrate filmstrips + idles to `gemini-3.1-flash-image`** — forced by the
   Oct 2 shutdown anyway. Pass the idle sprite as a character reference plus a
   blank 6-cell template strip as a layout reference; request 2K output.
   Fall back to `gemini-3-pro-image` for strips NB2 repeatedly fails.
3. **If misalignment persists**: adopt image→video→frames for walk cycles only
   (fal.ai/Replicate + ffmpeg), keep Gemini for idles/backgrounds/portraits.

## Flagged uncertainties

- NB2-vs-Pro consistency findings come from portrait tests, not sprite sheets.
- Layer.ai animation/API depth unverified.
- OpenRouter still lists `gemini-3-pro-image-preview`; use `gemini-3-pro-image`
  on the first-party API.
- All Gemini image output carries an invisible SynthID watermark (irrelevant
  for sprites).

## Key sources

[Gemini image docs](https://ai.google.dev/gemini-api/docs/image-generation) ·
[pricing](https://ai.google.dev/gemini-api/docs/pricing) ·
[deprecations](https://ai.google.dev/gemini-api/docs/deprecations) ·
[Nano Banana 2 announcement](https://blog.google/innovation-and-ai/technology/ai/nano-banana-2/) ·
[Nano Banana Pro announcement](https://blog.google/innovation-and-ai/products/nano-banana-pro/) ·
[falsprite](https://github.com/lovisdotio/falsprite) ·
[Scenario spritesheets](https://help.scenario.com/articles/9088582240-create-spritesheets-with-scenario) ·
[ai-game-spritesheets](https://github.com/chongdashu/ai-game-spritesheets) ·
[Sprite-Sheet Diffusion](https://arxiv.org/abs/2412.03685)
