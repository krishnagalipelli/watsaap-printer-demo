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

**2. One application, native widgets, no service.** A service runs in session 0
and has no desktop, so it cannot show a window at all. Everything is a single
`waprinter-agent.exe` that starts at logon in the user's own session.

The interface is Tk, which ships with Python. No web server, no template engine,
no browser runtime — the whole dependency list is PyMuPDF, httpx and watchdog.
An earlier build rendered the panel in WebView2; native widgets removed a runtime
dependency, four packages and about 36 MB, and look more like the printer
properties sheet they are modelled on.

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
properties sheet rather than a dashboard.

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

### A print job is not always one receipt

The counter prints a run of receipts as a single job: one PDF, a different
subscriber on every page. That has to become one job per subscriber, and not
only because a batch would otherwise sit in the queue forever — two subscribers
means two numbers scoring above the send threshold, and the gate is right to
refuse to choose. The real reason is the attachment. The send path uploads the
job's PDF whole, so resolving the hold by picking one of the offered numbers
would post that subscriber a document carrying everyone else's name, mobile and
amount paid.

The boundary is **a change of document number, not a page break**. A receipt
that runs onto a second page keeps it, because the continuation carries no
number of its own. A subscriber copy followed by an office copy of the same
receipt stays one job, because the number on both is the same. Only a page
naming a *different* document starts a new one.

Splitting reads the page without OCR: a scanned batch would have to be rendered
twice over, and a scan is held for a person anyway. A document that cannot be
split at all is processed whole rather than dropped.

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

**The title is read twice as well, for a stronger reason than the number.** A
misread number sends the right words to the wrong person, who can see the
message was not meant for them. A misread title sends the wrong words to the
right person — a member being removed, thanked for a payment — and nothing
about it looks wrong. So the two passes must agree on what the document is,
and a disagreement is held whatever `ocr_silent_send` says.

Agreeing on *nothing*, though, is not agreement. Two passes that both failed to
find a title say only that neither could read one, and an unrecognised document
falls back to `default_template` — the receipt. That fallback is right on an
install that only sends receipts, and on one that also sends removal notices it
is exactly how a notice goes out thanking someone for a payment. So where
`document_templates` maps anything, an unidentified scan is held rather than
assumed. Receipts are recognised positively for this: the chit fund's own
software prints only the filled-in fields, so `Received from` never reaches the
PDF's text layer — but it is printed on the paper, so a *scan* of a receipt
carries it, which is precisely where the positive identification is needed.

---

## More than one kind of paperwork

The same queue carries chit receipts, **removal notices** and **removal
letters**. They are different documents, to different people, saying different
things — one warns a member they are about to be removed, the other confirms
they have been — so each goes out under its own approved template. A member
sent "Thank you for your payment" over a removal notice would be worse off
than one sent nothing at all.

The kind is read from the title, which is the one thing these layouts do not
share, and it decides three things:

| | Receipt | Removal notice | Removal letter |
|---|---|---|---|
| Template | `chit_receipt` | `removal_notice` | `removal_letter` |
| Attached as | `Receipt-CR1747-26.pdf` | `Removal Notice-RN317-26.pdf` | `Removal Letter-RL151-26.pdf` |
| Name read from | `Received from` | `To,` | `To :` |

The filename matters as much as the wording: the member reads it before they
open anything, which is why a notice must not arrive called `Receipt-`.

Each kind is a `DocumentKind` in [`extract/profile.py`](src/waprinter/extract/profile.py)
— a title phrase and whatever anchors that layout needs — so a fourth document
is configuration, not a release. Anything the profile does not recognise is
read exactly as it always was and uses `default_template`, which is every
receipt.

Two things that layout taught us, both now regression-tested. `subscriber` is
a customer anchor, and it matched inside *"...removed from the list of
subscribers in terms of the provisions of the Chit Fund Act"*, so a notice
greeted the member with that sentence. And `Dear Sir/ Madam,` is printed
between the `To,` heading and the member's own name, which greeted them
`Dear Dear Sir/ Madam,`.

## Keeping the printed receipts

The office needs its own record of what it printed, and for a while there was
none worth having: captured PDFs stayed in `inbox` under the name capture gave
them — `20260907-113601-fee7f6b4.pdf` — flat, forever, inside ProgramData. Not
a folder anyone browses and not a name anyone can search.

Every processed receipt is now copied into a folder the operator picks on the
Settings tab, filed by the date it was printed and named after itself:

```
D:\Receipts\2026-09-07\CHQ6511-26 SHAHNAVAZDANISH MOHAMMAD.pdf
```

A **copy**, not a move. The file under `inbox` is what the queue reopens, what
"View PDF" shows and what a held receipt is eventually sent from; if that
pointed at a share that went offline, sending would break with it. Filing is
also wrapped so it can never fail a print — a receipt that reached the customer
but not the folder is a nuisance, the other way round is a lost receipt.

Which is also why `waprinter doctor` reports the folder and tries to write to
it. A drive letter that stopped being mapped loses copies quietly, exactly
because filing is not allowed to complain loudly.

## Installing 20+ machines

Put `provision.json` next to `WhatsAppPrinter-Setup.exe`, run setup, walk away.
Setup copies the file into `C:\ProgramData\WAPrinter`; the agent reads it on
first start, seals the token with DPAPI, writes the rest into settings and
deletes the file. Nothing is typed at the counter.

```json
{ "access_token":    "EAAG...",
  "phone_number_id": "123456789012345",
  "send_mode":       "api",
  "dry_run":         false,
  "pdf_folder":      "D:\\Receipts",
  "branch_name":     "Karimnagar" }
```

Every key is optional and anything left out keeps its current value, so the
same mechanism reconfigures a machine that is already running — a rotated
token, a new template, a different receipts folder — pushed over AnyDesk with:

```
waprinter provision \\share\it\provision.json
```

See [`provision.example.json`](provision.example.json) for the full set.

**The token is not compiled into the build, and must not be.** This repository
is public and the installer is published on GitHub Releases, so a token in the
source is a token on the internet: frozen Python unpacks with `strings`, GitHub
and Meta both scan for the pattern and revoke, and until they do, anyone can
send as the business. The number that would be suspended is the client's main
line — the risk that ruled out Baileys in the first place. So the secret
travels beside the installer on media the engineer controls, and Inno Setup
copies it with `external` rather than compiling it in.

A file that cannot be parsed is left in place and reported rather than silently
deleted, because it still holds the token; one that is applied is overwritten
before it is unlinked.

## Updating 20+ machines

Each installation checks a small JSON file over HTTPS — hosted on GitHub
Releases or any static host — and installs a newer build silently. There is no
server of ours involved; if the file is unreachable, printing and sending carry
on untouched.

Two triggers:

- **Daily**, in the background, for the routine version bump.
- **Check for updates**, on the Status tab. A fix released at eleven in the
  morning should not wait for a timer, and this is how it reaches a branch the
  moment it exists — including over AnyDesk.

Guard rails: the download is SHA-256 verified against the manifest before it is
executed, and an update never installs while a document is being processed.
The database is brought up to the current shape when it is opened, because
`CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists — a
column added in a release once never reached a single installed machine, and
every print failed on the missing column the next morning.

```json
{ "version": "1.1.0",
  "url": "https://.../WhatsAppPrinter-Setup-1.1.0.exe",
  "sha256": "…",
  "notes": "Fixes the receipt-number pattern for branch 4." }
```

Publishing a release is what makes it visible — pushing code does not. That tag
is the switch, and rolling back means publishing the older version number.

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
| [`ui/desktop.py`](src/waprinter/ui/desktop.py) | The application window |
| [`ui/viewmodel.py`](src/waprinter/ui/viewmodel.py) | What it says, testable without a display |
| [`ui/notification.py`](src/waprinter/ui/notification.py) | The corner panel after a print |
| [`update.py`](src/waprinter/update.py) | Version check, verified download, silent install |
| [`capture/watcher.py`](src/waprinter/capture/watcher.py) | Drains the spool folder; waits for `%%EOF` before claiming a file |
| [`extract/split.py`](src/waprinter/extract/split.py) | Splits a batch print into one job per receipt |
| [`archive.py`](src/waprinter/archive.py) | Files a copy of every printed receipt where the office can find it |
| [`provision.py`](src/waprinter/provision.py) | Configures an install from one file, then deletes it |
| [`extract/profile.py`](src/waprinter/extract/profile.py) | Per-client document vocabulary |
| [`extract/phone.py`](src/waprinter/extract/phone.py) | Number parsing and scoring, shared by the page reader and typed input |
| [`extract/ocr.py`](src/waprinter/extract/ocr.py) | Tesseract discovery and page OCR |
| [`rules/gate.py`](src/waprinter/rules/gate.py) | Send / confirm / hold / duplicate |
| [`send/whatsapp.py`](src/waprinter/send/whatsapp.py) | Meta Cloud API: upload media, send template |
| [`send/readiness.py`](src/waprinter/send/readiness.py) | One definition of "ready to send" |
| [`ui/result.py`](src/waprinter/ui/result.py) | What the after-print notification says |
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
