# ApkSession

Records a complete walkthrough of an Android app from a Flutter codebase **without
touching the source**, then removes the client's brand so the real UI can be shown
outside that engagement — chaptered, captioned, documented by timestamp, and bundled
with its own evidence.

Built for interview and portfolio demos: the UI has to be exactly what shipped, and the
client's identity must not be recoverable from the artifact. Both are hard requirements,
and they pull against each other. That tension is what most of this code is about.

## Layout

```
tool/                 the adrd CLI + its self-test (also installed as a Qoder skill)
  scripts/adrd/       12 modules: adb, ffmpeg, flowmap, timeline, render, watch, export...
  tests/run.sh        end-to-end self-test with no phone attached
sessions/             one folder per recording, resumable stage by stage
NOTES.md              the R&D findings - read this before changing anything
```

## Setup

```sh
python3 -m pip install imageio-ffmpeg pillow    # ffmpeg static build, ~30 MB, no ffprobe
./setup.sh                                      # checks deps, creates the emulator AVD
tool/tests/run.sh                               # must print PASS before you trust edits
```

## The pipeline

Each stage reads the session folder and writes the next record. Nothing has to be
redone when only a box or a step changes.

```sh
ADRD="python3 tool/scripts/adrd.py"
S=sessions/union-ai-20261023-1524

$ADRD new     --project /path/to/flutter/app   # creates the session
$ADRD flow    --session $S                     # routes + modules, read statically
$ADRD scaffold --session $S                    # walk skeleton, one chapter per module
$ADRD launch  --session $S --clear             # cold, logged-out start
$ADRD scout   --session $S                     # replay to calibrate; records nothing
$ADRD take    --session $S                     # RECORD. scout does not.
$ADRD brand   add --session $S --screen login --rect x,y,w,h --kind brand
$ADRD render  --session $S --step-captions
$ADRD proof   --session $S && $ADRD sweep --session $S
$ADRD doc     --session $S
$ADRD export  --session $S --yes --brief "..."
$ADRD watch   --session $S --device --public --daemon
```

`scout` and `take` are the same replay. Only `take` records. Confusing them wasted real
time here, so: **nothing exists on the dashboard until `take` has run.**

## Masking policy

Two tiers, chosen per box with `--kind`:

| tier | what | effect |
|---|---|---|
| `brand` | app name, app logo, wordmark, product phrases in copy | full blur, sigma sized to the box |
| `pii` | email, mobile, password, user and partner names | 0.4 of that strength |

Boxes are stored as **fractions of the screen**, so they survive a re-record on a
different device. Boxes with the same rect and mode merge into one filter, so "the wordmark
on nine screens" costs one entry in the filtergraph.

`--sigma 0` (the default) sizes the blur to the box. This matters: a fixed sigma 22 left a
140px wordmark **fully readable**, and mosaic at /14 did too. Anything weaker than roughly
`0.25 x min(width,height)` gets flagged before the render rather than shipped silently.

## The dashboard

`http://127.0.0.1:8734/` is fixed. `--public` puts it behind a Cloudflare quick tunnel
with a forced access token, and `--daemon` keeps it alive independently of the shell that
started it. The tunnel is a separate long-lived process, so restarting the dashboard no
longer changes the hostname.

It shows the device live with your boxes drawn over it, a masked/unmasked toggle on the
rendered video, every chapter, every evidence image individually downloadable, the whole
folder as a zip, and an Export all button.

**A public tunnel exposes unmasked client screens.** The token is the only gate. It dies
with the process and never survives a reboot; a permanent hostname needs a Cloudflare
named tunnel on a domain you control, or Tailscale.

## What is shareable

Only `render/demo_debranded.mp4`, `render/demo_chapters.vtt` and `render/SESSION_DOC.md`.
Everything else in a session — stills, `recording.mp4`, the proof sheet, the `SRC` column
of the region crops — deliberately still contains the brand. `export.zip` includes the
unmasked capture on purpose and labels it `READ_ME_FIRST.txt`.

## Read before trusting any of this

The automated region check **cannot** certify that branding is gone. It measures edge
energy, and edge energy saturates: sigma 12 and sigma 100 scored 0.23 and 0.15 on the same
wordmark, and sigma 12 was plainly readable. It also only ever judges boxes someone
declared — brand outside them is invisible to it.

So the gate is `sweep.png`, read by a human, every time. That check has caught real leaks
here twice: a product name surviving in a dialog body, and the tool's own burned-in
chapter caption naming the product.

See [NOTES.md](NOTES.md) for the findings that shaped these design decisions.
