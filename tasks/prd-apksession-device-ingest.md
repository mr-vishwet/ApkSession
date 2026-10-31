# PRD: ApkSession device ingest — resumable 4K transfer over a constrained link

## Introduction

ApkSession today produces video by **screen-recording** an app on a device. That cannot capture what
Cardboard actually cares about: real footage sitting in the phone's library — 4K, HEVC, multi-GB —
moving off the device and onto a server while the app is backgrounded, killed, or on a link that
drops.

This feature adds a second ingest leg to ApkSession: **pull clips from the device and transfer them
resumably**, reusing the infrastructure that already exists and is already trusted — the session
folder contract, the fixed local dashboard on `127.0.0.1:8734`, the Cloudflare quick-tunnel process,
and the access-token gate.

The deliverable is not just working code. It is a **measured report** of what the transfer costs and
where it breaks, written in the same style as `NOTES.md`. ApkSession's credibility comes from having
measured that `screenrecord` drops idle frames and that its own edge-energy check saturates. This
leg has to produce findings of that kind, or it is just another uploader.

## Goals

- Move a >1 GB 4K/HEVC clip from an Android device to the existing Python server without a single
  full-file re-transfer when the link fails mid-stream.
- Survive, and prove survival of: app process killed mid-upload, network dropped mid-upload, server
  restart mid-upload.
- Continue transferring with the app in the background, not only while a UI is visible.
- Verify the reassembled file is byte-identical to the source.
- Show real per-chunk progress, not a fake percentage.
- Produce `INGEST_NOTES.md` with measured numbers, including at least one finding that contradicts a
  reasonable assumption.
- Keep every existing ApkSession guarantee: nothing outside the session folder is written, the
  token gate is never bypassed, and the de-branding human-read gate is untouched.

## Non-Goals

- No transcoding, no codec work, no HDR grading, no thumbnail pipeline. This is a transport problem.
- No cloud storage SDKs (S3 multipart, GCS resumable). Using one would hide the exact mechanics this
  is meant to demonstrate. A later variant may compare against one — that comparison is a finding,
  not a shortcut.
- No auth system beyond the existing access token.
- No multi-user queue, no persistence layer, no database. Session folders are the store.
- No iOS work in this PRD. It is a deliberate follow-up (see Open Questions).
- No change to the recording, masking, proof, sweep, render or export stages.

## User Stories

### US-001: Chunked upload endpoint on the existing server
**Description:** As the pipeline, I need an endpoint that accepts numbered chunks and assembles them,
so that a transfer can stop and start without losing work.

**Acceptance Criteria:**
- [ ] `POST /ingest/<session>/init` accepts `{filename, size, sha256, chunk_size}` and returns an
      `upload_id`; a second init for the same file returns the existing `upload_id` (idempotent).
- [ ] `PUT /ingest/<session>/<upload_id>/chunk/<n>` stores one chunk, returns 200 on accept, and
      returns 200 (not 4xx) when re-sent an already-stored chunk.
- [ ] `GET /ingest/<session>/<upload_id>/status` returns the set of chunk indices already stored, so
      a client can resume without guessing.
- [ ] `POST /ingest/<session>/<upload_id>/complete` assembles in index order and 409s if any index is
      missing, naming the missing ranges.
- [ ] Reassembly verifies SHA-256 against the init value and fails loudly on mismatch.
- [ ] Rejects `filename` containing path separators or `..`; tests assert a traversal attempt is
      refused.
- [ ] Chunk files land only inside the session folder; `.gitignore` covers them.
- [ ] `tool/tests/run.sh` still prints PASS, and the new endpoint tests run inside it with no device
      attached.

### US-002: Chunk reader with checkpoint state
**Description:** As the uploader, I need to read a bounded slice of a large file and remember where I
got to, so that resume is a lookup rather than a re-scan.

**Acceptance Criteria:**
- [ ] Reads chunk *n* via random access (seek + fixed-size read); peak memory stays under 16 MB with
      a 4 MB chunk size, asserted by a test that reads a 2 GB sparse file.
- [ ] Checkpoint record holds `upload_id`, chunk size, next index, source size, source sha256, and a
      device-side generation id.
- [ ] Checkpoint is written **after** the server acks that chunk, never before. A test asserts that a
      crash between read and ack replays that one chunk rather than skipping it.
- [ ] Checkpoint write is atomic (temp + rename) so a kill mid-write cannot corrupt it.
- [ ] Unit tests pass on the host JVM with no device.

### US-003: Kotlin ingest module — picker and MediaStore read
**Description:** As a user, I want to pick real videos from the device so I am testing against actual
camera files rather than fixtures I generated myself.

**Acceptance Criteria:**
- [ ] Photo picker (Android 13+) with `READ_MEDIA_VIDEO` fallback for lower API levels; no all-files
      permission requested.
- [ ] Resolves the picked item to a readable file descriptor and reports codec, resolution, bitrate
      and byte size from `MediaMetadataRetriever`.
- [ ] Handles a content URI, not just a real path — a test uses a `content://` URI.
- [ ] Lists the picked clip's properties in the UI before any transfer starts, so a 4K HEVC file is
      visibly identifiable.
- [ ] `./gradlew lint` and `./gradlew test` pass.
- [ ] Verify on the `demo35` AVD and state in the commit which device was used.

### US-004: Background transfer that survives the app going away
**Description:** As a user, I want the upload to keep going when I leave the app, because a
multi-gigabyte clip is never foreground time.

**Acceptance Criteria:**
- [ ] Transfer driven by WorkManager with a `networkType` constraint, one worker per chunk-batch so a
      failure retries at chunk granularity, not job granularity.
- [ ] `am force-stop` mid-upload, then relaunch: transfer resumes from the last acked chunk and
      completes. Recorded as a test, not a manual claim.
- [ ] Reboot the device mid-upload: resumes correctly afterwards. If WorkManager does not restore the
      enqueue, the module re-enqueues from the persisted checkpoint on app start, and the test proves
      it.
- [ ] Foreground service only if actually required; if used, correct `foregroundServiceType` and
      Android 14 permission handling. State plainly in the commit whether it was needed.
- [ ] No busy-wait, no polling loop faster than 1 s; worker completes and does not leave a resident
      notification.

### US-005: Failure handling on a link that drops
**Description:** As a user on bad network, I want the transfer to back off and recover rather than
restart or silently stall.

**Acceptance Criteria:**
- [ ] Exponential backoff with jitter, capped, and a bounded attempt budget that surfaces a terminal
      failed state instead of retrying forever. (ApkSession's own notes flag "reconnect with no
      backoff" as a known defect elsewhere — do not repeat it here.)
- [ ] Airplane mode toggled mid-upload on the AVD: transfer pauses, and on restore resumes from the
      last acked chunk with zero duplicate assembled bytes.
- [ ] Server killed and restarted mid-upload: client recovers via `status`, not by assuming.
- [ ] HTTP 5xx and connection-reset paths each covered by a test using a local fault-injecting stub.
- [ ] A 416/already-have-this-chunk response is treated as success, not error.
- [ ] Terminal failure leaves the checkpoint intact so a later attempt can continue.

### US-006: Progress UI that reports the truth
**Description:** As a reviewer, I want to see what has actually been acknowledged so far, so that the
progress bar means something.

**Acceptance Criteria:**
- [ ] Progress derives from **server-acked** chunk count, never bytes handed to the socket.
- [ ] Shows MB transferred, MB total, current throughput, and a rolling ETA.
- [ ] Throughput is a moving average over the last N acks, not an instantaneous number; a test feeds a
      synthetic ack sequence and asserts the displayed value.
- [ ] Re-resume after failure shows the correct starting value rather than jumping to 0%.
- [ ] Handles a 0-byte and a single-chunk file without division-by-zero or NaN ETA.
- [ ] Verify on `demo35` with a real 4K clip and attach the screenshot to the session folder.

### US-007: Integrity and post-transfer playback
**Description:** As the pipeline, I need to know the file on the server is the file that was on the
phone, and that it still plays.

**Acceptance Criteria:**
- [ ] Server-side SHA-256 of the assembled file matches the value sent at init; mismatch is a hard
      failure that keeps chunks for diagnosis.
- [ ] Client computes source hash without loading the file whole (streaming digest), and that this
      does not add a second full read of the file — measure and report the cost.
- [ ] Assembled file is registered so the existing dashboard can serve it, and plays back through the
      existing masked/unmasked video path.
- [ ] A corrupted-chunk test (flip one byte before sending) is detected at `complete`, not silently
      assembled.
- [ ] Existing `proof`/`sweep` stages are unaffected; `tool/tests/run.sh` PASS.

### US-008: Bandwidth-adaptive serve and the measured report
**Description:** As the author, I want the numbers this leg produced written down, including the ones
that surprised me, because that is the part that is hard to fake.

**Acceptance Criteria:**
- [ ] `INGEST_NOTES.md` in the ApkSession root, same voice and evidence discipline as `NOTES.md`.
- [ ] Reports, with measured numbers: throughput by chunk size (at least 1 / 4 / 8 MB) on the tunnel,
      resume cost as a function of file size, and the real time to move a 1 GB clip over the quick
      tunnel versus plain loopback.
- [ ] Contains **at least one finding that contradicts an assumption held before the run**, stated as
      such. If nothing was contradicted, say so and say what was expected.
- [ ] Documents the tunnel's actual behaviour under a long sustained PUT, including whether the
      quick tunnel drops it, and what the client did about it.
- [ ] If adaptive chunk sizing is implemented, the rule that drives it is written down and its
      measured benefit is reported; if it is not implemented, that is stated rather than implied.
- [ ] Explicitly records what was **not** tested (iOS, real cellular network, HEVC-specific handling)
      so the document cannot be read as stronger than it is.

## Functional Requirements

- FR-1: The server must accept idempotent chunk writes and expose which chunks it already holds.
- FR-2: The client must persist a checkpoint that is advanced only on server acknowledgement.
- FR-3: The client must transfer via WorkManager and must resume after process kill, app kill, server
  restart, and network loss.
- FR-4: The system must verify end-to-end SHA-256 and must fail rather than assemble a corrupt file.
- FR-5: The UI must show ack-derived progress, throughput and ETA.
- FR-6: All artifacts must remain inside the session folder and must obey the existing token gate.
- FR-7: The pipeline must emit `INGEST_NOTES.md` containing measured results.
- FR-8: No existing ApkSession stage may change behaviour; `tool/tests/run.sh` must stay green.

## Design Considerations

- Reuse the existing dashboard server for ingest endpoints — do not start a second service.
- Reuse the session-folder-per-recording contract; ingest artifacts live beside `render/` and
  `proof/`, in a new `ingest/` subfolder.
- The Android module should follow the Kanan pattern (Jetpack Compose + Material 3, Hilt, Room only
  if genuinely needed — prefer plain files for checkpoints).
- No new design system. This is an internal tool; a clean single screen is correct.

## Technical Considerations

- Host is an M1 MacBook Air, 8 GB RAM, no CUDA, disk chronically near full. Do not install a large
  toolchain to solve this; the existing Python venv and `demo35` AVD must be enough.
- `imageio-ffmpeg` provides ffmpeg but **no ffprobe** — any media inspection uses
  `MediaMetadataRetriever` on-device or `ffmpeg -i` stderr parsing on the host.
- Emulator storage is limited; test the resume logic with sparse files and reserve the real-clip test
  for one run.
- `trycloudflare` mints a random hostname per process and the tunnel is a separate long-lived
  process — the client must not assume a stable URL across a dashboard restart.
- A `%` inside embedded dashboard HTML breaks `%`-formatting; use placeholders. (Existing known trap.)
- Never use `SIGKILL` on a device-side recorder or transfer — it corrupts in-flight files.

## Success Metrics

- A 1 GB clip transfers over the tunnel with a forced mid-stream failure and completes with **zero**
  re-sent acked chunks.
- All four failure modes (app kill, process kill, network drop, server restart) have an automated or
  scripted test that fails if resume breaks.
- Peak client memory during transfer stays under 16 MB regardless of file size.
- `INGEST_NOTES.md` contains at least three numbers that did not exist before the work.
- The whole leg is demoable in under 5 minutes for an interview: pick, upload, kill, resume, play.

## Open Questions

- iOS equivalent: `URLSessionBackgroundDelegates` on a separate branch, or hold until the Android leg
  is measured? Recommend a separate follow-up PRD rather than widening this one.
- Should the ingest leg be usable against a real client device over cellular, or is AVD + tunnel
  sufficient? AVD is sufficient for the demo; a real-device run would make the report stronger.
- Is there a defensible case for exposing the assembled clips through the existing masked-video path,
  or does that mix de-branding guarantees with unrelated ingest artifacts? Default: keep them
  separate paths in the dashboard.
