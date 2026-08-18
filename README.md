# WhatsApp Printer

A virtual Windows printer. The operator prints a receipt from whatever software
they already use, picks **WhatsApp Printer**, and the customer receives it on
WhatsApp. A small panel appears in the corner to say whether it went. Nothing
else to click.

```
Chit fund software  --Print-->  WhatsApp Printer  -->  PDF  -->  read the page  -->  send  -->  popup
```

No changes to their existing software, and no integration with it.

---

## Three decisions that shaped this

**1. We ship no print driver.** Microsoft is retiring third-party V3/V4 printer
drivers — new ones stopped reaching Windows Update in January 2026, the inbox IPP
driver became preferred in July 2026, and Windows Protected Print mode uninstalls
queues built on third-party drivers outright. So the queue uses Microsoft's own
inbox *Microsoft Print To PDF* driver bound to a Local Port whose name is a file
path. Windows writes each job straight to disk as a PDF, silently. No signing
certificate, no WHQL, nothing for WPP to remove.

**2. One application, no browser, no Windows service.** A service runs in session
0 and has no desktop, so it cannot show a window at all. Everything is a single
`waprinter-agent.exe` that starts at logon in the user's own session. Its windows
are drawn by WebView2 — the engine already built into Windows 10 and 11 — so the
panel is an application window, not a browser tab pointed at localhost.

**3. The official WhatsApp Business API, and only that.** An earlier build also
supported WhatsApp Web through Baileys — free, no template approval, fully
editable message text. It was removed: it is unofficial, Meta bans numbers that
use it, and the number at risk is the client's main business line. Meta's own
utility rate is about ₹0.115 per message, which is not worth a ban.

The consequence is that message wording is fixed by an approved template.
Variables (`{{1}}`, `{{2}}`) come from the printed page and can be remapped
instantly; changing the sentence itself means submitting a new template.

---

## What the operator sees

**On a normal print: a small notification in the corner, and that is all.** Green
if it went, naming the document and who received it. It closes itself after a few
seconds.

Failures and anything needing a decision do **not** auto-close — a receipt that
did not arrive has to be noticed. Those offer "Open queue" and stay until
dismissed.

**The control panel** is an application window laid out like a printer's
properties page rather than a dashboard. (It is served over loopback behind the
scenes, which is an implementation detail: nothing shows a URL.)

- A **device status line** across the top — Ready / Not ready / Test mode — plus
  a **Test send** button, the equivalent of "Print Test Page"
- **Status** — today's counts, anything blocking sending, and how to use the printer
- **Needs attention** — only documents whose recipient could not be read
- **Recent** — what went where, in plain words
- **Settings** — grouped fields with Apply

---

## Reading the page

Extraction is configuration, not code — see
[`extract/profile.py`](src/waprinter/extract/profile.py). Label lists (what a
client calls things) plus pattern lists (for unlabelled identifiers). The
built-in defaults read both a GST tax invoice and a chit fund receipt; a
`profile.json` overrides key by key, so a new client is configuration rather than
a release.

The recipient is scored, not guessed. A label anchor (`Mobile:`) and position in
the customer block earn points; the page footer and letterhead lose them, because
that is where the *seller's* number lives. Anything anchored to GSTIN, `A/c No`,
`Invoice No` is rejected, as are STD-code landlines — `080-25551234` normalises
into something that passes every mobile test otherwise. Only a single
high-confidence candidate is sent to automatically; everything else waits in the
queue.

### Scanned documents

Pages with no text layer go through Tesseract via PyMuPDF, which returns words in
**PDF coordinates** so all the geometry scoring keeps working. OCR gets a check
the text path does not need: every scanned page is read **twice at different
resolutions**, and anything that does not come back identically is demoted and
held. Measured against deliberately poor scans:

| Scan | OCR read | Outcome |
|---|---|---|
| 60 dpi | `+9198765`**`4`**`9210` — a 3 read as 9 | caught, held |
| 80 dpi | a different number entirely | caught, held |
| 110 dpi+ | correct | confirmed by both reads |

`ocr_silent_send` is off by default, so even a double-confirmed OCR number waits
for a person.

---

## Building the installer

**Via GitHub Actions** — push, and
[`build-windows.yml`](.github/workflows/build-windows.yml) builds on a Windows
runner. Download `WhatsAppPrinter-Setup` from the run's Artifacts, or push a `v*`
tag for a Release.

**On Windows**, with Python 3.12, Inno Setup and Tesseract:

```powershell
powershell -ExecutionPolicy Bypass -File installer\build.ps1
```

The build **runs what it produces** (`waprinter.exe --help`,
`waprinter-agent.exe --selftest`) and fails if either does not start. That check
exists because a build once shipped an executable that died instantly on a
relative import, invisibly, because it is frozen `--windowed`.

## Developing

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

```bash
.venv/bin/python -m waprinter.cli process receipt.pdf
```

Test mode is **on** by default: the pipeline runs and every decision is recorded
to `logs/dry_run.jsonl`, but nothing is sent.

---

## Layout

| Path | What it does |
|---|---|
| [`agent.py`](src/waprinter/agent.py) | The one process: GUI loop on the main thread, watcher and server on their own |
| [`ui/window.py`](src/waprinter/ui/window.py) | Every window in the application |
| [`capture/watcher.py`](src/waprinter/capture/watcher.py) | Drains the spool folder; waits for `%%EOF` before claiming a file |
| [`extract/profile.py`](src/waprinter/extract/profile.py) | Per-client document vocabulary |
| [`extract/phone.py`](src/waprinter/extract/phone.py) | Number parsing and scoring, shared by the page reader and typed input |
| [`extract/ocr.py`](src/waprinter/extract/ocr.py) | Tesseract discovery and page OCR |
| [`rules/gate.py`](src/waprinter/rules/gate.py) | Send / confirm / hold / duplicate |
| [`send/whatsapp.py`](src/waprinter/send/whatsapp.py) | Meta Cloud API: upload media, send template |
| [`send/readiness.py`](src/waprinter/send/readiness.py) | One definition of "ready to send" |
| [`ui/result.py`](src/waprinter/ui/result.py) | What the after-print notification says |
| [`ui/app.py`](src/waprinter/ui/app.py) | The control panel |
| [`installer/provision.ps1`](installer/provision.ps1) | Creates the printer and its ports |

---

## Not built yet

- **Delivery receipts.** Meta reports delivered/read/failed by webhook, which
  needs a public HTTPS endpoint an on-premise agent does not have. The panel says
  so rather than implying it knows.
- **Retry with backoff** — failures are classified as retryable or not, but
  retries are manual from the queue.
- **Code signing** — every client install currently shows "Windows protected
  your PC".
- **Amount extraction for chit receipts** — several competing figures on the page
  and no "Total" label, so it is deliberately left blank rather than guessed.
