# WhatsApp Printer

A virtual Windows printer. The operator prints a receipt from whatever software
they already use, picks **WhatsApp Printer**, and the customer receives it on
WhatsApp. A small panel appears in the corner to say whether it went. Nothing
else to click.

```
Chit fund software  --Print-->  WhatsApp Printer  -->  PDF  -->  read the page  -->  send  -->  popup
```

Windows 10/11 use the virtual printer. On Windows 7 SP1, setup enables PDF
folder capture instead: export a PDF from the billing software into
`C:\ProgramData\WAPrinter\spool`, using a unique filename for each document.
The **WhatsApp PDF folder** desktop shortcut opens it. The running app reads and
moves the PDF automatically. Windows 7 does not gain a virtual printer; the
billing software must support PDF export (or already have a compatible PDF
printer installed).

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
no browser runtime — the whole dependency list is PyMuPDF and httpx.
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

**The control panel** is one window: a side bar on the left and a page beside
it, ink on paper with a single mint accent. The counter clerk has three pages
and nothing that changes what members receive.

- A **header** with the printer's state as a badge — Ready / Not ready / Test
  mode — and a **Check configuration** button that checks setup without
  sending a message. The side bar's **Current mode** card says the same in a
  line.
- **Status** — today's counts, a card for each setup step still blocking
  sending (each with a link straight to that step), the last five documents,
  and **Check for updates**
- **Needs attention** — documents waiting for review and failed sends, with View
  PDF, Retry and Discard actions; Show more makes older jobs accessible. The
  setup checklist is repeated underneath, so a clerk who came here because
  nothing is sending finds out why.
- **Recent documents** — what went where, in plain words

Everything else is under **Setup** and **Message templates** in the side bar,
which swap in the setup screens in the same window. A PIN can be set so a clerk
does not change a member's message by accident; it keeps honest people out and
is not a password.

## Setting up a counter

Setup is seven steps down the left-hand side, each ticked when it is done:

1. **Connect WhatsApp.** Paste the access token and press **Look up**. The token
   is inspected with Meta: the WhatsApp Business Account and its phone numbers
   are read from it and shown in words (`+91 87822 51999 · Srinidhi Chit Funds`),
   so there are no IDs to copy. The token's expiry is shown too, and a
   temporary token — the one on the API Setup page, which dies within a day —
   is called out. Choose the number and **Save and load templates**. A token
   that does not name its account asks for the account ID once.
2. **Message templates.** Everything on the WhatsApp account: status, language,
   whether it can attach a PDF, and what it fills in, with Meta's example values
   shown in the wording. One template name in two languages is two templates.
   A template deleted in the Business Manager is marked *not on WhatsApp*
   rather than staying approved here.
3. **Document types.** Receipts need positive evidence. Removal notices and letters are
   built in. **Teach a new type from a PDF** opens the sample: click each value
   that matters, say what it is — a built-in field, the customer's mobile, or
   anything new such as *Customer ID* — and the label printed beside or above it
   is recorded, not its position. Every field shows what it reads *on the sample*,
   so a rule that would read the wrong thing is caught before saving. The title
   that identifies the new type is suggested, and refused if it also appears on
   another type's sample. **Try on recent prints** reads the last thirty prints
   with the new fields without saving or sending anything, including OCR.
   **Review validation PDFs** lets you compare at least three different PDFs
   against their expected field values and customer mobiles. Saving validates
   those answers and stored samples from other types. Fix mismatches before
   saving. Reviewed samples stay locally in `samples/validation`; the export
   setup file does not contain customer PDFs. Cached OCR is reused only while
   the PDF, extraction settings and rules remain unchanged.
4. **Fill in messages.** For each document type: the template, the PDF name the
   member sees, and what fills each `{{variable}}`. Each variable is a drop-down
   of the fields that type has — including taught ones — plus fixed text or
   deliberately blank. A first guess is made from the names (`{{id}}` is offered
   *Customer id*, `{{receipt_no}}` the document number), each row shows what the
   latest print of that type actually reads, and the preview is the message a
   member would receive. A template cannot be saved with a variable unfilled.
5. **Try it.** Check one PDF, or re-read the last thirty prints, with the saved
   setup. Nothing is sent or queued.
6. **Counter settings.** Sending behaviour, scanned pages, where printed
   receipts are kept, and this computer's branch and name.
7. **Share setup.** Export and import, and the setup PIN.

A template mapped on step 4 is strict: if a print is missing a mapped value,
the document is held with the reason ("Could not read customer id…") instead of
sending "-". Templates never mapped there keep the shared `template_variables`
behaviour every install has always had.

The taught fields are data in `profile.json` — `custom_kinds` and
`field_rules` — next to the built-in vocabulary, and the per-template mapping is
`template_mappings` in settings.json. The customer's mobile is never a mappable
value: teaching where it sits only makes that label count as a phone label for
the scorer, and the own-number blocklist, the double OCR read and the
ambiguity hold still decide who is sent to.

### Repeat the setup on another counter

Use **Setup → Share setup → Export setup** on the configured counter, or
`waprinter export-setup FILE`. The exported `provision.json` carries the
account IDs, message choices and mappings, cached template definitions, taught
document types and fields, and the setup PIN. It excludes the access token and
counter-specific branch, computer and archive-folder values, and starts the
destination in test mode.

Put the file beside the installer for automatic import, select it on the installer's
optional **Counter setup** page, or use **Share setup → Import setup** afterwards.
Connect WhatsApp on the new counter and check one sample PDF of each kind before
turning test mode off. Importing through the app preserves the reusable source
file. The installer reports printer-creation failures instead of silently
finishing with no working queue.

Over AnyDesk, the same connection is one command: `waprinter connect` reads the
token (paste with a right-click, or `--from-file`), finds the account and
number, stores them and loads the templates. `--phone` chooses when there are
several numbers.

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

Headings on the first page take priority over body phrases. A removal letter
mentioning an earlier removal notice remains a removal letter. Multiple matching
headings are held for review. Receipts require a receipt/invoice heading, both
receipt payment phrases, or the untitled chit form's CR identifier, customer
anchor and amount in words. On installations with multiple message types,
unrecognised text PDFs and scans are both held. In Needs attention, choose the
document type and review the message before sending; fields are re-read using
that type's rules.

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
is configuration, not a release, and **Setup → Document types** makes one by
clicking on a sample. Anything the profile does not recognise uses
the receipt selection (`default_template`). Recognised non-receipt documents
require an explicit message mapping.

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

Every processed receipt is now copied into a folder chosen in
Setup → Counter settings, filed by the date it was printed and named after itself:

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

The working copies under `inbox` are pruned after `keep_inbox_days` (90 by
default; 0 keeps them forever). Every unresolved job is protected, including
failed, captured and in-progress jobs, regardless of its age or queue position.
Failed sends stay in Needs attention until retried or discarded; review WhatsApp
before retrying when the previous result was uncertain.

Test sends and live sends have separate duplicate histories. Testing a receipt
does not suppress its first live send, and test sends do not inflate the live
sent-today count.

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
- **Check for updates**, at the foot of the Status page. A fix released at eleven in the
  morning should not wait for a timer, and this is how it reaches a branch the
  moment it exists — including over AnyDesk.

Guard rails: the manifest and the installer are fetched over HTTPS only, the
manifest must carry a SHA-256 and the download is verified against it before it
is executed. Capture, processing and queue sends share a lock with installation:
the updater checks again after download and excludes new work until the installer
exits. If a document is busy, use Check for updates again when it finishes.
The installer asks for administrator rights because it creates a printer queue,
so on a standard user account Windows shows that prompt rather than installing
unattended.
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
| [`ui/desktop.py`](src/waprinter/ui/desktop.py) | The application window: side bar, header and the counter's three pages |
| [`ui/theme.py`](src/waprinter/ui/theme.py) | The palette, typefaces and ttk styles every window uses |
| [`ui/icons.py`](src/waprinter/ui/icons.py) | Line icons, drawn from SVG by PyMuPDF at the screen's scaling |
| [`ui/setup.py`](src/waprinter/ui/setup.py) | The setup screens, one page per step |
| [`ui/teach.py`](src/waprinter/ui/teach.py) | Teaching a document type by clicking on a sample |
| [`ui/setupmodel.py`](src/waprinter/ui/setupmodel.py) | What setup decides — mapping guesses, checks, PIN — testable without a display |
| [`teaching.py`](src/waprinter/teaching.py) | Saving taught types, title checks, trials on recent prints |
| [`extract/rules.py`](src/waprinter/extract/rules.py) | Turns a click into a field rule, and reads it back off every print |
| [`send/meta_account.py`](src/waprinter/send/meta_account.py) | What a token reaches: account, numbers, expiry |
| [`send/sync.py`](src/waprinter/send/sync.py) | Refreshes templates from WhatsApp, examples included |
| [`ui/viewmodel.py`](src/waprinter/ui/viewmodel.py) | What it says, testable without a display |
| [`ui/notification.py`](src/waprinter/ui/notification.py) | The corner panel after a print |
| [`update.py`](src/waprinter/update.py) | Version check, verified download, silent install |
| [`capture/watcher.py`](src/waprinter/capture/watcher.py) | Drains the spool folder; waits for `%%EOF` before claiming a file |
| [`extract/split.py`](src/waprinter/extract/split.py) | Splits a batch print into one job per receipt |
| [`archive.py`](src/waprinter/archive.py) | Files a copy of every printed receipt where the office can find it |
| [`housekeeping.py`](src/waprinter/housekeeping.py) | Prunes old working copies and caps the logs, once a day |
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
  your PC". Until then the update relies on HTTPS to GitHub and the manifest's
  checksum; there is no signature on the installer itself.
- **Unattended updates on standard user accounts** — the installer needs
  elevation, so a clerk without admin rights sees a UAC prompt.
- **Amount extraction for chit receipts** — several competing figures on the page
  and no "Total" label, so it is deliberately left blank rather than guessed.

## Windows 7 build and verification

Use the `win7-x86` installer on Windows 7 SP1 (32-bit or 64-bit Windows).
The normal installer now requires 64-bit Windows 10 or later. Setup checks for
`AddDllDirectory`, the loader API supplied by KB2533623 or a superseding update,
before installing. The Windows 7 build requires exactly 32-bit Python 3.8 and
bundles the Universal CRT beside both executables.

The Windows 7 build also requires Visual Studio's **v142 (14.29) x86/x64
tools** and **Windows SDK 10.0.19041.0**. CI installs these explicitly on
`windows-2022`. `build.ps1` copies the x86 VC142 redistributable and that SDK's
x86 UCRT into both payloads, replacing any copies collected from the build
machine. `python3.dll` and `python38.dll` come from the selected Python 3.8
installation. Missing prerequisites stop the build; it never falls back to
the newest runtime installed on the runner.

Before smoke tests, `packaging/verify_win7_payload.py` checks every packaged
EXE/DLL/PYD for x86 architecture, checks runtime versions and Python stable-ABI
forwarders, compares imports with bundled exports, and rejects known Windows
8+ imports observed in incompatible runtimes. This audit is deliberately not
a claim of complete Windows 7 compatibility. Rebuild the installer after
source changes; old files in `installer/Output` do not contain those changes.
Windows 7 setup replaces the runtime files and removes only obsolete
`python312.dll` and `VCRUNTIME140_1.dll` copies from the app/CLI directories.
It does not remove `C:\ProgramData\WAPrinter` during an upgrade.

Both builds include OCR language data by default. PyMuPDF provides the OCR
engine; no separate Tesseract executable is shipped. The frozen build performs
an actual raster OCR smoke test, and the installer omits its OCR option when
built with `-SkipOcr`. Tagged releases wait for both builds before publishing
one manifest with separate x64 and win7-x86 downloads and checksums.

Modern Windows CI does not certify Windows 7 compatibility. Before shipping,
install on a clean Windows 7 SP1 VM without Python or Tesseract, then run
`powershell -ExecutionPolicy Bypass -File installer\verify-win7.ps1` from the
checkout. Also check: missing-loader-update rejection; exported PDF capture;
receipt/notice/letter routing in test mode; a scanned sample; logon startup;
upgrade and uninstall retaining history. Repeat on x86 and x64 Windows 7.
The verification script checks startup and OCR without sending messages; the
interactive capture and install checks still require the VM.
