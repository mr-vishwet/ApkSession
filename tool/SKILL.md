---
name: app-demo-recorder
description: >-
  Records a complete Android app walkthrough from a Flutter codebase without editing
  the code, then de-brands it: masks the client logo, app name and wordmark, burns in
  tutorial-style chapter captions, embeds real video chapters, and writes a timestamped
  SESSION_DOC.md explaining what happens at each timecode. Use when the user wants a
  demo/walkthrough/screen-recording of an app they built, a portfolio or interview video
  that must show their UI but hide the client's brand, a module-by-module recorded flow,
  or asks to "record the app", "blur the branding", "de-brand the demo", "walk through
  the app and document it". Phase 1 is Android + Flutter; the walk is a replayable
  calibrated script, so re-records are free.
---

# App demo recorder

Turn a running app into a shareable, chaptered walkthrough with the client's brand
removed. The app source is **never modified** — read only, plus an optional throwaway
`build/` artifact.

## What you are driving

```
ADRD="$PWD/adrd"   # the repo root wrapper; run ./setup.sh first
```

That wrapper runs the pinned `.venv`; the tool breaks if you call `python3` directly
and PATH happens to point at an interpreter without Pillow. The skill is a symlink to
`ApkSession/tool`, so edit the project, not `~/.qoder/skills`.

Every stage reads and writes one session directory (default
`~/.qoder/app-demo-sessions/<app>-<date>-<time>/`). Stages are resumable and re-runnable;
nothing needs to be redone from scratch when only the boxes or the script change.

| stage | command | produces |
|---|---|---|
| check | `$ADRD doctor` | what is missing on this machine |
| session | `$ADRD new --project <path> --out <dir>` | session dir, app id, package |
| flow | `$ADRD flow --session $S` | `flow_map.json`, `FLOW_MAP.md` |
| script | `$ADRD scaffold --session $S` | `walk.json` skeleton, one chapter per module |
| launch | `$ADRD launch --session $S [--build]` | cold start on device/emulator |
| calibrate | `$ADRD shot / pick / scout --session $S` | per-screen stills + coordinates |
| record | `$ADRD take --session $S` | `recording.mp4`, `timeline.json` |
| re-time | `$ADRD retimeline --session $S [--offset SEC]` | re-fit clocks without re-recording |
| mask | `$ADRD brand add/list/preset/rm --session $S` | `branding.json` |
| review | `$ADRD sheet --session $S` | annotated contact sheet of every screen |
| render | `$ADRD render --session $S --step-captions` | `render/demo_debranded.mp4` + `.vtt` |
| verify | `$ADRD proof` / `$ADRD sweep --session $S` | region proof sheet + full-frame sweep |
| document | `$ADRD doc --session $S` | `render/SESSION_DOC.md`, `ARTIFACTS.md` |
| watch | `$ADRD watch --session $S` | live dashboard on a fixed port |

## The live dashboard

`$ADRD take --watch`, `scout --watch` and `render --watch` serve a dashboard in-process
for the duration of the command (default `http://127.0.0.1:8734/`, walking up to 8743 if
busy, so the URL is stable), then keep it up for `--keep-alive` seconds. `adrd watch
--session $S --device` serves it standalone, and `/` lists every session you have ever
recorded.

It shows the device frame with your declared brand boxes drawn over it live — so you can
see whether a box actually covers the logo *before* spending a render — plus the chapter
rail with the step executing now, `verify` mismatches, real render progress from ffmpeg's
`-progress`, and download links for every artifact.

**The frames on that page are unmasked client app content.** It binds `127.0.0.1` by
default. `--lan` binds `0.0.0.0` for a phone or laptop on the same network and generates
an access token that every route then requires, including the index. Do not put this
behind a tunnel or forward the port: that is the one artifact in the whole pipeline where
the brand is still visible.

## Non-negotiables

1. **Do not edit the app source.** `flow` is static analysis; `launch --build` runs
   `flutter build apk --debug`, which writes only into `build/`. If a change seems
   needed to make a screen reachable, stop and ask.
2. **The session directory is confidential.** `stills/`, `recording.mp4`,
   `debrand_proof.png`, the `SRC` column of the proof sheet and **the live dashboard
   itself** all contain the client brand — that is the point of them. Only
   `render/demo_debranded.mp4`, `render/demo_chapters.vtt` and `render/SESSION_DOC.md`
   are shareable. Say so when handing off.
3. **Never claim "branding is gone" from the numbers.** The per-region check measures
   edge energy, and edge energy saturates: sigma 12 and sigma 100 scored 0.23 and 0.15
   on the same wordmark, and sigma 22 was still perfectly readable. `$ADRD sweep`
   produces full frames — **read that image** before you say the video is safe.
4. **A box only hides what is inside it.** Branding outside the declared rectangles is
   invisible to every automated check. This is why stage 5 is a gate, not a formality.

## The workflow

### 1. Read the flow out of the code

`$ADRD flow` parses GoRouter / `MaterialApp.routes` / `onGenerateRoute` declarations,
`AppRoutes` constants, and every `context.go/push` + `Navigator.pushNamed` call site.
Routes are attributed to the module that owns the **screen widget**, not the file that
declares them, so an `app_router.dart` monolith still yields `auth`, `home`, `reports`.

Read `FLOW_MAP.md` and confirm the suggested chapter order with the user before
recording. Module names are folder-structure inference and will sometimes be wrong;
unresolved navigation calls are listed, not hidden.

### 2. Get it on a screen

`doctor` first: it reports adb, ffmpeg (a pip static build is enough — no ffprobe is
shipped, and the code reads media headers off `ffmpeg -i` stderr instead), Pillow, a
caption font, and whether a device or AVD exists.

`launch` installs an existing APK (`--apk`) or builds a debug one (`--build`) and cold
starts it. **Debug builds draw Flutter's MODEL_MARKER banner in the top-left of every
screen** — that plus the "DEBUG" text tells an interviewer this is not a store build.
Handle it with `brand preset --name debug-banner`, or record a release build instead.

### 3. Calibrate the walk (the only expensive step)

Flutter paints into a single `FlutterView`, so `uiautomator` exposes no widget ids or
text, and this machine has no OCR. Coordinates therefore come from the screenshots, and
you are the reader:

1. `$ADRD shot --session $S --name login` → a still plus the screen size.
2. Look at the still. Estimate the tap target as **normalized** `rx,ry` (0..1 of width
   and height) — normalized coordinates are what let one walk replay on both an
   emulator and the user's Vivo.
3. `$ADRD pick --session $S --image login.png --at 0.5,0.83` draws a crosshair so you
   can confirm the point before committing it.
4. Write the step into `walk.json`, then `$ADRD scout --session $S` to replay it with no
   recording and check each screen actually arrives.

Step shape:

```json
{"chapter": "Login", "label": "Sign in with demo account", "screen": "login",
 "verify": true,
 "do": [{"op": "tap", "rx": 0.5, "ry": 0.42},
        {"op": "wait", "s": 0.5},
        {"op": "text", "value": "demo@mail.com"},
        {"op": "tap", "rx": 0.5, "ry": 0.62},
        {"op": "text", "value": "hunter2", "secret": true}]}
```

`chapter` opens a new chapter at that step. `screen` names the still used for box
placement and binds brand boxes to the time window that screen occupies. `verify`
captures a hash so a step that failed to navigate is reported instead of recorded.
**`secret: true` keeps the literal out of `walk.json` echoes and the docs** — use it for
every password. Other ops: `long_press`, `swipe` (`rx,ry` → `rx2,ry2`), `key`, `back`,
`hide_keyboard`, `screenshot`.

Scout until it is clean, then record once. After that, re-records are free: fix the
script, `$ADRD take` again, boxes and chapter names survive because they are stored as
fractions.

### 4. Record

`$ADRD take` starts `screenrecord` in ≤29-minute segments, replays the walk, timestamps
every step against the host clock, stops with `SIGINT` so the moov atom flushes, pulls,
concatenates, then puts every timestamp onto the **video's own clock**.

That second step is not a nicety. `screenrecord` encodes on surface invalidation, not on
a timer: measured on an emulator, 5 wall seconds on a static screen wrote a **0.00s**
file, while 5 seconds of swiping wrote 3.30s. Pauses never reach the video, so host time
runs ahead of video time by however long the walkthrough dawdled, and no single constant
offset describes it. `take` therefore fits a scale plus offset from the transition
sequence, matches each transition to the scene cut that *is* that transition
(order-preserving DP, extra cuts from keyboards and animations are ignored cheaply), and
interpolates everything in between. `timeline.json`'s `align` records the method, how
many transitions snapped, the residual, and the scale.

Consequences to state out loud:

- Timecodes in `SESSION_DOC.md` are **video time**, which is what a viewer experiences.
  A 40-minute walkthrough can ship as a 12-minute file. That is usually a feature.
- If `align.scale` is well under 1, say so: the pauses were compressed out.
- `ALIGN WEAK` or a `constant offset` fallback means the cuts did not corroborate the
  script. Scrub each chapter start, or re-run `retimeline --offset SEC`.

Verified on the emulator: `screenrecord` and a concurrent `screencap` coexist, so the
live dashboard works during a take.

### 5. Place the brand boxes, then prove them

`$ADRD sheet` renders every screen with its boxes drawn on it. **Read that image.**
For each screen, look for: app bar wordmark, splash logo, launcher glyph, client name in
body copy, onboarding artwork, store screenshots, user avatars and emails, and the
package id in any debug overlay.

```
$ADRD brand add --session $S --screen login --rect 160,105,560,140 \
                --label "app wordmark" --note "client name in app bar"
$ADRD brand add --session $S --screen home --rect 520,115,90,90 --mode pixelate --label "brand icon"
$ADRD brand preset --session $S --name debug-banner status-bar
```

Leave `--sigma` at 0 so the blur sizes itself to the box (a 140px-tall wordmark needs
sigma ~53; sigma 22 leaves it readable). `--mode solid` is the only choice that is
certain without looking. Boxes with the same rect and mode merge into one filter, so
"the wordmark on all six screens" costs one entry in the graph, not six.

Then render and gate:

```
$ADRD render --session $S --step-captions --clock
$ADRD proof  --session $S      # per-box crops vs source stills
$ADRD sweep  --session $S      # full frames — the one that can see unmarked branding
$ADRD doc    --session $S
```

Read `sweep.png` end to end. Anything still readable is a box you did not declare: add
it, re-render, sweep again. `proof` returning `unchanged - box is wrong` means that box
covers nothing or its screen window never matched — fix the `screen` name in the walk.

### 6. Hand off

Give the user: `demo_debranded.mp4` (chapters are embedded and a `.vtt` ships beside it),
`SESSION_DOC.md` (chapter table with start/end timecodes, then a per-chapter step table
with what happened, coordinates, and typed values — secrets redacted), and the honest
caveats from its "Known gaps" section.

## Failure modes

- **`screencap` returns nothing / black frames** — the screen sets `FLAG_SECURE`
  (payment, auth, some OEM lock screens) or the ROM blocks capture. Record around it or
  drop that screen; do not fake it.
- **`screenrecord` dies instantly** — Vivo/OEM power management. Try
  `adb shell settings put global window_animation_scale 0`, keep the screen awake with
  `adb shell svc power stayon true`, and check `--verbose` output in the segment log.
- **captions drift against the action** — alignment failed; `retimeline --offset`.
- **`no time window for screen:x`** — the walk never set `screen` on that step, or the
  take didn't reach it.
- **render slow** — 1080x2400 with ~36 filters ran ~22s per minute of video on an 8 GB
  Mac. `--preset superfast --crf 26` for drafts; keep the good one at defaults.
- **the walk lands on the wrong screen after an app update** — that is what `verify`
  reports; re-calibrate only the broken steps.

## Beyond phase 1

Not built: iOS simulator capture (`xcrun simctl io recordVideo` reuses the same
align/blur/doc stages), narration/audio, and per-step auto-advance from the flow map.
The `walk.json` → `timeline.json` → `branding.json` → render contract is what those
would extend.
