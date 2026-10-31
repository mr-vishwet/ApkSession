# R&D notes

Measured findings, not opinions. Each one changed the design. Read this before editing —
several are counter-intuitive and were discovered by being wrong in public.

## Recording

**`screenrecord` encodes on surface invalidation, not on a clock.** Measured on the
emulator: 5 wall seconds on a static screen produced a **0.00 s** file; 5 seconds of
swiping produced **3.30 s**. Idle time is simply not in the video.

Consequences, all of them paid for:
- Host time and video time diverge by however long the walkthrough paused. A single
  constant offset cannot describe that; the first alignment model was wrong and produced
  chapter starts up to 8.5 s off.
- Alignment now anchors each chapter/screen transition to the scene cut that *is* that
  transition, via an order-preserving DP (extra cuts from keyboards and animations are
  cheap to skip, giving up on an anchor is expensive).
- The compression factor is **measured** — video duration ÷ wall duration — and bounded at
  1.0, because the encoder can remove time but never invent it. Regressing it from anchor
  pairs instead reported a confident "5% of wall time" on a take that barely compressed.
- The first screen has no cut to anchor to. The recording's origin is the first step.
- A synthetic fixture using `lavfi` colour sources **never** reproduced this, because those
  emit frames regardless. The `--compressed` test mode exists specifically to cover it.

**Killing the recorder must use `SIGINT`** (`pkill -2 -f screenrecord`). Killing the local
`adb` process leaves the device-side one running, and `SIGKILL` corrupts the mp4 by never
flushing the moov atom.

**`imageio-ffmpeg` ships ffmpeg but no ffprobe.** Duration and dimensions come from parsing
`ffmpeg -i` stderr. A static build also needs `--enable-libfreetype` or `drawtext` is absent
and no captions can be burned in.

## Blur strength

**A fixed sigma is wrong.** On a 560x140 wordmark crop: sigma 12 and sigma 22 were both
clearly readable; ~50 was the first genuinely unreadable value; sigma 100 looked the same as
50. Mosaic at `/14` was also still readable. So sigma scales with the box
(`0.38 x min(w,h)`), and anything below `0.25 x min(w,h)` is refused with a warning.

**Mosaic blocks invent their own edges**, so a pixelated crop reads as "detail survived"
unless you pre-smooth before measuring.

**`drawbox` colour must be fully opaque.** `black@0.97` let the wordmark ghost through on a
flat area — a 3% leak is perfectly visible when the surround is uniform.

**`drawbox` cannot animate.** The chapter progress bar is one lit segment per chapter using
`enable='gte(t,N)'`, not a moving width.

## The verification metric

**Edge energy saturates and cannot certify readability.** Sigma 12 → ratio 0.23, sigma 100 →
0.15. The metric cannot tell readable from safe, so it was demoted: it reports
`unchanged - box is wrong` (a real defect: wrong place or wrong time window),
`flat source`, and `strongly reduced - confirm by eye`. Nothing claims "masked".

**A box only hides what is inside it.** Branding outside declared rectangles is invisible to
every automated check here. The gate is a full-frame contact sheet read by a human. That
check found two real leaks the numbers had passed:
1. a product name surviving in a dialog body, because the box sat 12% too high;
2. the tool's **own burned-in chapter caption** naming the product.

The shareable `SESSION_DOC.md` also leaked, via labels I had typed myself. The redactor now
folds out every word of the app name and package id rather than a hand-maintained list.

## Flutter and adb

**Flutter paints into a single `FlutterView`, so `uiautomator` exposes no widget ids, text
or bounds.** Coordinates must come from screenshots, and this machine has no OCR. That is
why the walk is calibrated once and then replayed, rather than driven from the code.

**System dialogs are the exception and do expose bounds.** The notification prompt's ALLOW
button came from `uiautomator` (`[133,1207][947,1354]`) and was exact, versus guessing.

**The IME re-lays the Flutter form.** A tap target measured with the keyboard closed misses
once it opens. `KEYCODE_ESCAPE` does not dismiss the soft keyboard; a neutral tap does.

**`am force-stop` does not log you out.** The app persists its session, so a "cold start"
silently replays a logged-in dashboard and the first scout passed while recording nothing
real. `launch --clear` (`pm clear`) is required for a genuine first launch. Note the server
still remembers account progress, so a first-time journey needs a fresh account.

## Static analysis of routes

**`path: AppRoutes.login` is not a literal.** A scanner matching only quoted strings found
2 of 35 routes in a real app. Identifiers must be resolved against the project's constants.

**Constants must be qualified by their class.** Keyed by member name alone, `AppRoutes.login`
(`/login`) resolved to `ApiEndpoints.login` (`/api/auth/login`) — a confidently wrong route
table. Bare names are only trusted when exactly one class defines them.

**A route belongs to the module that owns its screen widget**, not the file that declares it.
Attributing by declaring file collapsed an `app_router.dart` monolith into one module.

Named routes, query strings and `pushNamed(context, '/x')` argument shapes all needed
handling. Unresolved calls are reported as unresolved rather than hidden.

## Dashboard and serving

**A tunnel reaches a loopback bind fine**, so `--public` must force the access token itself;
relying on the bind to decide left the whole thing wide open.

**The index page leaked session names and package ids without the token** until it was
gated too.

**Browsers play an mp4 rather than saving it** without `Content-Disposition: attachment`.

**Query params built by string concatenation landed in the path** (`...mp4&dl=1?t=x`),
producing 404s on the one link that mattered.

**`trycloudflare` mints a random hostname per process.** Every dashboard restart changed the
user's URL. The tunnel is now its own long-lived process recorded in `tunnel.json` and reused.
It still cannot survive a reboot without a Cloudflare named tunnel or Tailscale.

**A `%` inside the embedded HTML's CSS broke `%`-formatting** and crashed the index handler,
returning an empty body. Placeholders, not `%s`.

**An orphan `)` left by an edit made the page silently unparseable** — it sat on "loading"
with no console error, while `curl` reported 200. `tests/check_page_js.py` bracket-balances
the inline script so this cannot ship again.

## Client security finding

The **release** build logs full HTTP request bodies to logcat:

```
I flutter : [HTTP] body: {email: <user>, password: <plaintext>}
```

That is readable by anyone with a USB cable. Unrelated to this tool, and worth reporting
to the client.

## Machine constraints

8 GB RAM, disk chronically near full. `brew install ffmpeg` is ~600 MB; the pip static build
is ~30 MB and has every filter needed. An emulator is viable here — AVD `demo35` boots in
~40 s at 1080x2400 — but `avdmanager create` defaults `hw.gpu.enabled=no`, which makes it
crawl; host GPU plus 2 GB RAM is what works. A second, lighter AVD (1.5 GB, 2 cores) runs
alongside it for two-device flows.

## Process lesson

Three separate silent failures came from unasserted string replacements during edits — the
patch reports success while changing nothing, and the bug surfaces much later as a mystery.
Every bulk edit here asserts on its anchor, and the page script and self-test exist because
"it printed something" is not evidence.
