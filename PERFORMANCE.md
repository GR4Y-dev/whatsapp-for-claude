# Smaller, safer updates

The updater builds an image only when that service's tracked build inputs or image ID
change, or the image is missing. The first update records a baseline and builds once.
After that, a gateway change leaves the WhatsApp bridge alone. `--now` uses the same
shared lock and refuses local edits, just like automatic updates. GitHub's successful
test gate and failed-install code rollback are kept.

Build state lives in `.git/connector-builds.json`. It contains only source fingerprints
and image IDs. Compose configuration (which includes secrets) is never saved or logged.
Updates no longer run global image or builder cleanup. This preserves useful cache and
does not remove images belonging to another application. Check `docker system df`
periodically. If a build runs out of disk, free space deliberately before retrying;
the updater does not delete other applications' data to make room.

The bridge is still upstream v0.6.0, pinned to commit
`078ac8c5e3f8a6f32c6a5902e4859f86c853330f`. Upstream uses ffmpeg only to convert
uploaded audio to Opus. Its new image builds ffmpeg from Debian's signed source package,
with native decoders and the Opus encoder, without graphics libraries, device capture,
or network input. Audio inside a video file is extracted as audio. No unverified
prebuilt binary is downloaded. CI converts WAV, MP3, AAC/M4A, FLAC and Ogg to Opus.
The gateway keeps its slim Python base and no pip cache. Build contexts explicitly
exclude credentials, local files and account data.

To pick up base-image/security-package updates even when source is unchanged, explicitly
run `docker compose build --pull` during maintenance, then `bash update.sh --now`.
For rollback, the old image tag remains available; no automatic image pruning occurs.
The updater rolls code back on failure; it does not claim to atomically roll back all
already-started containers. Review the failed update log before retrying.

## Measurements and remaining server checks

The before figures below were supplied from the running server. This development
environment has no running Docker daemon or access to that server. Image size and
Linux resource measurements must be collected from CI/server; estimates are not results.

| Metric | Before | After / verification |
|---|---|---|
| Gateway idle RSS | about 65 MB | unchanged code; remeasure on server |
| Bridge idle RSS | about 32 MB | same Go source; remeasure on server |
| Telegram in-call peak / latency | separate Telegram service | unaffected by this PR |
| Voice models / caller venv | separate Telegram service | unaffected by this PR |
| ffmpeg package footprint | about 484 MB incl. graphics/LLVM | native audio build; CI reports whole image bytes |
| Bridge/gateway image sizes | server measurement needed | `docker image inspect --format '{{.Size}}' IMAGE` |
| Unchanged update builds | bridge + gateway every time | zero; source/image regression tests |

Check `docker system df`, image byte sizes, `ps` RSS and real WhatsApp voice-message
conversion before and after deployment. The local/unit tests do not log in to WhatsApp.

Optional journal cap: an administrator can create a journald drop-in containing
`[Journal]` and `SystemMaxUse=200M`, then restart journald. This is a system-wide
retention choice, so setup and updates never apply it automatically.

Source and licensing: the bridge is [upstream mcp-whatsapp](https://github.com/Sealjay/mcp-whatsapp/tree/078ac8c5e3f8a6f32c6a5902e4859f86c853330f).
FFmpeg source and Debian patches come from the configured Bookworm `deb-src`
repositories; retain the matching source package when distributing an image.
FFmpeg's [license and source obligations](https://ffmpeg.org/legal.html) still apply.
