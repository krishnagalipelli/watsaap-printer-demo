# WhatsApp Printer

A virtual Windows printer. The operator prints a receipt or statement from
whatever software they already use, picks **WhatsApp Printer** in the print
dialog, a window opens asking for the customer's WhatsApp number, and the PDF is
sent. No changes to their existing software, no integration with it.

```
Chit fund software  --Print-->  WhatsApp Printer  -->  PDF  -->  dialog  -->  WhatsApp
```

First client is a chit fund group. Their documents do not print the member's
phone number, so the number is typed at the point of sending.

---

## Three decisions that shaped this

**1. We ship no print driver.** Microsoft is retiring third-party V3/V4 printer
drivers — new ones stopped reaching Windows Update in January 2026, the inbox IPP
driver became preferred in July 2026, and Windows Protected Print mode uninstalls
queues built on third-party drivers outright. So the queue uses Microsoft's own
inbox *Microsoft Print To PDF* driver bound to a Local Port whose name is a file
path. Windows writes each job straight to disk as a PDF, silently. No signing
certificate, no WHQL, nothing for WPP to remove.

**2. No Windows service.** A service runs in session 0 and has no desktop, so it
physically cannot show the send dialog. Everything runs in one agent that starts
at logon in the user's own session. This also removes a class of file-permission
problems that came with running as LocalSystem.

**3. "Editable message" has two speeds.** WhatsApp permits free-form text only
inside a 24-hour window the *customer* opens. Sending a receipt is
business-initiated, so it must use a provider-approved template.

| Editable instantly | Editable with a delay |
|---|---|
| Which template is used | The template's fixed wording |
| Customer name and the other `{{n}}` variables | (submit for approval, usually under a day) |
| Recipient, attachment filename | |

This is Meta's rule, not the provider's — going through a BSP such as Dove makes
onboarding easier but does not change it.

---

## The send dialog

This is the product, from the operator's point of view. It opens on every print:

- **PDF preview** of page 1, so they can see what they are about to send
- **Customer name**, prefilled from the document where it can be read
- **WhatsApp number**, typed — validated live, with the normalised number echoed
  back (`Will send to +919876543210`) before Send will even enable
- **Message preview**, updating as the name is typed, showing exactly what arrives
- **Send** or **Skip** — skipping leaves the job in the queue for later

The number is validated by the same rules the page scanner uses, deliberately:
anything the extractor refuses to read off a page, an operator cannot type
either. Landlines are the reason. `080-25551234` looks like a valid mobile once
you strip the separators (`8025551234` starts with 8), so the separators are kept
and the grouping is checked.

## The dashboard

On `http://127.0.0.1:8731` — loopback only, no authentication because there is no
network exposure.

- **Dashboard** — today's counts: sent, printed, waiting on you, failed; delivery
  state; recent activity
- **Queue** — anything skipped or still awaiting a number, each with the reason
- **History** — every job, who it went to, and the exact message
- **Settings** — own numbers, provider details, OCR toggles, dry run

---

## Scanned documents (OCR)

Some software renders its output to a bitmap before printing, so the page arrives
with no text layer. Those pages go through Tesseract via PyMuPDF's integrated
OCR — chosen because it returns words in **PDF coordinates**, so the Bill To block
detection, footer and letterhead penalties, and row grouping all keep working on a
scanned page.

OCR gets a check the text path does not need. Tesseract confuses digits, so every
scanned page is read **twice at different resolutions**, and anything that does
not come back identically is demoted and held. Measured against deliberately poor
scans:

| Scan quality | OCR read | Outcome |
|---|---|---|
| 60 dpi | `+9198765`**`4`**`9210` — a 3 read as 9 | caught, held |
| 80 dpi | `+919845012945` — a different number entirely | caught, held |
| 110 dpi+ | correct | confirmed by both reads |

Both wrong numbers are valid-looking Indian mobiles. Neither reached a send path.

---

## Building the installer

**Via GitHub Actions (no Windows machine needed).** Push to GitHub and
[`.github/workflows/build-windows.yml`](.github/workflows/build-windows.yml)
builds it on a Windows runner. Download `WhatsAppPrinter-Setup` from the run's
Artifacts, or push a `v*` tag to get it attached to a Release. Tests run on Linux
first as a cheap gate before the Windows runner starts.

**On a Windows machine**, with Python 3.12, Inno Setup, and Tesseract installed:

```powershell
powershell -ExecutionPolicy Bypass -File installer\build.ps1
```

Both paths produce `installer\Output\WhatsAppPrinter-Setup-0.1.0.exe`, which
installs the agent, creates the printer queue, bundles Tesseract, and adds a
logon entry plus a dashboard shortcut.

## Developing

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

Push a PDF through the pipeline without a Windows machine:

```bash
.venv/bin/python -m waprinter.cli process invoice.pdf
```

Dry run is **on** by default: the pipeline runs and every decision is recorded to
`logs/dry_run.jsonl`, but nothing is sent.

---

## Layout

| Path | What it does |
|---|---|
| [`agent.py`](src/waprinter/agent.py) | The one process: Tk dialogs on the main thread, watcher and web UI on their own |
| [`capture/watcher.py`](src/waprinter/capture/watcher.py) | Drains the spool folder; waits for `%%EOF` before claiming a file |
| [`capture/spooler.py`](src/waprinter/capture/spooler.py) | Recovers job title/user from the PrintService event log (optional) |
| [`extract/pdf_text.py`](src/waprinter/extract/pdf_text.py) | Words, lines, and visual rows with geometry; OCR fallback per page |
| [`extract/ocr.py`](src/waprinter/extract/ocr.py) | Tesseract discovery and page OCR |
| [`extract/phone.py`](src/waprinter/extract/phone.py) | Number parsing and validation, shared by the page scanner and the dialog |
| [`rules/gate.py`](src/waprinter/rules/gate.py) | Confirm / send / hold / duplicate |
| [`send/base.py`](src/waprinter/send/base.py) | The seam a provider adapter slots into |
| [`send/whatsapp.py`](src/waprinter/send/whatsapp.py) | Meta Cloud API implementation |
| [`ui/send_dialog.py`](src/waprinter/ui/send_dialog.py) | The window that opens on print |
| [`ui/app.py`](src/waprinter/ui/app.py) | Dashboard, queue, history, settings |
| [`installer/provision.ps1`](installer/provision.ps1) | Creates the printer and its ports |

Nothing upstream of `send/base.py` knows how messages leave the machine.

---

## Automatic mode

`confirm_before_send = False` skips the dialog and sends to a number detected on
the page. Off by default and not what the current client needs, but supported for
one whose documents *do* print the customer's number. It is the reason the
confidence gate, the own-number blocklist, the reprint dedupe, and the OCR
double-read exist — everything that has to be true before a machine sends someone
else's document unattended. Measure with `waprinter corpus --score` before
enabling it.

---

## Not built yet

- **The Dove Soft adapter.** Blocked on their API docs. One new file implementing
  `Sender`, plus config. The open question is whether they take a media upload or
  require a public HTTPS URL for the PDF — the latter would mean pulling forward a
  cloud relay, since an on-premise agent has no public URL.
- **Delivery receipts.** Needs a public webhook endpoint, same problem. The
  dashboard says so rather than implying it knows.
- **Retry with backoff** for `retryable` send failures — classified correctly, but
  retries are manual from the queue.
- **Nothing has run on Windows yet.** The printer queue script, the frozen build,
  and the installer are written and unverified. That is the next thing to do.
