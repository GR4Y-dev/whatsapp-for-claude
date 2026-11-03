# Security review — 2026-11-03

This is a source and dependency review of this change, based on main commit
`4a504c01eb155986aa17bdefd2578a70a50fa23b`. It is not a penetration test of the live server
or a guarantee against every vulnerability.

## Fixed in this release

- **Disabled-tool bypass:** `HIDE_TOOLS` now covers the gateway's own tool list and direct
  dispatch, not just forwarded upstream tools. Internal upstream calls enforce it too.
  Disabling `send_message` prevents new schedules and pauses queued sends. Existing jobs
  retain the one-hour delivery grace period when sending is re-enabled.
- **Attachment file access:** resolve paths returned by the upstream server, require a
  regular file inside the configured store and reject database files and journal files.
  An upstream response cannot make the gateway open an arbitrary outside file.
- **Dependency vulnerabilities:** replace Pillow 11.3.0 with 13.0.0. The resolved gateway
  requirements passed pip-audit 2.10.1 on this date with no known vulnerabilities (37
  packages on the review machine). CI repeats the audit on Linux. See
  [Pillow's release notes](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html).
- **Attachment resource limits:** reject images above 25 megapixels before decoding,
  cap PDF text extraction at 50 pages per request and bound scanned-page rendering.
  Office ZIPs are limited to 64 MB total, 32 MB per entry and 10,000 entries; spreadsheet
  extraction to 20 sheets, 10,000 rows and 100 columns per sheet.
- **Untested automatic updates:** require a completed, successful `tests` check from
  GitHub Actions for the exact target commit. Missing, skipped, incomplete, failed or
  unverified results cannot install. Move the updater lock into the repository's private
  Git directory to avoid predictable shared `/tmp` lock paths.
- **Pairing page:** add no-store, no-sniff and frame-denial headers alongside no-referrer,
  reducing browser caching and embedding of the secret pairing page.
- **CI:** read-only repository permissions, dependency auditing and dedicated security
  regression tests alongside the existing gateway, updater and Docker build checks.

## Validation and reproducibility

```bash
pip install -r gateway/requirements.txt
python tests/test_gateway.py
python -m unittest discover -s tests -p test_security.py -v
bash tests/test_update.sh
pip-audit -r gateway/requirements.txt
bandit -r gateway
```

The gateway tests use a fake local WhatsApp server. They exercise attachments, storage
pruning, scheduled sends, disabled tools, secret routes and upstream restarts. No real
WhatsApp messages are sent. New regressions cover own-tool and scheduling bypasses, outside
paths/database files, oversized attachments and pairing response headers.

Bandit 1.9.4 reported no high-severity findings. Remaining findings concern best-effort
fallback handling and binding to `0.0.0.0` inside the Docker network. The supplied Compose
file publishes no host ports. Do not expose the gateway or upstream container directly.

## Remaining limits and deployment

- The connector/pairing URL is a bearer credential. Anyone who obtains it can use enabled
  tools. Rotate it after disclosure. Claude's instructions to ask before sending are not
  a server-enforced human approval system. Use `HIDE_TOOLS` for capabilities never needed.
- The upstream WhatsApp server remains trusted: it shares login/message storage and can
  access the account. Path validation is additional protection, not isolation from a
  compromised upstream service or another process running as the same user.
- Stored schedules, media, account sessions and message databases are sensitive plaintext.
  Native attachment readers still process untrusted data; resource caps reduce exposure
  without proving every malformed file is harmless.
- This review covers application source and Python requirements, not the external Go
  WhatsApp server's dependencies, host firewall, Cloudflare account, container base images
  or native system libraries. Some dependencies/tags are not locked. Keep rerunning audits.
- New saved-fact and recurring-reminder features are in the Telegram assistant; this
  repository keeps its existing WhatsApp scheduling behavior.
- Deployment was not verified. After merging and updating, confirm pairing access, one
  ordinary attachment and your disabled-tool settings. Review queued sends before
  re-enabling a previously disabled `send_message` tool.

The running service keeps its current behavior until the change is merged and installed.

