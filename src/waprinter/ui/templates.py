"""The control panel, laid out like a printer's properties window.

Deliberately not a dashboard full of charts. The mental model is the dialog you
get from Printer Properties: a status line at the top telling you whether the
device is ready, tabs for the few things you can change, grouped fields with an
Apply button, and a Test Send that plays the same role as "Print Test Page".

Everything on screen is either something the operator must act on or something
that tells them the printer is working. Anything else was cut.
"""

from __future__ import annotations

BASE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} — WhatsApp Printer</title>
<style>
  :root { color-scheme: light dark;
          --bg:#f0f0f0; --panel:#fff; --fg:#1a1a1a; --muted:#5c5c5c;
          --line:#c8c8c8; --line-soft:#e6e6e6;
          --ok:#0f7b43; --warn:#a35a00; --bad:#b3261e; --sel:#0f6cbd; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#1b1c1f; --panel:#232529; --fg:#e9e9ea; --muted:#a0a3a8;
            --line:#3a3d43; --line-soft:#2c2f34;
            --ok:#3fbf7f; --warn:#e0a44a; --bad:#f4776b; --sel:#4aa3f0; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:13px/1.45 "Segoe UI", system-ui, -apple-system, sans-serif; }
  .window { max-width:720px; margin:18px auto; background:var(--panel);
            border:1px solid var(--line); border-radius:6px; overflow:hidden; }

  /* --- device status strip, like the top of a printer properties page --- */
  .device { display:flex; align-items:center; gap:14px; padding:16px 18px;
            border-bottom:1px solid var(--line-soft); }
  .device .icon { font-size:26px; line-height:1; }
  .device h1 { margin:0; font-size:15px; font-weight:600; }
  .device .state { margin-top:2px; font-size:12.5px; }
  .state.ok   { color:var(--ok); }
  .state.warn { color:var(--warn); }
  .state.bad  { color:var(--bad); }
  .device .spacer { margin-left:auto; }

  /* --- tabs ------------------------------------------------------------- */
  nav { display:flex; gap:2px; padding:0 12px; background:var(--bg);
        border-bottom:1px solid var(--line); }
  nav a { padding:8px 15px; font-size:12.5px; color:var(--muted);
          text-decoration:none; border:1px solid transparent;
          border-bottom:none; border-radius:5px 5px 0 0; position:relative;
          top:1px; }
  nav a:hover { color:var(--fg); }
  nav a.on { background:var(--panel); color:var(--fg); font-weight:600;
             border-color:var(--line); }
  nav .count { display:inline-block; min-width:17px; padding:0 5px;
               margin-left:5px; border-radius:9px; background:var(--warn);
               color:#fff; font-size:11px; text-align:center; }

  main { padding:18px; }

  /* --- grouped fields --------------------------------------------------- */
  fieldset { border:1px solid var(--line-soft); border-radius:5px;
             padding:14px 16px 16px; margin:0 0 16px; }
  legend { padding:0 6px; font-size:12px; font-weight:600; color:var(--muted);
           text-transform:uppercase; letter-spacing:.4px; }
  .field { display:flex; align-items:baseline; gap:12px; margin-bottom:11px; }
  .field > label { flex:0 0 170px; text-align:right; color:var(--fg); }
  .field > .control { flex:1; min-width:0; }
  input[type=text], input[type=number], select {
      width:100%; padding:5px 8px; border:1px solid var(--line);
      border-radius:3px; background:var(--panel); color:var(--fg);
      font:inherit; }
  input:focus, select:focus { outline:2px solid var(--sel); outline-offset:-1px; }
  .hint { color:var(--muted); font-size:11.5px; margin-top:3px; }
  .check { display:flex; gap:8px; align-items:flex-start; margin-bottom:11px; }
  .check input { margin-top:2px; }

  /* --- buttons ---------------------------------------------------------- */
  .buttons { display:flex; gap:8px; justify-content:flex-end;
             border-top:1px solid var(--line-soft); padding-top:14px; }
  button, .btn { font:inherit; padding:5px 18px; border-radius:3px;
                 border:1px solid var(--line); background:var(--panel);
                 color:var(--fg); cursor:pointer; text-decoration:none; }
  button.primary { background:var(--sel); border-color:var(--sel); color:#fff; }
  button:hover, .btn:hover { border-color:var(--sel); }

  /* --- readouts --------------------------------------------------------- */
  .counters { display:flex; gap:0; border:1px solid var(--line-soft);
              border-radius:5px; overflow:hidden; margin-bottom:16px; }
  .counters div { flex:1; padding:12px 14px; border-right:1px solid var(--line-soft); }
  .counters div:last-child { border-right:none; }
  .counters .n { font-size:21px; font-weight:600; }
  .counters .k { color:var(--muted); font-size:11.5px; margin-top:1px; }
  .counters .n.ok { color:var(--ok); }
  .counters .n.warn { color:var(--warn); }
  .counters .n.bad { color:var(--bad); }

  table { border-collapse:collapse; width:100%; font-size:12.5px; }
  th { text-align:left; padding:6px 8px; color:var(--muted); font-weight:600;
       border-bottom:1px solid var(--line); }
  td { padding:6px 8px; border-bottom:1px solid var(--line-soft);
       vertical-align:top; }
  .pill { display:inline-block; padding:1px 8px; border-radius:9px;
          font-size:11px; border:1px solid var(--line); color:var(--muted); }
  .pill.ok { color:var(--ok); border-color:var(--ok); }
  .pill.warn { color:var(--warn); border-color:var(--warn); }
  .pill.bad { color:var(--bad); border-color:var(--bad); }

  .item { border:1px solid var(--line-soft); border-radius:5px;
          padding:12px 14px; margin-bottom:10px; }
  .item h3 { margin:0 0 2px; font-size:13.5px; }
  .item .why { color:var(--warn); margin:6px 0 10px; }
  .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  .row input[type=text] { width:190px; }
  .empty { color:var(--muted); padding:22px 0; text-align:center; }
  .notice { padding:9px 12px; border-radius:4px; margin-bottom:14px;
            border:1px solid var(--ok); color:var(--ok); font-size:12.5px; }
  .notice.err { border-color:var(--bad); color:var(--bad); }
  .notice ul { margin:5px 0 0 16px; padding:0; }
  code { font:12px ui-monospace, Menlo, Consolas, monospace; }
  pre { background:var(--bg); border:1px solid var(--line-soft);
        border-radius:4px; padding:11px; white-space:pre-wrap;
        font:12px/1.5 ui-monospace, Menlo, Consolas, monospace; margin:0; }
</style>
</head>
<body>
<div class="window">
  <div class="device">
    <div class="icon">{{ device.icon }}</div>
    <div>
      <h1>WhatsApp Printer</h1>
      <div class="state {{ device.tone }}">{{ device.state }}</div>
    </div>
    <div class="spacer"></div>
    <form method="post" action="/test-send">
      <button type="submit">Test send</button>
    </form>
  </div>

  <nav>
    <a href="/" class="{{ 'on' if page == 'status' }}">Status</a>
    <a href="/queue" class="{{ 'on' if page == 'queue' }}">Needs attention{% if attention %}<span class="count">{{ attention }}</span>{% endif %}</a>
    <a href="/history" class="{{ 'on' if page == 'history' }}">Recent</a>
    <a href="/settings" class="{{ 'on' if page == 'settings' }}">Settings</a>
  </nav>

  <main>
    {% if flash %}<div class="notice {{ flash_kind }}">{{ flash }}</div>{% endif %}
    {{ body | safe }}
  </main>
</div>
</body>
</html>
"""

STATUS = """
<div class="counters">
  <div><div class="n ok">{{ today.sent }}</div>
       <div class="k">{{ 'sent today' if not settings.dry_run else 'test sends today' }}</div></div>
  <div><div class="n">{{ today.printed }}</div><div class="k">documents printed</div></div>
  <div><div class="n {{ 'warn' if today.waiting }}">{{ today.waiting }}</div>
       <div class="k">need attention</div></div>
  <div><div class="n {{ 'bad' if today.failed }}">{{ today.failed }}</div>
       <div class="k">failed</div></div>
</div>

{% if problems %}
<fieldset>
  <legend>Before this can send</legend>
  <ul style="margin:0 0 0 16px; padding:0; color:var(--warn)">
    {% for p in problems %}<li>{{ p }}</li>{% endfor %}
  </ul>
  <div class="hint" style="margin-top:8px">
    Fix these in <a href="/settings">Settings</a>. Printing still works meanwhile —
    documents are captured and wait here.
  </div>
</fieldset>
{% endif %}

<fieldset>
  <legend>How to use</legend>
  <div>In any program choose <strong>File → Print</strong>, pick
  <strong>WhatsApp Printer</strong>, and print as normal. The customer's number
  is read off the page and the document is sent to them on WhatsApp. A small
  panel appears in the corner to say whether it went.</div>
</fieldset>
"""

QUEUE = """
{% if not jobs %}
  <p class="empty">Nothing waiting. Documents appear here only when the
  customer's number could not be read off the page.</p>
{% endif %}
{% for job in jobs %}
  <div class="item">
    <h3>{{ job.fields.invoice_number or job.doc_title or 'Document' }}</h3>
    <div class="hint">
      {{ job.created_at.strftime('%d %b, %H:%M') }}
      {% if job.fields.customer_name %} · {{ job.fields.customer_name }}{% endif %}
      · <a href="/jobs/{{ job.id }}/pdf" target="_blank">view PDF</a>
    </div>
    <p class="why">{{ job.hold_reason or job.error }}</p>
    <form class="row" method="post" action="/jobs/{{ job.id }}/send">
      <input type="text" name="recipient" placeholder="Mobile number"
             value="{{ job.recipient or '' }}" required>
      <button class="primary" type="submit">Send</button>
      <span style="flex:1"></span>
    </form>
    <form method="post" action="/jobs/{{ job.id }}/discard" style="margin-top:8px">
      <button type="submit">Discard</button>
    </form>
  </div>
{% endfor %}
"""

HISTORY = """
<table>
  <tr><th>Time</th><th>Status</th><th>Sent to</th><th>Document</th><th></th></tr>
  {% for job in jobs %}
  <tr>
    <td>{{ job.created_at.strftime('%d %b %H:%M') }}</td>
    <td><span class="pill {{ job.tone }}">{{ job.label }}</span></td>
    <td>{{ job.recipient or '—' }}</td>
    <td>{{ job.fields.invoice_number or job.doc_title or '—' }}</td>
    <td class="hint">{{ job.error or job.hold_reason or '' }}</td>
  </tr>
  {% endfor %}
</table>
{% if not jobs %}<p class="empty">Nothing printed yet.</p>{% endif %}
"""

SETTINGS = """
<form method="post" action="/settings">
  <fieldset>
    <legend>WhatsApp account</legend>
    <div class="field">
      <label for="pid">Phone number ID</label>
      <div class="control">
        <input id="pid" type="text" name="phone_number_id"
               value="{{ settings.phone_number_id }}">
        <div class="hint">Meta Business → WhatsApp → API Setup.</div>
      </div>
    </div>
    <div class="field">
      <label for="own">Our own numbers</label>
      <div class="control">
        <input id="own" type="text" name="own_numbers"
               value="{{ ','.join(settings.own_numbers) }}">
        <div class="hint">Comma separated. Never treated as a customer, so the
        number printed on your own letterhead cannot be sent its own receipt.</div>
      </div>
    </div>
    <div class="field">
      <label for="tpl">Message</label>
      <div class="control">
        <input id="tpl" type="text" name="default_template"
               value="{{ settings.default_template }}">
        <div class="hint">Approved templates only — Meta requires this for
        messages you start. Change the wording by submitting a new template.</div>
      </div>
    </div>
  </fieldset>

  <fieldset>
    <legend>Sending</legend>
    <div class="check">
      <input id="dry" type="checkbox" name="dry_run"
             {{ 'checked' if settings.dry_run }}>
      <div><label for="dry"><strong>Test mode</strong> — process everything,
        send nothing</label>
        <div class="hint">Leave on until the WhatsApp account is live.</div></div>
    </div>
    <div class="check">
      <input id="confirm" type="checkbox" name="confirm_before_send"
             {{ 'checked' if settings.confirm_before_send }}>
      <div><label for="confirm">Ask before every send</label>
        <div class="hint">Off by default. Turn on while checking a new document
        layout, and every print will wait here instead of going straight out.</div>
      </div>
    </div>
    <div class="field">
      <label for="dedupe">Ignore reprints for</label>
      <div class="control" style="max-width:170px">
        <input id="dedupe" type="text" name="dedupe_window_hours"
               value="{{ settings.dedupe_window_hours }}"> <span class="hint">hours</span>
      </div>
    </div>
    <div class="field">
      <label for="rate">Maximum per minute</label>
      <div class="control" style="max-width:170px">
        <input id="rate" type="text" name="max_sends_per_minute"
               value="{{ settings.max_sends_per_minute }}">
        <div class="hint">Stops a runaway batch print.</div>
      </div>
    </div>
  </fieldset>

  <fieldset>
    <legend>Scanned documents</legend>
    <div class="check">
      <input id="ocr" type="checkbox" name="ocr_enabled"
             {{ 'checked' if settings.ocr_enabled }}>
      <div><label for="ocr">Read documents printed as an image (OCR)</label>
        <div class="hint">
          {% if ocr_available %}Ready.{% else %}
          <strong>Not available</strong> — reinstall with the OCR component.
          {% endif %}
        </div></div>
    </div>
    <div class="check">
      <input id="ocrsend" type="checkbox" name="ocr_silent_send"
             {{ 'checked' if settings.ocr_silent_send }}>
      <div><label for="ocrsend">Send to numbers read by OCR without asking</label>
        <div class="hint">Off by default. OCR misreads digits on a poor scan,
        and one wrong digit is a different person.</div></div>
    </div>
  </fieldset>

  <div class="buttons">
    <button class="primary" type="submit">Apply</button>
  </div>
</form>

<fieldset style="margin-top:18px">
  <legend>Current message</legend>
  {% if template %}
    <pre>{{ template.body }}{% if template.footer %}

{{ template.footer }}{% endif %}</pre>
    <div class="hint" style="margin-top:8px">
      Approval status: <strong>{{ template.status }}</strong>.
      Values in <code>{{ '{{1}}' }}</code> come from the printed page —
      currently {{ settings.template_variables }}.
    </div>
  {% else %}
    <p class="empty">No message configured.</p>
  {% endif %}
</fieldset>
"""


# The after-print notification. A window of its own, so it carries its own
# styles rather than the panel's chrome.
NOTE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>WhatsApp Printer</title>
<style>
  :root { color-scheme: light dark;
          --bg:#fff; --fg:#1a1a1a; --muted:#5c5c5c; --line:#dcdcdc; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#232529; --fg:#e9e9ea; --muted:#a0a3a8; --line:#3a3d43; }
  }
  html, body { height:100%; }
  body { margin:0; background:var(--bg); color:var(--fg); display:flex;
         font:13px/1.45 "Segoe UI", system-ui, -apple-system, sans-serif;
         border:1px solid var(--line); border-radius:6px; overflow:hidden;
         user-select:none; }
  .bar { width:5px; flex:0 0 5px; background:var(--accent); }
  .body { flex:1; padding:13px 15px; display:flex; flex-direction:column; }
  .head { display:flex; align-items:center; gap:9px; }
  .glyph { width:19px; height:19px; border-radius:50%; background:var(--accent);
           color:#fff; font-size:12px; font-weight:700; line-height:19px;
           text-align:center; flex:0 0 19px; }
  h1 { margin:0; font-size:13.5px; font-weight:600; }
  p { margin:7px 0 0; color:var(--muted); font-size:12.5px;
      overflow:hidden; text-overflow:ellipsis; }
  .actions { margin-top:auto; padding-top:11px; display:flex; gap:8px;
             justify-content:flex-end; }
  button { font:inherit; font-size:12.5px; padding:4px 14px; border-radius:3px;
           border:1px solid var(--line); background:transparent;
           color:var(--muted); cursor:pointer; }
  button.primary { border-color:var(--accent); color:var(--accent);
                   font-weight:600; }
</style>
</head>
<body style="--accent: {{ accent }}">
  <div class="bar"></div>
  <div class="body">
    <div class="head">
      <div class="glyph">{{ glyph }}</div>
      <h1>{{ headline }}</h1>
    </div>
    <p>{{ detail }}</p>
    {% if actionable %}
    <div class="actions">
      <button onclick="window.pywebview.api.dismiss()">Dismiss</button>
      <button class="primary" onclick="window.pywebview.api.open_panel()">Open</button>
    </div>
    {% endif %}
  </div>
</body>
</html>
"""
