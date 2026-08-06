"""Inline HTML for the local UI.

Kept as strings rather than template files so the frozen executable stays a
single self-contained bundle with no data-file paths to get wrong.
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
  :root { color-scheme: light dark; --bg:#fff; --fg:#1a1a1a; --muted:#666;
          --line:#e2e2e2; --accent:#128c7e; --warn:#b45309; --bad:#b91c1c;
          --card:#fafafa; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#16181c; --fg:#e8e8e8; --muted:#9aa0a6; --line:#2c2f36;
            --accent:#25d366; --warn:#f59e0b; --bad:#f87171; --card:#1e2127; }
  }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
         background:var(--bg); color:var(--fg); }
  header { border-bottom:1px solid var(--line); padding:12px 20px;
           display:flex; align-items:center; gap:20px; flex-wrap:wrap; }
  header h1 { font-size:16px; margin:0; font-weight:600; }
  nav a { color:var(--muted); text-decoration:none; margin-right:16px; }
  nav a.on { color:var(--fg); font-weight:600; }
  main { padding:20px; max-width:900px; }
  .mode { margin-left:auto; font-size:13px; padding:3px 10px; border-radius:99px;
          border:1px solid var(--line); }
  .mode.dry { color:var(--warn); border-color:var(--warn); }
  .mode.live { color:var(--accent); border-color:var(--accent); }
  .job { border:1px solid var(--line); border-radius:8px; padding:14px 16px;
         margin-bottom:12px; background:var(--card); }
  .job h3 { margin:0 0 4px; font-size:15px; }
  .why { color:var(--warn); margin:6px 0 12px; }
  .meta { color:var(--muted); font-size:13px; }
  form.row { display:flex; gap:8px; align-items:center; flex-wrap:wrap;
             margin-top:10px; }
  input[type=text] { padding:7px 10px; border:1px solid var(--line);
                     border-radius:6px; background:var(--bg); color:var(--fg);
                     font-size:14px; min-width:190px; }
  button { padding:7px 14px; border-radius:6px; border:1px solid var(--accent);
           background:var(--accent); color:#fff; font-size:14px; cursor:pointer; }
  button.ghost { background:transparent; color:var(--muted);
                 border-color:var(--line); }
  table { border-collapse:collapse; width:100%; font-size:14px; }
  th, td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:600; }
  .tag { font-size:12px; padding:2px 8px; border-radius:99px;
         border:1px solid var(--line); }
  .tag.sent { color:var(--accent); border-color:var(--accent); }
  .tag.held { color:var(--warn); border-color:var(--warn); }
  .tag.failed { color:var(--bad); border-color:var(--bad); }
  .empty { color:var(--muted); padding:30px 0; }
  .flash { padding:10px 14px; border-radius:6px; margin-bottom:16px;
           border:1px solid var(--accent); color:var(--accent); }
  .flash.err { border-color:var(--bad); color:var(--bad); }
  label { display:block; margin:14px 0 4px; font-size:14px; }
  .hint { color:var(--muted); font-size:13px; margin-top:2px; }
  pre { background:var(--card); border:1px solid var(--line); border-radius:6px;
        padding:12px; white-space:pre-wrap; font:13px/1.5 ui-monospace, monospace; }
  .tiles { display:grid; gap:12px; margin-bottom:24px;
           grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); }
  .tile { border:1px solid var(--line); border-radius:8px; padding:14px 16px;
          background:var(--card); }
  .tile .n { font-size:30px; font-weight:600; line-height:1.1; }
  .tile .k { color:var(--muted); font-size:13px; margin-top:4px; }
  .tile.good .n { color:var(--accent); }
  .tile.warn .n { color:var(--warn); }
  .tile.bad  .n { color:var(--bad); }
  h2 { font-size:15px; margin:0 0 10px; }
  .cta { display:inline-block; margin-top:4px; color:var(--warn); }
</style>
</head>
<body>
<header>
  <h1>WhatsApp Printer</h1>
  <nav>
    <a href="/" class="{{ 'on' if page == 'dashboard' }}">Dashboard</a>
    <a href="/queue" class="{{ 'on' if page == 'queue' }}">Queue{{ queue_badge }}</a>
    <a href="/history" class="{{ 'on' if page == 'history' }}">History</a>
    <a href="/settings" class="{{ 'on' if page == 'settings' }}">Settings</a>
  </nav>
  <span class="mode {{ 'dry' if dry_run else 'live' }}">
    {{ 'DRY RUN — nothing is sent' if dry_run else 'LIVE' }}
  </span>
</header>
<main>
{% if flash %}<div class="flash {{ flash_kind }}">{{ flash }}</div>{% endif %}
{# `body` is already-rendered markup from an autoescaping pass, so it is safe
   here. Every value inside it was escaped when that inner template ran. #}
{{ body | safe }}
</main>
</body>
</html>
"""

DASHBOARD = """
<h2>Today</h2>
<div class="tiles">
  <div class="tile good">
    <div class="n">{{ today.sent }}</div>
    <div class="k">{{ 'would have been sent' if settings.dry_run else 'sent' }}</div>
  </div>
  <div class="tile">
    <div class="n">{{ today.printed }}</div>
    <div class="k">documents printed</div>
  </div>
  <div class="tile {{ 'warn' if today.waiting }}">
    <div class="n">{{ today.waiting }}</div>
    <div class="k">waiting on you</div>
  </div>
  <div class="tile {{ 'bad' if today.failed }}">
    <div class="n">{{ today.failed }}</div>
    <div class="k">failed</div>
  </div>
</div>

{% if today.waiting %}
  <p><a class="cta" href="/queue">{{ today.waiting }} document(s) need a number
  — open the queue →</a></p>
{% endif %}

<h2>Delivery</h2>
{% if not delivery_available %}
  <p class="meta">Delivery receipts aren't wired up yet, so this shows what the
  provider accepted rather than what reached the customer's phone.</p>
{% endif %}
<table>
  <tr><th>State</th><th>Count</th><th></th></tr>
  <tr><td>Accepted by provider</td><td>{{ today.sent }}</td>
      <td class="meta">handed over successfully</td></tr>
  <tr><td>Rejected / failed</td><td>{{ today.failed }}</td>
      <td class="meta">see the queue for why</td></tr>
  <tr><td>Suppressed reprints</td><td>{{ today.duplicate }}</td>
      <td class="meta">same document already sent</td></tr>
</table>

<h2 style="margin-top:24px">Recent activity</h2>
<table>
  <tr><th>When</th><th>Status</th><th>Sent to</th><th>Document</th></tr>
  {% for job in recent %}
  <tr>
    <td>{{ job.created_at.strftime('%H:%M') }}</td>
    <td><span class="tag {{ job.status }}">{{ job.status }}</span></td>
    <td>{{ job.recipient or '—' }}</td>
    <td>{{ job.fields.invoice_number or job.doc_title or '—' }}</td>
  </tr>
  {% endfor %}
</table>
{% if not recent %}
  <p class="empty">Nothing printed yet today. Print something to the
  <strong>WhatsApp Printer</strong> and it will appear here.</p>
{% endif %}
"""

QUEUE = """
{% if not jobs %}
  <p class="empty">Nothing waiting. Held jobs appear here when the number on a
  printed page is missing, ambiguous, or not certain enough to send blind.</p>
{% endif %}
{% for job in jobs %}
  <div class="job">
    <h3>{{ job.fields.invoice_number or job.doc_title or 'Untitled document' }}</h3>
    <div class="meta">
      {{ job.created_at.strftime('%d %b %Y, %H:%M') }}
      {% if job.fields.customer_name %} · {{ job.fields.customer_name }}{% endif %}
      {% if job.fields.total_amount %} · ₹{{ job.fields.total_amount }}{% endif %}
      · <a href="/jobs/{{ job.id }}/pdf" target="_blank">view PDF</a>
    </div>
    <p class="why">{{ job.hold_reason }}</p>
    <form class="row" method="post" action="/jobs/{{ job.id }}/send">
      <input type="text" name="recipient" placeholder="Mobile number"
             value="{{ job.recipient or '' }}" required>
      <button type="submit">Send</button>
    </form>
    {% if job.fields.candidates %}
      <div class="meta" style="margin-top:8px">
        Found on the page:
        {% for c in job.fields.candidates %}
          {{ c.e164 }} ({{ c.confidence }}){% if not loop.last %}, {% endif %}
        {% endfor %}
      </div>
    {% endif %}
    <form class="row" method="post" action="/jobs/{{ job.id }}/discard">
      <button type="submit" class="ghost">Discard</button>
    </form>
  </div>
{% endfor %}
"""

HISTORY = """
<table>
  <tr><th>When</th><th>Status</th><th>Sent to</th><th>Invoice</th><th>Detail</th></tr>
  {% for job in jobs %}
  <tr>
    <td>{{ job.created_at.strftime('%d %b %H:%M') }}</td>
    <td><span class="tag {{ job.status }}">{{ job.status }}</span></td>
    <td>{{ job.recipient or '—' }}</td>
    <td>{{ job.fields.invoice_number or '—' }}</td>
    <td class="meta">{{ job.error or job.hold_reason or job.wamid or '' }}</td>
  </tr>
  {% endfor %}
</table>
{% if not jobs %}<p class="empty">No jobs yet.</p>{% endif %}
"""

SETTINGS = """
<form method="post" action="/settings">
  <label>Your own phone numbers</label>
  <input type="text" name="own_numbers" style="width:100%"
         value="{{ ','.join(settings.own_numbers) }}">
  <div class="hint">Comma separated. These are never treated as a customer, so
  the number in your invoice footer can't receive its own invoice.</div>

  <label>WhatsApp phone number ID</label>
  <input type="text" name="phone_number_id" style="width:100%"
         value="{{ settings.phone_number_id }}">
  <div class="hint">From Meta Business — WhatsApp → API Setup.</div>

  <label>Message template</label>
  <input type="text" name="default_template" style="width:100%"
         value="{{ settings.default_template }}">
  <div class="hint">Must be approved by Meta before it can be used.</div>

  <label>Suppress reprints for (hours)</label>
  <input type="text" name="dedupe_window_hours"
         value="{{ settings.dedupe_window_hours }}">

  <label>Maximum sends per minute</label>
  <input type="text" name="max_sends_per_minute"
         value="{{ settings.max_sends_per_minute }}">
  <div class="hint">Stops a runaway batch print from fanning out.</div>

  <label>
    <input type="checkbox" name="dry_run" {{ 'checked' if settings.dry_run }}>
    Dry run — process everything, send nothing
  </label>
  <div class="hint">Leave this on until extraction has been measured against
  real invoices.</div>

  <h3>Scanned invoices</h3>
  <label>
    <input type="checkbox" name="ocr_enabled" {{ 'checked' if settings.ocr_enabled }}>
    Read invoices that were printed as an image (OCR)
  </label>
  <div class="hint">
    {% if ocr_available %}
      Tesseract is installed and working.
    {% else %}
      <strong>Tesseract is not installed</strong>, so scanned pages can't be read.
      Reinstall with the OCR component selected.
    {% endif %}
  </div>

  <label>
    <input type="checkbox" name="ocr_silent_send"
           {{ 'checked' if settings.ocr_silent_send }}>
    Send to numbers read by OCR without asking
  </label>
  <div class="hint">Off by default, and worth leaving off. OCR confuses digits
  on a poor scan, and one wrong digit in a mobile number is a different real
  person. With this off, scanned invoices land in the queue for a two-second
  glance instead.</div>

  <p><button type="submit">Save</button></p>
</form>

<h3>Current message</h3>
{% if template %}
  <pre>{{ template.body }}{% if template.footer %}

{{ template.footer }}{% endif %}</pre>
  <p class="meta">Status with Meta: <strong>{{ template.status }}</strong></p>
  <p class="hint">The variable mapping ({{ settings.template_variables }}) takes
  effect on the next print. Changing the wording itself means submitting a new
  template to Meta for approval, which usually takes under a day — the current
  template keeps working meanwhile.</p>
{% else %}
  <p class="empty">No template configured.</p>
{% endif %}
"""
