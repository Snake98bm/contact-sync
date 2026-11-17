"""Server-rendered pages."""
import html
import json
import re
import time
import urllib.parse

from version import VERSION

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#667085;--line:#e4e7ec;--accent:#0d8078;
--ok:#1a7f4b;--okbg:#e3f5ea;--warn:#9a6200;--warnbg:#fff3d6;--bad:#b42318;--badbg:#fde8e7;
--info:#0b6b65;--infobg:#e0f4f2;--mute:#667085;--mutebg:#eef0f3}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#171b23;--text:#e6e9ef;--muted:#98a2b3;
--line:#2a303c;--accent:#4fd1c5;--ok:#5fd394;--okbg:#14301f;--warn:#f2c14e;--warnbg:#33290f;
--bad:#ff8a80;--badbg:#3a1714;--info:#7ee0d6;--infobg:#123532;--mute:#98a2b3;--mutebg:#232833}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{background:var(--card);border-bottom:1px solid var(--line)}
.wrap{max-width:1100px;margin:0 auto;padding:0 16px}
.top{display:flex;align-items:center;gap:20px;min-height:56px;flex-wrap:wrap}
.brand{font-weight:650;font-size:17px;display:flex;gap:8px;align-items:center}.brand img{width:24px;height:24px}
nav a{color:var(--muted);text-decoration:none;margin-right:16px}nav a.on{color:var(--text);font-weight:600}
main{padding:20px 16px 48px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px}
.sub{color:var(--muted);margin:0 0 16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:16px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.tile b{display:block;font-size:24px;font-variant-numeric:tabular-nums}.tile span{color:var(--muted);font-size:13px}
a.tile{color:inherit;text-decoration:none}a.tile:hover{border-color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow-x:auto;padding:0}
.pad{padding:14px 16px}.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:600}
tr:last-child td{border-bottom:0}td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
.pill{display:inline-block;padding:1px 9px;border-radius:99px;font-size:12.5px;font-weight:600;white-space:nowrap}
.p-ok{color:var(--ok);background:var(--okbg)}.p-warn{color:var(--warn);background:var(--warnbg)}
.p-bad{color:var(--bad);background:var(--badbg)}.p-info{color:var(--info);background:var(--infobg)}
.p-mute{color:var(--mute);background:var(--mutebg)}tr.review>td{background:var(--warnbg)}tr.review:has(input:checked)>td{background:none}tr.review:has(input:checked) .p-warn{color:var(--ok);background:var(--okbg)}tr.review:has(input:checked) .rv:after{content:"ed"}.muted{color:var(--muted)}.small{font-size:13px}
button,.btn{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:8px 14px;font:inherit;
font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
@media (prefers-color-scheme:dark){button,.btn{color:#0f1218}}
button.ghost{background:transparent;color:var(--accent);padding:2px 6px;font-weight:500}
button:disabled{opacity:.5;cursor:default}.bar{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.banner{background:var(--infobg);color:var(--info);border-radius:10px;padding:10px 14px;margin:12px 0}
.err{background:var(--badbg);color:var(--bad)}.warnb{background:var(--warnbg);color:var(--warn)}
form.inline{display:inline;margin:0}label{display:block;margin:14px 0 4px;font-weight:600}
input[type=text],input[type=number],input[type=time],input[type=password],input[type=url]{padding:7px 9px;
border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);font:inherit;width:160px}
input.wide{width:100%}input[type=password]{width:100%;max-width:320px}
.hint{color:var(--muted);font-size:13px;margin-top:3px}.check{display:flex;gap:8px;align-items:center;font-weight:600;margin-top:14px}
.code{font:600 28px/1.2 ui-monospace,Consolas,monospace;letter-spacing:.12em;background:var(--bg);
border:1px dashed var(--line);border-radius:10px;padding:10px 16px;display:inline-block;margin:8px 0}
a{color:var(--accent)}.break{word-break:break-all}details{margin:6px 0}summary{cursor:pointer}
"""


def esc(v):
    return html.escape("" if v is None else str(v))


def when(ts):
    if not ts:
        return "never"
    t = time.localtime(ts)
    return time.strftime("%a %b ", t) + f"{t.tm_mday}, {t.tm_hour % 12 or 12}:{t.tm_min:02d} " + (
        "AM" if t.tm_hour < 12 else "PM")


def pill(text, cls):
    return f'<span class="pill p-{cls}">{esc(text)}</span>'


def page(title, active, body, refresh=False, nav_on=True):
    links = [("/", "Dashboard"), ("/contacts", "Contacts"), ("/sync", "Sync"), ("/cleanup", "Cleanup"),
             ("/report", "Report"),
             ("/bin", "Recycle bin"), ("/backups", "Backups"),
             ("/settings", "Settings")]
    nav = ""
    if nav_on:
        nav = "".join(f'<a href="{h}" class="{"on" if h == active else ""}">{t}</a>' for h, t in links)
        nav += ('<form method="post" action="/logout" class="inline">'
                '<button class="ghost" style="padding:0">Log out</button></form>')
    js = ""
    if refresh:
        js = """<script>setInterval(async()=>{try{const r=await fetch('/api/status');const s=await r.json();
if(!s.job.running){location.reload()}else{const e=document.getElementById('prog');if(e)e.textContent=s.job.step+'…'}}
catch(e){}},2500)</script>"""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · Contact Sync</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml"><link rel="icon" href="/favicon.ico" sizes="32x32">
<link rel="apple-touch-icon" href="/apple-touch-icon.png"><link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0d8078"><meta name="apple-mobile-web-app-title" content="Contacts">
<style>{CSS}</style></head><body><header><div class="wrap top"><div class="brand"><img src="/favicon.svg" alt="">
Contact Sync</div><nav>{nav}</nav></div></header><main class="wrap">{body}
<p class="muted small" style="margin-top:40px">contact-sync {VERSION} · changes are made only from Contacts and the
recycle bin, and every change keeps a copy first.</p></main>{js}</body></html>"""


PASSKEY_JS = """<script>
const b64u=b=>btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
const unb64u=s=>Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-s.length%4)%4)),c=>c.charCodeAt(0));
async function post(url,data){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify(data||{})});const j=await r.json().catch(()=>({error:'Unexpected reply ('+r.status+')'}));
if(!r.ok)throw new Error(j.error||('Error '+r.status));return j}
function say(msg,bad){const e=document.getElementById('msg');e.textContent=msg;e.className='banner'+(bad?' err':'');e.hidden=!msg}
function supported(){if(!window.isSecureContext||!window.PublicKeyCredential){say('This browser can\\'t use passkeys here. '+
'Install the certificate first (see the link below), then open this site again.',true);return false}return true}
function nice(e){if(e&&e.name==='NotAllowedError')return 'Cancelled or timed out. Try again.';
if(e&&e.name==='InvalidStateError')return 'This device already has a passkey for this site.';return (e&&e.message)||String(e)}
async function addPasskey(code,name){
  const o=await post('/auth/register/options',{code});
  o.challenge=unb64u(o.challenge);o.user.id=unb64u(o.user.id);
  o.excludeCredentials=(o.excludeCredentials||[]).map(c=>({...c,id:unb64u(c.id)}));
  const c=await navigator.credentials.create({publicKey:o});
  return post('/auth/register',{name,clientDataJSON:b64u(c.response.clientDataJSON),
    attestationObject:b64u(c.response.attestationObject)});
}
async function signIn(next){
  const o=await post('/auth/login/options');
  o.challenge=unb64u(o.challenge);o.allowCredentials=(o.allowCredentials||[]).map(c=>({...c,id:unb64u(c.id)}));
  const a=await navigator.credentials.get({publicKey:o});
  return post('/auth/login',{next,id:a.id,clientDataJSON:b64u(a.response.clientDataJSON),
    authenticatorData:b64u(a.response.authenticatorData),signature:b64u(a.response.signature)});
}
function guessName(){const u=navigator.userAgent;return /iPhone/.test(u)?'iPhone':/iPad/.test(u)?'iPad':
/Android/.test(u)?'Android phone':/Windows/.test(u)?'Windows PC':/Mac/.test(u)?'Mac':'Passkey'}
</script>"""


def setup_page(origin, error=""):
    body = f"""<h1>Set up sign-in</h1>
<p class="sub">Choose how you'll sign in to this site. You can add the other one later in Settings.</p>
<div id="msg" class="banner{' err' if error else ''}"{'' if error else ' hidden'}>{esc(error)}</div>
<label>Setup code</label><input type="text" id="code" autocomplete="off" autocapitalize="characters"
style="width:200px;text-transform:uppercase;letter-spacing:.1em" placeholder="ABCD2345">
<div class="hint">A one-time code printed in the container's log (Dockhand → contact-sync → Logs). It stops
working once the first sign-in method exists.</div>
<div class="grid2" style="margin-top:16px">
<div class="card pad"><b>Passkey</b> <span class="muted small">(recommended)</span>
<p class="small">Windows Hello, your phone, or a security key. Nothing to remember or type.</p>
<label>Name for this passkey</label><input type="text" id="name" maxlength="60">
<p><button id="go">Create passkey</button></p>
<p class="small"><a href="/cert">Install this site's certificate</a> first: passkeys need it.</p></div>
<div class="card pad"><b>Password</b>
<form method="post" action="/setup/password" onsubmit="this.code.value=document.getElementById('code').value">
<input type="hidden" name="code"><label>Password</label><input type="password" name="password"
autocomplete="new-password" minlength="10"><label>Again</label><input type="password" name="password2"
autocomplete="new-password"><div class="hint">At least 10 characters.</div>
<p><button>Set password</button></p></form></div></div>
{PASSKEY_JS}<script>
document.getElementById('name').value=guessName();
document.getElementById('go').onclick=async()=>{{if(!supported())return;say('Follow your device\'s prompt…');
try{{await addPasskey(document.getElementById('code').value,document.getElementById('name').value);location.href='/'}}
catch(e){{say(nice(e),true)}}}};</script>"""
    return page("Set up", "", body, nav_on=False)


def login_page(nxt="/", error="", has_password=False, has_passkey=True):
    pk = f"""<p><button id="go">Sign in with a passkey</button></p>""" if has_passkey else ""
    pw = f"""<form method="post" action="/login/password" style="margin-top:18px">
<input type="hidden" name="next" value="{esc(nxt)}"><label>Password</label>
<input type="password" name="password" autocomplete="current-password" {'' if has_passkey else 'autofocus'}>
<p><button{' class="ghost"' if has_passkey else ''}>Sign in with password</button></p></form>""" if has_password else ""
    body = f"""<h1>Sign in</h1>
<div id="msg" class="banner{' err' if error else ''}"{'' if error else ' hidden'}>{esc(error)}</div>
{pk}{pw}
<p class="small"><a href="/cert">Install this site's certificate</a> on a new device first.</p>
<p class="muted small">Locked out? Delete <code>auth.json</code> in the container's data folder and restart it;
a new setup code is printed in the log.</p>
{PASSKEY_JS}<script>
const nxt={json.dumps(nxt).replace("<", "\u003c")};
const b=document.getElementById('go');
if(b)b.onclick=async()=>{{if(!supported())return;say('');try{{const r=await signIn(nxt);location.href=r.next||'/'}}
catch(e){{say(nice(e),true)}}}};</script>"""
    return page("Sign in", "", body, nav_on=False)


def cert_page(fingerprint, host, origin):
    body = f"""<h1>Install the certificate</h1>
<p class="sub">This site uses its own private certificate authority instead of a public one. Install it once on
each device you use. Browsers then trust <b>{esc(origin)}</b> with no warning, which passkeys need. It is limited
to this site's names, so it can't vouch for any other website.</p>
<p><a class="btn" href="/ca.crt">Download the certificate</a></p>
<p class="small">Check it before trusting it. SHA-256 fingerprint:<br><code class="break">{esc(fingerprint)}</code></p>
<h2>Windows</h2><ol class="small">
<li>Open the downloaded <code>contact-sync-ca.crt</code> → <b>Install Certificate…</b></li>
<li>Store location: <b>Current User</b> → Next.</li>
<li><b>Place all certificates in the following store</b> → Browse → <b>Trusted Root Certification Authorities</b> → OK → Next → Finish.</li>
<li>Accept the security warning (check the fingerprint matches), then fully close and reopen the browser.</li></ol>
<h2>iPhone / iPad</h2><ol class="small">
<li>Open this page in <b>Safari</b> and tap the download button → <b>Allow</b>.</li>
<li>Settings → <b>Profile Downloaded</b> → Install.</li>
<li>Settings → General → About → <b>Certificate Trust Settings</b> → turn on <b>contact-sync local CA</b>.</li></ol>
<h2>Android</h2><ol class="small">
<li>Download the file, then Settings → Security → More security settings → Encryption &amp; credentials →
<b>Install a certificate</b> → <b>CA certificate</b> → pick the file.</li></ol>
<h2>Mac</h2><ol class="small">
<li>Open the file → Keychain Access adds it to <b>login</b>.</li>
<li>Double-click it there → Trust → <b>When using this certificate: Always Trust</b>.</li></ol>
<p><a href="{esc(origin)}/">Go to the site</a></p>"""
    return page("Certificate", "", body, nav_on=False)


STATE = {"ok": ("Signed in", "ok"), "signed_out": ("Not signed in", "warn"),
         "broken": ("Sign in again", "bad"), "unconfigured": ("Not set up", "bad")}


def _account_card(title, st, start_url, setup_hint):
    label, cls = STATE[st["state"]]
    out = [f'<div class="card pad"><div class="bar" style="justify-content:space-between"><b>{esc(title)}</b>'
           f'{pill(label, cls)}</div>']
    if st.get("account"):
        out.append(f'<p class="muted small">{esc(st["account"])}</p>')
    if st["state"] == "unconfigured":
        out.append(f'<p class="small">{esc(setup_hint)}</p>')
    else:
        if st.get("error"):
            out.append(f'<p class="small" style="color:var(--bad)">{esc(st["error"])}</p>')
        text = "Sign in again" if st["state"] == "ok" else "Sign in"
        out.append(f'<p><a class="btn" href="{start_url}">{text}</a></p>')
    out.append("</div>")
    return "".join(out)


def dashboard(ms, gg, job, report, last_run, next_backup, api_on):
    b = ['<div class="bar" style="justify-content:space-between"><div><h1>Dashboard</h1>'
         f'<p class="sub">Next backup: <b>{esc(next_backup)}</b></p></div>']
    can = ms["state"] == "ok" or gg["state"] == "ok"
    dis = "" if can and not job["running"] else " disabled"
    b.append(f'<form method="post" action="/run" class="inline"><button{dis}>Back up and build report now'
             '</button></form></div>')
    if job["running"]:
        b.append(f'<div class="banner" id="prog">{esc(job["step"])}…</div>')
    if job.get("error"):
        b.append(f'<div class="banner err">Last run failed: {esc(job["error"])}</div>')
    b.append('<div class="grid2">')
    b.append(_account_card("Microsoft 365", ms, "/signin/microsoft",
                           "Set MS_CLIENT_ID (and MS_TENANT) in the stack's environment, then redeploy."))
    b.append(_account_card("Google Contacts", gg, "/signin/google",
                           "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in the stack's environment, then redeploy."))
    b.append("</div>")
    if last_run:
        state = (pill("Failed", "bad") if last_run["error"] else pill("OK", "ok")) if last_run["finished"] else pill("Running", "info")
        b.append(f'<p class="muted small">Last run: {when(last_run["started"])} ({esc(last_run["trigger"])}) {state}</p>')
    if report:
        b.append("<h2>Latest cleanup report</h2>")
        b.append(_tiles(report))
        b.append(f'<p><a href="/report">Open the full report</a> · made {when(report.get("created"))}</p>')
    else:
        b.append('<p class="muted">No report yet. Sign in to both accounts, then click '
                 '<b>Back up and build report now</b>.</p>')
    return page("Dashboard", "/", "".join(b), refresh=job["running"])


def _tiles(r):
    c = r["counts"]
    t = [("Microsoft 365", c["microsoft"], ""), ("Google", c["google"], ""), ("People", c["people"], ""),
         ("Paired 1 : 1", c["paired"], ""), ("Only in Microsoft", c["only_ms"], "#only_ms"),
         ("Only in Google", c["only_google"], "#only_google"),
         ("Duplicate groups", c["dup_groups"], "#duplicates"), ("Extra copies", c["dup_extra"], "#duplicates"),
         ("Possible matches", c["possible"], "#possible"), ("Junk", c["junk"], "#junk"),
         ("Phone fixes", c["phone_fixes"], "#phones"), ("Unreadable phones", c["phone_unknown"], "#phones"),
         ("Name fixes", c["name_fixes"], "#names"), ("Old sync-tool notes", c["sync_tool_contacts"], "#synctool"),
         ("“Contact Imported” blocks", c.get("import_blocks", 0), "#imported"),
         ("“Last, First” names", c.get("name_order", 0), "#nameorder"),
         ("Shared phones/emails", c.get("shared_info", 0), "#shared")]
    return '<div class="tiles">' + "".join(
        (f'<a class="tile" href="/report{h}">' if h else '<div class="tile">') + f"<b>{n}</b><span>{esc(lbl)}</span>"
        + ("</a>" if h else "</div>") for lbl, n, h in t) + "</div>"


SIDE = {"microsoft": ("M365", "info"), "google": ("Google", "ok")}


def _who(c):
    side = pill(*SIDE[c["side"]])
    bits = [esc(c["name"] or "(no name)")]
    if c.get("company"):
        bits.append(f'<span class="muted">{esc(c["company"])}</span>')
    detail = ", ".join(esc(x) for x in (c["emails"] + [p["value"] for p in c["phones"]])[:4])
    return f'{side} {" · ".join(bits)}<div class="muted small">{detail}</div>'


def _section(anchor, title, intro, rows, head, limit=300):
    out = [f'<h2 id="{anchor}">{esc(title)} <span class="muted">({len(rows)})</span></h2>']
    if intro:
        out.append(f'<p class="muted small">{intro}</p>')
    if not rows:
        out.append('<p class="muted small">None.</p>')
        return "".join(out)
    out.append('<div class="card"><table><tr>' + "".join(f"<th>{h}</th>" for h in head) + "</tr>")
    out += [f"<tr>{r}</tr>" for r in rows[:limit]]
    out.append("</table></div>")
    if len(rows) > limit:
        out.append(f'<p class="muted small">Showing the first {limit}. The full list is in the report.json of '
                   'the backup.</p>')
    return "".join(out)


def report_page(r):
    if not r:
        return page("Cleanup report", "/report", "<h1>Cleanup report</h1><p class='muted'>No report yet.</p>")
    b = ["<h1>Cleanup report</h1>",
         f'<p class="sub">From the backup of {when(r.get("created"))}. Nothing has been changed; this is what a '
         'cleanup would work through.</p>']
    for n in r.get("notes") or []:
        b.append(f'<div class="banner warnb">{esc(n)}</div>')
    b.append(_tiles(r))

    st = r["sync_tool"]
    rows = [f"<td>{esc(line)}</td><td class='num'>{n}</td>" for line, n in st["lines"]]
    b.append(_section("synctool", "Old sync-tool notes",
                      "Note lines that look like they were written by a sync tool (SyncGene and similar). "
                      "Numbers and ids are shown as #. These get stripped in the cleanup; your own notes stay.",
                      rows, ["Line pattern", "Contacts"]))
    rows = [f"<td>{_who(i['contact'])}</td><td>{'yes' if i['keeps_text'] else ''}</td>"
            f"<td>{pill('no address field', 'warn') if i['no_address'] else 'already in the fields'}</td>"
            for i in r.get("import_blocks", [])]
    b.append(_section("imported", "“Contact Imported” blocks in notes",
                      "A dashed line, “Contact Imported:” and “Home Street : …” lines appended by an older import or "
                      "sync tool. The cleanup removes the block. Your own text above it stays, and an address that "
                      "isn't in the address fields yet is moved there first.",
                      rows, ["Contact", "Has your own notes too", "Address"]))
    rows = [f"<td>{pill(*SIDE[f['contact']['side']])}</td><td>{esc(f['old'])}</td><td>{esc(f['new'])}</td>"
            for f in r.get("name_order", [])]
    b.append(_section("nameorder", "Names stored as “Last, First”",
                      "Rebuilt from the first and last name fields.", rows, ["Side", "Now", "Would become"]))
    rows = [f"<td>{esc(s['kind'])}: {esc(s['value'])}</td><td>"
            + "".join(f"<div>{_who(c)}</div>" for c in s["contacts"]) + "</td>" for s in r.get("shared_info", [])]
    b.append(_section("shared", "Phones and emails shared by different people",
                      "A household line or a business number on more than one person. These are <b>not</b> merged.",
                      rows, ["Shared", "Contacts"]))
    rows = [f"<td>{esc(line)}</td><td class='num'>{n}</td>" for line, n in r["repeated_note_lines"]]
    b.append(_section("repeated", "Note lines repeated in 5+ contacts",
                      "Lines that appear in many contacts are usually written by a tool, not by you. "
                      "Tell me which of these to strip.", rows, ["Line pattern", "Contacts"]))
    rows = [f"<td>{esc(g)}</td><td class='num'>{n}</td>" for g, n in r["groups"]]
    b.append(_section("groups", "Groups, labels, categories and folders", "", rows, ["Name", "Contacts"]))

    rows = []
    for p in r["duplicates"]:
        rows.append(f"<td>{esc(p['name'])}</td><td class='num'>{p['ms']}</td><td class='num'>{p['google']}</td>"
                    "<td>" + "".join(f"<div>{_who(c)}</div>" for c in p["members"]) + "</td>")
    b.append(_section("duplicates", "Duplicates (same email or phone)",
                      "More than one copy on the same side. The cleanup merges each group into one contact, "
                      "keeping every email, phone and note.", rows, ["Person", "M365", "Google", "Copies"]))

    rows = []
    for g in r["possible"]:
        rows.append(f"<td>{esc(g['name'])}</td><td>" + "".join(
            f"<div>{_who(c)}</div>" for p in g["people"] for c in p["members"]) + "</td>")
    b.append(_section("possible", "Possible matches (same name, nothing shared)",
                      "Same name but no shared email or phone. Could be one person or two; these get a closer look.",
                      rows, ["Name", "Contacts"]))

    rows = [f"<td>{_who(j['contact'])}</td><td>{esc(j['reason'])}</td>" for j in r["junk"]]
    b.append(_section("junk", "Junk", "Flagged only. Nothing is deleted without your OK.", rows, ["Contact", "Why"]))

    rows = [f"<td>{_who(f['contact'])}</td><td>{esc(f['old'])}</td><td>{esc(f['new'])}</td>" for f in r["phone_fixes"]]
    rows += [f"<td>{_who(f['contact'])}</td><td>{esc(f['value'])}</td><td class='muted'>can't read: left as is</td>"
             for f in r["phone_unknown"]]
    b.append(_section("phones", "Phone formatting", "", rows, ["Contact", "Now", "Would become"]))
    rows = [f"<td>{pill(*SIDE[f['contact']['side']])}</td><td>{esc(f['old'])}</td><td>{esc(f['new'])}</td>"
            for f in r["name_fixes"]]
    b.append(_section("names", "Name capitalisation", "", rows, ["Side", "Now", "Would become"]))
    rows = [f"<td>{pill(*SIDE[f['contact']['side']])}</td><td>{esc(f['old'])}</td><td>{esc(f['new'])}</td>"
            for f in r["email_fixes"]]
    b.append(_section("emails", "Email formatting", "", rows, ["Side", "Now", "Would become"]))

    for key, title in (("only_ms", "Only in Microsoft 365"), ("only_google", "Only in Google")):
        rows = ["<td>" + "".join(f"<div>{_who(c)}</div>" for c in p["members"]) + "</td>" for p in r[key]]
        b.append(_section(key, title, "These get copied to the other side when sync starts.", rows, ["Contact"]))
    return page("Cleanup report", "/report", "".join(b))


def signin_microsoft(state, error=""):
    b = ["<h1>Sign in to Microsoft 365</h1>"]
    if error:
        b.append(f'<div class="banner err">{esc(error)}</div>')
    if state.get("status") == "waiting":
        b.append(f"""<p>1. Open <a href="{esc(state['verification_uri'])}" target="_blank" rel="noopener">
{esc(state['verification_uri'])}</a> (new tab).</p><p>2. Enter this code:</p><div class="code">{esc(state['user_code'])}</div>
<p>3. Sign in with your Microsoft 365 account and accept. This page moves on by itself.</p>
<script>setInterval(async()=>{{try{{const r=await fetch('/api/signin/microsoft');const s=await r.json();
if(s.status!=='waiting')location.href='/signin/microsoft/done'}}catch(e){{}}}},3000)</script>""")
    elif state.get("status") == "failed":
        b.append(f'<div class="banner err">{esc(state.get("error"))}</div>')
        b.append('<form method="post" action="/signin/microsoft"><button>Get a new code</button></form>')
    elif state.get("status") == "done":
        b.append('<div class="banner">Signed in.</div><p><a href="/">Back to the dashboard</a></p>')
    else:
        b.append('<p>You\'ll get a short code to type in at Microsoft\'s sign-in page.</p>'
                 '<form method="post" action="/signin/microsoft"><button>Get a code</button></form>')
    return page("Microsoft sign-in", "", "".join(b))


def signin_google(url, error=""):
    b = ["<h1>Sign in to Google</h1>"]
    if error:
        b.append(f'<div class="banner err">{esc(error)}</div>')
    b.append(f"""<p>1. <a href="{esc(url)}" target="_blank" rel="noopener">Open Google sign-in</a> (new tab) and sign
in with your Google account.</p>
<p>2. Google warns that it hasn't verified this app. That's expected for your own app: click <b>Advanced</b>, then
<b>Go to contact-sync (unsafe)</b>, then allow access to your contacts.</p>
<p>3. The tab ends on a page that won't load, with an address starting <code>http://localhost/?state=</code>.
Copy that whole address from the address bar and paste it here:</p>
<form method="post" action="/signin/google"><input type="url" class="wide" name="pasted" required
placeholder="http://localhost/?state=…&code=…"><p><button>Finish sign-in</button></p></form>
<p class="muted small">The link works once. If you reload this page, use the new link.</p>""")
    return page("Google sign-in", "", "".join(b))


def backups_page(items):
    rows = []
    for it in items:
        links = " · ".join(f'<a href="/backups/{esc(it["name"])}/{esc(f)}">{esc(f)}</a>' for f in it["files"])
        rows.append(f"<td class='num'>{esc(it['name'])}</td><td class='num'>{it['size'] / 1048576:.1f} MB</td><td>{links}</td>")
    body = ("<h1>Backups</h1><p class='sub'>One folder per run. <b>.vcf</b> files import into Outlook, Google "
            "Contacts or a phone. The <b>.json.gz</b> files are the full raw data a restore uses.</p>"
            + _section("list", "Saved backups", "", rows, ["Taken", "Size", "Files"]))
    return page("Backups", "/backups", body)


def settings_page(s, access, saved=False, error="", cert=""):
    chk = lambda k: " checked" if s[k] else ""
    pk_rows = "".join(
        f"<tr><td>{esc(p['name'])}</td><td class='num'>{when(p['created'])}</td><td class='num'>{when(p['last_used'])}</td>"
        f"<td><form method='post' action='/passkeys/remove' class='inline' onsubmit=\"return confirm('Remove this passkey?')\">"
        f"<input type='hidden' name='id' value='{esc(p['id'])}'><button class='ghost'>Remove</button></form></td></tr>"
        for p in access["passkeys"]) or "<tr><td colspan='4' class='muted'>No passkeys yet.</td></tr>"
    key_rows = "".join(
        f"<tr><td>{esc(k['name'])}</td><td><code>{esc(k['prefix'])}…</code></td><td class='num'>{when(k['created'])}</td>"
        f"<td class='num'>{when(k['last_used'])}</td><td><form method='post' action='/apikeys/revoke' class='inline' "
        f"onsubmit=\"return confirm('Revoke this key? Anything using it stops working.')\">"
        f"<input type='hidden' name='id' value='{esc(k['id'])}'><button class='ghost'>Revoke</button></form></td></tr>"
        for k in access["api_keys"])
    if access["env_key"]:
        key_rows += ("<tr><td>From the container setting <code>API_KEY</code></td><td></td><td></td><td></td>"
                     "<td class='muted small'>change it in the stack</td></tr>")
    key_rows = key_rows or "<tr><td colspan='5' class='muted'>No API keys.</td></tr>"
    new_key = ""
    if access["new_key"]:
        new_key = (f'<div class="banner"><b>New API key. Copy it now; it won\'t be shown again.</b><br>'
                   f'<code class="break" style="font-size:15px">{esc(access["new_key"])}</code><br>'
                   f'<span class="small">Use it as <code>Authorization: Bearer &lt;key&gt;</code>.</span></div>')
    has_pw = access["has_password"]
    pw_form = f"""<form method="post" action="/password">
{'<label>Current password</label><input type="password" name="current" autocomplete="current-password">' if has_pw else ''}
<label>{'New password' if has_pw else 'Password'}</label><input type="password" name="password" autocomplete="new-password"
minlength="{access['min_len']}"><label>Again</label><input type="password" name="password2" autocomplete="new-password">
<div class="hint">At least {access['min_len']} characters. Changing it signs out every other browser.</div>
<p><button>{'Change password' if has_pw else 'Set password'}</button></p></form>"""
    if has_pw:
        pw_form += ("<form method='post' action='/password/remove' onsubmit=\"return confirm('Remove the password? "
                    "You will sign in with a passkey only.')\"><button class='ghost' style='padding:0'>Remove the "
                    "password (passkeys only)</button></form>")
    body = f"""<h1>Settings</h1>{'<div class="banner">Saved.</div>' if saved else ''}
{f'<div class="banner err">{esc(error)}</div>' if error else ''}{new_key}
<form method="post" action="/settings">
<label class="check"><input type="checkbox" name="backup_enabled"{chk('backup_enabled')}> Nightly backup and report</label>
<label>Time</label><input type="time" name="backup_time" value="{esc(s['backup_time'])}">
<label>Keep backups for (days)</label><input type="number" name="keep_days" min="3" max="365" value="{s['keep_days']}">
<label>Country for phone numbers without a code</label><input type="text" name="default_region" maxlength="2"
value="{esc(s['default_region'])}"><div class="hint">Two letters, e.g. US, CA, GB, AU.</div>
<label>Write mode</label>
<select name="write_mode" style="padding:7px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--text)">
<option value="off"{' selected' if s.get('write_mode') == 'off' else ''}>Off: nothing is changed in your accounts</option>
<option value="test"{' selected' if s.get('write_mode', 'test') == 'test' else ''}>Test: only contacts named “Contact-Sync Test…”</option>
<option value="on"{' selected' if s.get('write_mode') == 'on' else ''}>On: any contact can be edited, deleted and restored</option>
</select><div class="hint">Every change keeps a copy first (history or recycle bin), so it can be undone.</div>
<p><button>Save</button></p></form>
<form method="post" action="/test-contact" style="margin-top:6px"><button class="ghost" style="padding:0">Create a
“Contact-Sync Test” contact in both accounts</button></form>

<h2>Passkeys</h2><div class="card"><table><tr><th>Name</th><th>Added</th><th>Last used</th><th></th></tr>
{pk_rows}</table></div>
<div id="msg" class="banner" hidden></div>
<div class="bar" style="margin-top:12px"><input type="text" id="pkname" maxlength="60" placeholder="Name, e.g. iPhone">
<button id="addpk">Add a passkey</button></div>
<div class="hint">Add one for each device (or a phone you can use from any computer). The last way to sign in
can't be removed.</div>

<h2>Password</h2><p class="small">{'A password is set. You can sign in with it or with a passkey.' if has_pw else
'No password: sign-in is by passkey only. Set one to have both.'}</p>{pw_form}

<h2>API keys</h2><p class="small">For scripts. A key opens only the <code>/api/</code> addresses (status, report,
backups, run a backup), not the website or these settings.</p>
<div class="card"><table><tr><th>Name</th><th>Starts with</th><th>Made</th><th>Last used</th><th></th></tr>
{key_rows}</table></div>
<form method="post" action="/apikeys/new" class="bar" style="margin-top:12px">
<input type="text" name="name" maxlength="60" placeholder="Name, e.g. Home Assistant"><button>Make an API key</button></form>

<h2>Sign-out</h2>
<form method="post" action="/logout-everywhere"><button class="ghost" style="padding:0">Sign out on every device</button></form>
<p class="small" style="margin-top:14px">Forget the saved sign-in for one account (it stays allowed on the account
side until you remove the app there).</p>
<form method="post" action="/signout/microsoft" class="inline"><button class="ghost">Forget Microsoft sign-in</button></form>
<form method="post" action="/signout/google" class="inline"><button class="ghost">Forget Google sign-in</button></form>
{f'<h2>HTTPS certificate</h2><p class="small">This site uses its own private certificate authority. '
 f'<a href="/cert">Install it on another device</a>. CA SHA-256 fingerprint:</p>'
 f'<p class="small break"><code>{esc(cert)}</code></p>' if cert else ''}
{PASSKEY_JS}<script>document.getElementById('pkname').value=guessName();
document.getElementById('addpk').onclick=async()=>{{if(!supported())return;say('Follow your device\'s prompt…');
try{{await addPasskey('',document.getElementById('pkname').value);location.href='/settings?saved=1'}}
catch(e){{say(nice(e),true)}}}};</script>"""
    return page("Settings", "/settings", body)


# ---------- contacts ----------

def _status(p):
    out = []
    if p["ms"] and p["google"]:
        out.append(pill("Both", "ok"))
    elif p["ms"]:
        out.append(pill("Microsoft only", "warn"))
    else:
        out.append(pill("Google only", "warn"))
    if p["ms"] > 1 or p["google"] > 1:
        out.append(pill("Duplicate", "bad"))
    return " ".join(out)


def _match(p, q):
    if not q:
        return True
    hay = " ".join([p["name"] or ""] + [" ".join([c["company"], " ".join(c["emails"]),
                                                  " ".join(x["value"] for x in c["phones"])]) for c in p["members"]])
    digits = "".join(ch for ch in q if ch.isdigit())
    if len(digits) >= 4 and digits in "".join(ch for ch in hay if ch.isdigit() or ch == " ").replace(" ", ""):
        return True
    return all(w in hay.lower() for w in q.lower().split())


CONTACT_FILTERS = [
    ("", "Everyone"),
    ("both", "In both accounts"),
    ("one", "Only in one account"),
    ("dup", "Duplicates"),
    ("no_phone", "No phone"),
    ("no_email", "No email"),
    ("no_addr", "No address"),
    ("addr_gaps", "Address missing city, state or ZIP"),
    ("notes", "Has notes"),
    ("differ", "Copies differ"),
    ("recent", "Changed in the last 30 days"),
]
CONTACT_SORTS = [("name", "Name A–Z"), ("name_desc", "Name Z–A"), ("last", "Last name A–Z"),
                 ("company", "Company"), ("recent", "Recently changed")]


def _p_updated(p):
    return max((c.get("updated") or "" for c in p["members"]), default="")


def _p_filter(p, show):
    cs = p["members"]
    if show == "both":
        return bool(p["ms"] and p["google"])
    if show == "one":
        return not (p["ms"] and p["google"])
    if show == "dup":
        return p["ms"] > 1 or p["google"] > 1
    if show == "no_phone":
        return not any(c["phones"] for c in cs)
    if show == "no_email":
        return not any(c["emails"] for c in cs)
    if show == "no_addr":
        return not any(c.get("addr") or c.get("addresses") for c in cs)
    if show == "addr_gaps":
        return any(not (a.get("city") and a.get("region") and a.get("postal")) for c in cs for a in c.get("addr") or [])
    if show == "notes":
        return any((c.get("notes") or "").strip() for c in cs)
    if show == "differ":
        if len(cs) < 2:
            return False
        norm = lambda c: (c["name"].lower(), sorted(e.lower() for e in c["emails"]),
                          sorted("".join(ch for ch in x["value"] if ch.isdigit())[-10:] for x in c["phones"]),
                          " ".join((c.get("notes") or "").split()), sorted(a.lower() for a in c.get("addresses") or []))
        return len({repr(norm(c)) for c in cs}) > 1
    if show == "recent":
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 30 * 86400))
        return _p_updated(p) >= cutoff
    return True


def _p_sort_key(sort):
    if sort == "last":
        return lambda p: ((next((c["family"] for c in p["members"] if c.get("family")), "") or p["name"] or "~").lower(),
                          (p["name"] or "").lower())
    if sort == "company":
        return lambda p: ((next((c["company"] for c in p["members"] if c.get("company")), "") or "~").lower(),
                          (p["name"] or "").lower())
    return lambda p: (p["name"] or "~").lower()


def contacts_page(r, q="", show="", group="", sort="name"):
    if not r or "people" not in r:
        return page("Contacts", "/contacts", "<h1>Contacts</h1><p class='muted'>No backup with contact data yet. "
                    "Run <b>Back up and build report now</b> on the dashboard.</p>")
    people = [p for p in r["people"] if _match(p, q) and _p_filter(p, show)]
    if group:
        people = [p for p in people if any(group in c.get("groups") or [] for c in p["members"])]
    if sort == "recent":
        people.sort(key=_p_updated, reverse=True)
    else:
        people.sort(key=_p_sort_key(sort), reverse=(sort == "name_desc"))
    rows = []
    for p in people[:1000]:
        c0 = p["members"][0]
        emails = sorted({e.lower() for c in p["members"] for e in c["emails"]})
        phones = []
        for c in p["members"]:
            for x in c["phones"]:
                if x["value"] not in phones:
                    phones.append(x["value"])
        changed = _p_updated(p)[:10]
        rows.append(f"<tr><td><a href='/contacts/{esc(p['key'])}'>{esc(p['name'] or '(no name)')}</a>"
                    f"<div class='muted small'>{esc(c0['company'])}</div></td>"
                    f"<td class='small'>{'<br>'.join(esc(e) for e in emails[:2])}</td>"
                    f"<td class='small num'>{'<br>'.join(esc(x) for x in phones[:2])}</td>"
                    f"<td class='small num'>{esc(changed)}</td><td>{_status(p)}</td></tr>")
    style = "padding:7px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--text)"
    opt = lambda v, name, cur: f"<option value='{esc(v)}'{' selected' if cur == v else ''}>{esc(name)}</option>"
    groups = [g for g, _ in r.get("groups") or []]
    group_sel = ""
    if groups:
        group_sel = (f"<select name='group' style='{style}'>" + opt("", "Any label or category", group)
                     + "".join(opt(g, g, group) for g in groups) + "</select>")
    form = (f"<form method='get' action='/contacts' class='bar'><input type='text' name='q' value='{esc(q)}' class='wide' "
            f"style='max-width:320px' placeholder='Search name, company, email or phone' autofocus>"
            f"<select name='show' style='{style}'>" + "".join(opt(v, n, show) for v, n in CONTACT_FILTERS) + "</select>"
            + group_sel
            + f"<select name='sort' style='{style}'>" + "".join(opt(v, n, sort) for v, n in CONTACT_SORTS) + "</select>"
            "<button>Show</button>" + ("<a href='/contacts'>clear</a>" if q or show or group or sort != "name" else "")
            + "</form>")
    body = (f"<h1>Contacts</h1><p class='sub'>From the backup of {when(r.get('created'))}, kept up to date as you edit. "
            f"Click a name to see both copies, edit or delete.</p>{form}"
            f"<p class='muted small'>{len(people)} of {len(r['people'])} people"
            + (" (showing the first 1000)" if len(people) > 1000 else "") + "</p>"
            "<div class='card'><table><tr><th>Name</th><th>Email</th><th>Phone</th><th>Changed</th><th>In</th></tr>"
            + "".join(rows) + "</table></div>")
    return page("Contacts", "/contacts", body)


FIELDS = [("Name", lambda c: c["name"]), ("Shown as", lambda c: c.get("shown_as", "")),
          ("First / last", lambda c: " / ".join(x for x in (c["given"], c["family"]) if x)),
          ("Company", lambda c: c["company"]), ("Job title", lambda c: c["title"]),
          ("Emails", lambda c: "\n".join(c["emails"])),
          ("Phones", lambda c: "\n".join(f"{x['value']}  ({x['type']})" for x in c["phones"])),
          ("Addresses", lambda c: "\n".join(c["addresses"])), ("Birthday", lambda c: c["birthday"]),
          ("Groups", lambda c: ", ".join(c["groups"])), ("Notes", lambda c: c["notes"]),
          ("Last changed", lambda c: (c.get("updated") or "")[:16].replace("T", " "))]


def contact_page(p, r, history=(), msg="", error="", write_mode="off"):
    cols = p["members"]
    head = "".join(f"<th>{pill(*SIDE[c['side']])}</th>" for c in cols)
    rows = []
    for label, get in FIELDS:
        vals = [get(c) or "" for c in cols]
        if not any(vals):
            continue
        norm = {" ".join(v.lower().split()) for v in vals}
        differ = len(cols) > 1 and len(norm) > 1
        cls = ' class="diff"' if differ else ""
        tds = "".join(f"<td style='white-space:pre-wrap'{cls}>{esc(v)}</td>" for v in vals)
        rows.append(f"<tr><th style='text-transform:none;font-size:13px'>{esc(label)}</th>{tds}</tr>")
    notes = []
    keys = {(c["side"], c["id"]) for c in cols}
    for sec, title in (("import_blocks", "Has a “Contact Imported” block in its notes (removed by the cleanup)"),
                       ("name_order", "Name stored as “Last, First” (fixed by the cleanup)")):
        if any((i["contact"]["side"], i["contact"]["id"]) in keys for i in r.get(sec, [])):
            notes.append(f"<li>{title}</li>")
    for s in r.get("shared_info", []):
        if any((c["side"], c["id"]) in keys for c in s["contacts"]):
            others = [c["name"] for c in s["contacts"] if (c["side"], c["id"]) not in keys]
            notes.append(f"<li>Shares {esc(s['kind'])} {esc(s['value'])} with {esc(', '.join(sorted(set(others))))} "
                         "(not merged)</li>")
    key = esc(p["key"])
    dup = p["ms"] > 1 or p["google"] > 1
    confirm_delete = ("return confirm('Delete this contact from both Microsoft 365 and Google? It goes to the "
                      "recycle bin for 2 years and can be restored.')")
    if write_mode != "off" and not dup:
        actions = (f'<div class="bar" style="margin:14px 0"><a class="btn" href="/contacts/{key}/edit">Edit</a>'
                   f'<form method="post" action="/contacts/{key}/delete" class="inline" onsubmit="{confirm_delete}">'
                   f'<button class="ghost" style="color:var(--bad)">Delete from both</button></form></div>')
    elif dup:
        actions = '<p class="small muted">Duplicate copies on one side: edit and delete wait until they are merged.</p>'
    else:
        actions = '<p class="small muted">Writing is off (Settings → Write mode).</p>'
    hist = ""
    if history:
        hrows = []
        confirm_restore = "return confirm('Put this earlier version back on both sides?')"
        for h in history:
            btn = ""
            if h["has_before"] and h["action"] in ("edit", "restore", "sync") and write_mode != "off" and not dup:
                btn = (f'<form method="post" action="/contacts/{key}/history/{h["id"]}/restore" class="inline" '
                       f'onsubmit="{confirm_restore}"><button class="ghost">Restore this version</button></form>')
            hrows.append(f"<tr><td class='num'>{when(h['ts'])}</td><td>{esc(h['action'])}</td>"
                         f"<td>{pill(*SIDE[h['side']])}</td><td class='small'>{esc(h['note'] or '')}</td><td>{btn}</td></tr>")
        hist = ("<h2>History</h2><p class='muted small'>“Restore this version” puts the contact back the way it was "
                "<b>before</b> that change.</p><div class='card'><table><tr><th>When</th><th>Change</th><th>Side</th>"
                "<th>Note</th><th></th></tr>" + "".join(hrows) + "</table></div>")
    banner_notes = ('<div class="banner warnb"><ul style="margin:0;padding-left:18px">' + "".join(notes)
                    + "</ul></div>") if notes else ""
    body = (f'<p><a href="/contacts">← Contacts</a></p><h1>{esc(p["name"] or "(no name)")}</h1>'
            f'<p class="sub">{_status(p)}</p>'
            + (f'<div class="banner">{esc(msg)}</div>' if msg else "")
            + (f'<div class="banner err">{esc(error)}</div>' if error else "")
            + banner_notes + actions
            + f'<div class="card"><table><tr><th></th>{head}</tr>{"".join(rows)}</table></div>'
            + '<p class="muted small">Fields that differ between the copies are highlighted.</p>' + hist
            + "<style>td.diff{background:var(--warnbg)}</style>")
    return page(p["name"] or "Contact", "/contacts", body)


PHONE_TYPES = ("mobile", "home", "work", "main", "other")


def contact_edit_page(p, rec, error="", write_mode="test"):
    def v(k):
        return esc(rec.get(k) or "")
    key = esc(p["key"])
    phones = "\n".join(f"{x['value']} | {x.get('type') or 'other'}" for x in rec.get("phones") or [])
    emails = "\n".join(rec.get("emails") or [])
    by_type = {a.get("type"): a for a in rec.get("addr") or []}
    addr_html = ""
    for t, label in (("home", "Home"), ("work", "Work / business"), ("other", "Other")):
        a = by_type.get(t, {})
        addr_html += (
            f'<div class="card pad"><b>{label} address</b>'
            f'<label>Street</label><textarea name="{t}_street" rows="2" style="width:100%">{esc(a.get("street", ""))}</textarea>'
            f'<div class="bar"><div><label>City</label><input type="text" name="{t}_city" value="{esc(a.get("city", ""))}"></div>'
            f'<div><label>State</label><input type="text" name="{t}_region" value="{esc(a.get("region", ""))}" style="width:80px"></div>'
            f'<div><label>ZIP</label><input type="text" name="{t}_postal" value="{esc(a.get("postal", ""))}" style="width:100px"></div>'
            f'<div><label>Country</label><input type="text" name="{t}_country" value="{esc(a.get("country", ""))}" '
            f'style="width:120px"></div></div></div>')
    sides = " and ".join("Microsoft 365" if c["side"] == "microsoft" else "Google" for c in p["members"])
    test_note = ('<div class="banner warnb">Write mode is “test”: only contacts named “Contact-Sync Test…” can be '
                 'saved.</div>') if write_mode == "test" else ""
    body = (
        f'<p><a href="/contacts/{key}">← {esc(p["name"] or "Contact")}</a></p><h1>Edit contact</h1>'
        f'<p class="sub">Saved to {esc(sides)}. The current version is kept in the history first, so this can be undone.</p>'
        + (f'<div class="banner err">{esc(error)}</div>' if error else "") + test_note
        + f'<form method="post" action="/contacts/{key}/edit">'
        f'<div class="bar"><div><label>First</label><input type="text" name="given" value="{v("given")}"></div>'
        f'<div><label>Middle</label><input type="text" name="middle" value="{v("middle")}"></div>'
        f'<div><label>Last</label><input type="text" name="family" value="{v("family")}"></div></div>'
        f'<div class="bar"><div><label>Company</label><input type="text" name="company" value="{v("company")}" style="width:260px"></div>'
        f'<div><label>Job title</label><input type="text" name="title" value="{v("title")}" style="width:220px"></div></div>'
        f'<label>Emails</label><textarea name="emails" rows="3" style="width:100%;max-width:480px">{esc(emails)}</textarea>'
        f'<div class="hint">One per line. Outlook keeps up to 3.</div>'
        f'<label>Phones</label><textarea name="phones" rows="4" style="width:100%;max-width:480px">{esc(phones)}</textarea>'
        f'<div class="hint">One per line as <code>number | type</code>; type is usually one of {", ".join(PHONE_TYPES)} (other labels are kept).</div>'
        f'<div class="grid2" style="margin-top:14px">{addr_html}</div>'
        f'<label>Birthday</label><input type="text" name="birthday" value="{v("birthday")}" placeholder="YYYY-MM-DD or --MM-DD">'
        f'<label>Notes</label><textarea name="notes" rows="6" style="width:100%">{v("notes")}</textarea>'
        f'<p><button>Save to both</button> <a href="/contacts/{key}" style="margin-left:12px">Cancel</a></p></form>'
        "<style>textarea{padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:var(--bg);"
        "color:var(--text);font:inherit}</style>")
    return page("Edit", "/contacts", body)


def parse_edit_form(f, validate=True):
    """Form fields -> editable record (see model.EDIT_FIELDS). Raises ValueError for bad input,
    unless validate=False (used to show the form again with what was typed)."""
    if not validate:
        def bad(msg):
            return None
    else:
        def bad(msg):
            raise ValueError(msg)
    rec = {k: (f(k) or "").strip() for k in ("given", "middle", "family", "company", "title", "birthday")}
    rec["notes"] = (f("notes") or "").replace("\r\n", "\n").strip()
    rec["emails"] = [e.strip() for e in (f("emails") or "").splitlines() if e.strip()]
    for e in rec["emails"]:
        if "@" not in e or " " in e:
            bad(f"“{e}” doesn't look like an email address.")
    rec["phones"] = []
    for line in (f("phones") or "").splitlines():
        if not line.strip():
            continue
        num, _, typ = line.partition("|")
        typ = typ.strip().lower() or "mobile"
        if not re.fullmatch(r"[a-z][a-z0-9 \-]{0,29}", typ):      # custom labels (Google) are fine
            bad(f"Phone type “{typ}” should be a short word like {', '.join(PHONE_TYPES)}.")
        rec["phones"].append({"value": num.strip(), "type": typ})
    rec["addr"] = []
    for t in ("home", "work", "other"):
        a = {"type": t, "street": (f(f"{t}_street") or "").replace("\r\n", "\n").strip()}
        a.update({k: (f(f"{t}_{k}") or "").strip() for k in ("city", "region", "postal", "country")})
        if any(a[k] for k in ("street", "city", "region", "postal", "country")):
            rec["addr"].append(a)
    b = rec["birthday"]
    if b and not (re.fullmatch(r"\d{4}-\d{2}-\d{2}", b) or re.fullmatch(r"--\d{2}-\d{2}", b)):
        bad("Birthday must look like 1980-02-03, or --02-03 without a year.")
    if not (rec["given"] or rec["family"] or rec["company"]):
        bad("Give a first name, last name or company.")
    return rec


def _bin_copies(it):
    """The saved copies of a recycle-bin entry in the common shape (import here: web stays light)."""
    import model
    out = [model.from_graph(r) for r in json.loads(it["ms_raw"] or "[]")]
    out += [model.from_people(r, {}) for r in json.loads(it["g_raw"] or "[]")]
    return out


def _bin_text(it):
    """Everything searchable about a bin entry."""
    bits = [it["name"] or "", it.get("reason") or ""]
    for c in _bin_copies(it):
        bits += [c["name"], c["company"], " ".join(c["emails"]), " ".join(p["value"] for p in c["phones"])]
    return " ".join(bits)


def bin_page(items, msg="", error="", write_mode="off", q="", show="", sort="new"):
    total = len(items)
    if q:
        digits = "".join(ch for ch in q if ch.isdigit())
        def hit(it):
            text = _bin_text(it)
            if len(digits) >= 4 and digits in "".join(ch for ch in text if ch.isdigit()):
                return True
            return all(w in text.lower() for w in q.lower().split())
        items = [it for it in items if hit(it)]
    has = lambda it, k: bool(json.loads(it[k] or "[]"))
    filters = {
        "waiting": lambda it: not it["restored"], "restored": lambda it: bool(it["restored"]),
        "both": lambda it: has(it, "ms_raw") and has(it, "g_raw"),
        "one": lambda it: has(it, "ms_raw") != has(it, "g_raw"),
        "photo": lambda it: bool(it.get("has_photo")),
        "sync": lambda it: (it.get("reason") or "").startswith("Sync"),
        "cleanup": lambda it: (it.get("reason") or "").startswith("Cleanup"),
        "page": lambda it: (it.get("reason") or "").startswith("Deleted from"),
    }
    if show in filters:
        items = [it for it in items if filters[show](it)]
    sorts = {"new": (lambda it: -it["ts"]), "old": (lambda it: it["ts"]),
             "name": (lambda it: (it["name"] or "").lower()), "name_desc": None}
    if sort == "name_desc":
        items = sorted(items, key=lambda it: (it["name"] or "").lower(), reverse=True)
    else:
        items = sorted(items, key=sorts.get(sort) or sorts["new"])
    rows = []
    for it in items:
        sides = []
        if has(it, "ms_raw"):
            sides.append(pill(*SIDE["microsoft"]))
        if has(it, "g_raw"):
            sides.append(pill(*SIDE["google"]))
        if it.get("has_photo"):
            sides.append(pill("photo", "mute"))
        if it["restored"]:
            act = f"<span class='muted small'>Restored {when(it['restored'])}</span>"
        elif write_mode == "off":
            act = "<span class='muted small'>Writing is off</span>"
        else:
            act = (f"<form method='post' action='/bin/{it['id']}/restore' class='inline'>"
                   f"<button class='ghost'>Restore to both</button></form>")
        rows.append(f"<tr><td><a href='/bin/{it['id']}'>{esc(it['name'])}</a>"
                    f"<div class='muted small'>{esc(it.get('reason') or '')}</div></td>"
                    f"<td class='num'>{when(it['ts'])}</td><td>{' '.join(sides)}</td><td>{act}</td></tr>")
    sel = lambda name, v, cur: f"<option value='{v}'{' selected' if cur == v else ''}>{name}</option>"
    style = "padding:7px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--text)"
    form = (f"<form method='get' action='/bin' class='bar' style='margin:10px 0'>"
            f"<input type='text' name='q' value='{esc(q)}' placeholder='Search name, company, email, phone or reason' "
            f"style='max-width:340px' class='wide'>"
            f"<select name='show' style='{style}'>" + sel("Everything", "", show) + sel("Not restored", "waiting", show)
            + sel("Restored", "restored", show) + sel("Had copies in both", "both", show)
            + sel("Only in one account", "one", show) + sel("With a photo", "photo", show)
            + sel("Deleted by the sync", "sync", show) + sel("Removed by the cleanup", "cleanup", show)
            + sel("Deleted from the Contacts page", "page", show) + "</select>"
            f"<select name='sort' style='{style}'>" + sel("Newest first", "new", sort) + sel("Oldest first", "old", sort)
            + sel("Name A–Z", "name", sort) + sel("Name Z–A", "name_desc", sort) + "</select>"
            "<button>Show</button>" + ("<a href='/bin'>clear</a>" if q or show or sort != "new" else "") + "</form>")
    body = ("<h1>Recycle bin</h1><p class='sub'>Contacts deleted through contact-sync, with both sides' data. Kept 2 "
            "years. Click a name to see everything that was saved. Restoring creates the contact again in Microsoft "
            "365 and Google (everything, the photo and labels included, comes back as it was).</p>"
            + (f'<div class="banner">{esc(msg)}</div>' if msg else "")
            + (f'<div class="banner err">{esc(error)}</div>' if error else "")
            + form + f"<p class='muted small'>{len(items)} of {total}</p>"
            + _section("items", "Deleted contacts", "", rows, ["Name", "Deleted", "Had copies in", ""]))
    return page("Recycle bin", "/bin", body)


def bin_detail_page(it, write_mode="off"):
    cols = _bin_copies(it)
    head = "".join(f"<th>{pill(*SIDE[c['side']])}</th>" for c in cols)
    photos = ""
    for side, key in (("microsoft", "ms_photo"), ("google", "g_photo")):
        if it.get(key):
            import base64 as _b
            data = _b.b64decode(it[key])
            ctype = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
            photos += (f"<figure style='display:inline-block;margin:0 16px 0 0;text-align:center'>"
                       f"<img src='data:{ctype};base64,{it[key]}' alt='' style='width:120px;height:120px;"
                       f"object-fit:cover;border-radius:12px;border:1px solid var(--line)'>"
                       f"<figcaption>{pill(*SIDE[side])}</figcaption></figure>")
    rows = []
    for label, get in FIELDS:
        vals = [get(c) or "" for c in cols]
        if not any(vals):
            continue
        differ = len(cols) > 1 and len({" ".join(v.lower().split()) for v in vals}) > 1
        cls = ' class="diff"' if differ else ""
        rows.append(f"<tr><th style='text-transform:none;font-size:13px'>{esc(label)}</th>"
                    + "".join(f"<td style='white-space:pre-wrap'{cls}>{esc(v)}</td>" for v in vals) + "</tr>")
    if it["restored"]:
        act = f"<p class='muted'>Restored {when(it['restored'])}.</p>"
    elif write_mode == "off":
        act = "<p class='muted'>Writing is off (Settings → Write mode).</p>"
    else:
        act = (f"<form method='post' action='/bin/{it['id']}/restore'><button>Restore to both</button></form>")
    body = (f"<p><a href='/bin'>← Recycle bin</a></p><h1>{esc(it['name'])}</h1>"
            f"<p class='sub'>Deleted {when(it['ts'])}" + (f" · {esc(it.get('reason'))}" if it.get("reason") else "")
            + "</p>" + (f"<div style='margin:12px 0'>{photos}</div>" if photos else "") + act
            + (f"<div class='card'><table><tr><th></th>{head}</tr>{''.join(rows)}</table></div>" if cols else
               "<p class='muted'>No saved data.</p>")
            + "<p class='muted small'>This is the copy saved when it was deleted. Fields that differ between the two "
              "accounts are highlighted.</p><style>td.diff{background:var(--warnbg)}</style>")
    return page(it["name"] or "Deleted contact", "/bin", body)


# ---------- sync ----------

FIELD_LABEL = {"given": "first name", "middle": "middle name", "family": "last name", "company": "company",
               "title": "job title", "emails": "emails", "phones": "phones", "addr": "addresses",
               "birthday": "birthday", "notes": "notes"}


def _short(v):
    try:
        v = json.loads(v)
    except (TypeError, ValueError):
        pass
    if isinstance(v, list):
        v = ", ".join(x.get("value") if isinstance(x, dict) and "value" in x else
                      (", ".join(str(y) for y in x.values() if y) if isinstance(x, dict) else str(x)) for x in v)
    v = str(v or "(empty)")
    return v if len(v) <= 80 else v[:77] + "…"


def sync_page(st, runs, conflicts, write_mode, msg="", error=""):
    b = ["<h1>Sync</h1>"]
    if msg:
        b.append(f'<div class="banner">{esc(msg)}</div>')
    if error:
        b.append(f'<div class="banner err">{esc(error)}</div>')
    if write_mode == "off":
        b.append('<div class="banner warnb">Write mode is off (Settings), so sync can\'t change anything.</div>')
    elif write_mode == "test":
        b.append('<div class="banner warnb">Write mode is “test”: only contacts named “Contact-Sync Test…” are '
                 'synced. Everything else is counted as skipped until you switch it to “on” in Settings.</div>')
    if not st["started"]:
        b.append("""<p class="sub">Sync isn't running yet. Starting it links every contact that is already the same
person in both accounts (same email or phone, matching name). Contacts that are only on one side, or duplicated,
are left alone until the cleanup; contacts you add after today are copied to the other side.</p>
<ul class="small"><li>A change on one side is copied to the other.</li>
<li>Changed on both: merged field by field; if the same field changed on both, the newer edit wins and the other
value is kept in the conflict list below.</li>
<li>Deleted on one side: deleted on the other too. Both copies go to the recycle bin (kept 2 years) first.</li></ul>
<form method="post" action="/sync/start"><button>Start syncing</button></form>""")
        return page("Sync", "/sync", "".join(b))
    last = st["last"]
    state = pill("Running", "info") if st["running"] else (pill("On", "ok") if st["enabled"] else pill("Paused", "warn"))
    b.append(f'<p class="sub">{state} · {st["linked"]} contacts linked · every {st["minutes"]} minutes'
             + (f' · last run {when(last["started"])}' if last else "") + "</p>")
    if st["running"]:
        b.append(f'<div class="banner">{esc(st["step"])}…</div>')
    if last and last.get("blocked"):
        b.append(f'<div class="banner err">{esc(last["blocked"])}<form method="post" action="/sync/allow-deletes" '
                 'style="margin-top:8px"><button>Allow these deletes and run now</button></form></div>')
    if last and last.get("error"):
        b.append(f'<div class="banner err">Last run: {esc(last["error"])}</div>')
    dis = " disabled" if st["running"] else ""
    b.append('<div class="bar">'
             f'<form method="post" action="/sync/run" class="inline"><button{dis}>Sync now</button></form>'
             + (f'<form method="post" action="/sync/pause" class="inline"><button class="ghost">Pause</button></form>'
                if st["enabled"] else
                f'<form method="post" action="/sync/resume" class="inline"><button class="ghost">Resume</button></form>')
             + "</div>")
    rows = []
    for r in runs:
        lines = json.loads(r.get("log") or "[]")
        detail = ("<details><summary>" + f"{len(lines)} change{'s' if len(lines) != 1 else ''}</summary><ul class='small'>"
                  + "".join(f"<li>{esc(x)}</li>" for x in lines) + "</ul></details>") if lines else ""
        status = pill("Failed", "bad") if r.get("error") else (pill("Held deletes", "warn") if r.get("blocked") else pill("OK", "ok"))
        rows.append(f"<tr><td class='num'>{when(r['started'])}</td><td>{esc(r['trigger'])}</td><td>{status}</td>"
                    f"<td class='num'>{r['created'] or 0}</td><td class='num'>{r['updated'] or 0}</td>"
                    f"<td class='num'>{r['deleted'] or 0}</td><td class='num'>{r['conflicts'] or 0}</td>"
                    f"<td class='num'>{r['skipped'] or 0}</td><td>{detail}</td></tr>")
    b.append(_section("runs", "Recent runs", "", rows,
                      ["When", "Why", "Result", "Copied", "Updated", "Deleted", "Conflicts", "Skipped", "Details"]))
    rows = [f"<tr><td class='num'>{when(cf['ts'])}</td><td>{esc(cf['name'])}</td>"
            f"<td>{esc(FIELD_LABEL.get(cf['field'], cf['field']))}</td>"
            f"<td>{pill(*SIDE[cf['kept_side']])} {esc(_short(cf['kept_value']))}</td>"
            f"<td>{pill(*SIDE[cf['lost_side']])} {esc(_short(cf['lost_value']))}</td></tr>" for cf in conflicts]
    b.append(_section("conflicts", "Conflicts", "The same field changed in both accounts; the newer edit was kept. "
                      "To use the other value, open the contact and restore that version from its history, or edit it.",
                      rows, ["When", "Contact", "Field", "Kept", "Replaced"]))
    b.append(f"""<h2>Sync settings</h2><form method="post" action="/sync/settings">
<label>Run every (minutes)</label><input type="number" name="sync_minutes" min="5" max="1440" value="{st['minutes']}">
<label>Hold back deletes when more than this many happen in one run</label>
<input type="number" name="sync_max_deletes" min="0" max="1000" value="{st['max_deletes']}">
<div class="hint">A safety stop: a big batch of deletes waits for you to allow it here.</div>
<p><button>Save</button></p></form>""")
    return page("Sync", "/sync", "".join(b), refresh=False)


# ---------- cleanup ----------

KIND_TITLE = {
    "tidy": ("Tidy up contacts", "Remove “Contact Imported” notes blocks, “Last, First” → “First Last”, phones as "
             "(559) 555-1234, lowercase emails, fix codes like &amp;amp;."),
    "combine": ("Combine copies of the same person", "Every email, phone, address and note is kept; one copy per "
                "account remains and extra copies go to the recycle bin; the two accounts are linked for sync. "
                "Unticked ones need a look: the names differ."),
    "shared": ("Different names with the same phone or email", "Usually a household line or a business and its "
               "staff, so each starts as “keep them separate”. To take the number or email off someone, tick "
               "them (it's removed from both their Microsoft and Google copies; their other numbers stay). "
               "“Combine” merges them into one contact like the section above."),
    "address": ("Fix addresses", "Addresses typed into the street box are split into city, state and ZIP; a "
                "missing state comes from the ZIP; a missing ZIP, city or state is looked up with the US Census "
                "Bureau (only the street, city and state are sent). Ticked ones are confident; the boxes are "
                "editable, so you can correct any of them or fill in an unticked one by hand and tick it."),
    "copy": ("Copy contacts that are only in one account", "Created in the other account and linked for sync."),
    "label": ("Remove a Google label", "Only the label goes; its contacts keep everything else."),
    "category": ("Copy a Google label to Outlook", "Adds an Outlook category to the Outlook copies of its members."),
}


def _name_editor(i):
    """Pick one of the copies' names (fills the boxes) and/or edit the boxes: they become the name."""
    n, opts = i["id"], i.get("names") or []
    if not opts:
        return ""
    first = opts[0]
    radios = "".join(
        f"<label class='check' style='margin:2px 0;font-weight:500'><input type='radio' name='pick_{n}'"
        f"{' checked' if k == 0 else ''} onclick=\"nm({n},{json.dumps(o).replace(chr(34), '&quot;')})\"> "
        f"{esc(o['label'])}</label>" for k, o in enumerate(opts))
    box = lambda f, ph, w: (f"<input type='text' name='{f}_{n}' id='{f}_{n}' value='{esc(first.get(f, ''))}' "
                            f"placeholder='{ph}' style='width:{w}px'>")
    return (f"<details style='margin-top:6px'><summary class='small'>Name: <b id='lbl_{n}'>{esc(first['label'])}"
            f"</b> (choose or edit)</summary>{radios}<div class='bar' style='margin-top:6px'>"
            f"{box('given', 'First', 120)}{box('middle', 'Middle', 90)}{box('family', 'Last', 140)}"
            f"{box('company', 'Company', 180)}</div><div class='hint'>Pick a name to fill the boxes, then change "
            f"anything. The boxes are what's saved.</div></details>")


NAME_JS = """<script>function nm(n,o){for(const f of ['given','middle','family','company']){
const e=document.getElementById(f+'_'+n);if(e)e.value=o[f]||''}document.getElementById('lbl_'+n).textContent=o.label}
document.addEventListener('input',e=>{const m=(e.target.id||'').match(/^(given|middle|family|company)_(\\d+)$/);if(!m)return;
const n=m[2],v=f=>(document.getElementById(f+'_'+n)||{}).value||'';
document.getElementById('lbl_'+n).textContent=[v('given'),v('middle'),v('family')].filter(x=>x).join(' ')||v('company')})
</script>"""


SAVE_CHOICES_JS = """<script>(function(){
// Keep the owner's ticks, choices and name edits in this browser, per plan, so a reload or a trip to
// Settings doesn't lose them. Cleared once the plan has been applied (the page then shows results).
const key='cs-cleanup-PLANID', f=document.getElementById('cf');if(!f)return;
const save=()=>{try{const s={};for(const e of f.elements){if(!e.name||e.type==='hidden'||e.type==='submit')continue;
const k=e.type==='checkbox'||e.type==='radio'?e.name+'|'+e.value:e.name;
s[k]=(e.type==='checkbox'||e.type==='radio')?e.checked:e.value}localStorage.setItem(key,JSON.stringify(s))}catch(x){}};
try{const s=JSON.parse(localStorage.getItem(key)||'null');if(s){for(const e of f.elements){if(!e.name)continue;
const k=e.type==='checkbox'||e.type==='radio'?e.name+'|'+e.value:e.name;if(!(k in s))continue;
if(e.type==='checkbox'||e.type==='radio')e.checked=s[k];else e.value=s[k];
if(/^(given|middle|family|company)_/.test(e.name))e.dispatchEvent(new Event('input',{bubbles:true}))}
const n=document.createElement('div');n.className='banner';n.textContent='Your earlier choices on this plan were restored.';
f.parentNode.insertBefore(n,f)}}catch(x){}
f.addEventListener('change',save);f.addEventListener('input',save);
})()</script>"""


REVIEW_PILL = ' <span class="pill p-warn rv">Review</span>'    # reads "Reviewed" once ticked (CSS)


def _address_rows(items):
    """Each address: before, and editable boxes holding the proposed fix."""
    rows = []
    for i in items:
        n, a, b = i["id"], i.get("after") or {}, i.get("before") or {}
        before = ", ".join(x for x in ((b.get("street") or "").replace("\n", " / "), b.get("city"), b.get("region"),
                                       b.get("postal"), b.get("country")) if x)
        who = " ".join(pill(*SIDE[m["side"]]) for m in i["members"])
        box = lambda k, ph, w: (f"<input type='text' name='addr_{n}_{k}' value='{esc(a.get(k, ''))}' placeholder='{ph}' "
                                f"style='width:{w}px' onchange=\"document.getElementById('a{n}').checked=true\">")
        # a fix was found but it isn't sure (ZIP correction, street suggestion, guess): yellow, for review
        review = not i["default"] and any((a.get(k) or "") != (b.get(k) or "")
                                          for k in ("street", "city", "region", "postal", "country"))
        rows.append(
            f"<tr{' class=review' if review else ''}><td><input type='checkbox' name='item' value='{n}' id='a{n}' "
            f"class='k-address'{' checked' if i['default'] else ''}></td><td>{esc(i['title'])} {who}"
            f"{REVIEW_PILL if review else ''}"
            f"<div class='muted small'>{esc((i.get('addr_type') or '').title())} · was: {esc(before) or '(empty)'}</div>"
            f"<textarea name='addr_{n}_street' rows='{max(1, (a.get('street') or '').count(chr(10)) + 1)}' "
            f"style='width:100%;max-width:420px;margin-top:4px' onchange=\"document.getElementById('a{n}').checked=true\">"
            f"{esc(a.get('street', ''))}</textarea><div class='bar'>{box('city', 'City', 140)}{box('region', 'State', 60)}"
            f"{box('postal', 'ZIP', 90)}{box('country', 'Country', 120)}</div></td>"
            f"<td class='small'>{esc(i['detail'])}</td></tr>")
    return ("<div class='card'><table><tr><th></th><th>Address (editable)</th><th>What was done</th></tr>"
            + "".join(rows) + "</table></div>")


def _shared_cards(items):
    """One card per shared phone/email: keep / remove from ticked people / combine."""
    out = []
    for i in items:
        n = i["id"]
        what = "number" if i.get("kind_of") == "phone" else "email"
        people = ""
        for k, p in enumerate(i.get("people") or []):
            sides = " ".join(pill(*SIDE[m["side"]]) for m in p["members"])
            people += (f"<label class='check' style='font-weight:500'><input type='checkbox' name='strip_{n}' "
                       f"value='{k}' onchange=\"if(this.checked)document.getElementById('s{n}strip').checked=true\">"
                       f"Remove {esc(i.get('shown'))} from <b>{esc(p['name'])}</b> {sides}</label>")
        out.append(
            f"<div class='card pad' style='margin-bottom:10px'><b>{esc(i.get('shown'))}</b> "
            f"<span class='muted'>on {esc(i['title'])}</span>"
            f"<div class='bar' style='margin:8px 0'>"
            f"<label class='check' style='margin:0'><input type='radio' name='shared_{n}' value='keep' checked> "
            f"Keep them separate, leave as is</label>"
            f"<label class='check' style='margin:0'><input type='radio' name='shared_{n}' value='strip' id='s{n}strip'> "
            f"Remove the {what} from the people ticked below</label>"
            f"<label class='check' style='margin:0'><input type='radio' name='shared_{n}' value='combine'> "
            f"Combine them into one contact</label></div>{people}{_name_editor(i)}</div>")
    return "".join(out)


def cleanup_page(plan, st, write_mode, msg="", error=""):
    b = ["<h1>Cleanup</h1>"]
    if write_mode != "on" and not st["running"]:
        b.append('<div class="banner err"><b>Nothing on this page can be applied until write mode is On.</b> '
                 'Switch it first in <a href="/settings" target="_blank" rel="noopener">Settings → Write mode</a> '
                 '(the cleanup changes real contacts). You can still review and tick now: your choices are kept in '
                 'this browser, even if you reload the page.</div>')
    if msg:
        b.append(f'<div class="banner">{esc(msg)}</div>')
    if error:
        b.append(f'<div class="banner err">{esc(error)}</div>')
    if st.get("build_error"):
        b.append(f'<div class="banner err">Building the plan failed: {esc(st["build_error"])}</div>')
    if st.get("building"):
        b.append(f'<div class="banner" id="prog">Building the plan: {esc(st.get("build_step") or "starting")}… '
                 'This page refreshes by itself.</div><script>setTimeout(()=>location.reload(),3000)</script>')
        return page("Cleanup", "/cleanup", "".join(b))
    if st["running"]:
        pct = int(100 * st["done"] / max(st["total"], 1))
        b.append(f'<div class="banner" id="prog">Working: {st["done"]} of {st["total"]} ({pct}%) — {esc(st["step"])}</div>'
                 '<ul class="small">' + "".join(f"<li>{esc(x)}</li>" for x in st["log"][-15:]) + "</ul>"
                 "<script>setTimeout(()=>location.reload(),4000)</script>")
        return page("Cleanup", "/cleanup", "".join(b))
    build = ('<form method="post" action="/cleanup/plan" class="inline"><button class="ghost">'
             + ("Rebuild the plan from the accounts now" if plan else "Build the cleanup plan") + "</button></form>")
    if not plan:
        b.append("<p class='sub'>Reads both accounts and lists every change the cleanup would make. Nothing is "
                 "changed until you tick what you want and press Apply.</p>" + build)
        return page("Cleanup", "/cleanup", "".join(b))
    res = plan.get("result")
    if plan.get("applied") and res:
        b.append(f"<h2>Applied {when(plan['applied'])}</h2><p>{res['ok']} done, {res['skipped']} skipped, "
                 f"{res['failed']} failed.</p><details><summary>What happened</summary><ul class='small'>"
                 + "".join(f"<li>{esc(x)}</li>" for x in res["log"]) + "</ul></details>")
        b.append(f"<p>{build}</p>")
        return page("Cleanup", "/cleanup", "".join(b))
    b.append(f"<p class='sub'>Plan made {when(plan['created'])} from both accounts. A backup is taken before "
             f"anything changes, and every change is kept in the contact's history (or the recycle bin), so it can "
             f"be undone.</p><p>{build}</p>")
    b.append(f'<form method="post" action="/cleanup/apply" id="cf"><input type="hidden" name="plan_id" '
             f'value="{plan["plan_id"]}">')
    for kind in ("tidy", "address", "combine", "shared", "copy", "label", "category"):
        items = [i for i in plan["items"] if i["kind"] == kind]
        if not items:
            continue
        title, intro = KIND_TITLE[kind]
        if kind == "address":
            on = sum(1 for i in items if i["default"])
            rev = sum(1 for i in items if not i["default"] and any(
                ((i.get("after") or {}).get(k) or "") != ((i.get("before") or {}).get(k) or "")
                for k in ("street", "city", "region", "postal", "country")))
            b.append(f"<h2>{esc(title)} <span class='muted'>({on} of {len(items)} ticked)</span></h2>"
                     + (f"<p class='small'>{pill(f'{rev} to review', 'warn')} yellow rows have a fix that isn't "
                        "certain: check it, then tick it.</p>" if rev else "") +
                     f"<p class='muted small'>{intro} <a href='#' onclick=\"document.querySelectorAll('.k-address')"
                     f".forEach(x=>x.checked=true);return false\">tick all</a> · <a href='#' onclick=\"document"
                     f".querySelectorAll('.k-address').forEach(x=>x.checked=false);return false\">untick all</a></p>")
            b.append(_address_rows(items))
            continue
        if kind == "shared":
            b.append(f"<h2>{esc(title)} <span class='muted'>({len(items)})</span></h2><p class='muted small'>{intro}</p>")
            b.append(_shared_cards(items))
            continue
        on = sum(1 for i in items if i["default"])
        rows = []
        for i in items:
            who = " ".join(pill(*SIDE[m["side"]]) for m in i["members"][:6]) or ""
            names = ", ".join(sorted({m["name"] for m in i["members"] if m["name"]}))
            look = (f" · <a href='/contacts?q={urllib.parse.quote(i['members'][0]['name'] or '')}' target='_blank'>"
                    f"see the copies</a>") if kind == "combine" and i["members"] else ""
            rows.append(f"<tr><td><input type='checkbox' name='item' value='{i['id']}' class='k-{kind}'"
                        f"{' checked' if i['default'] else ''}></td><td>{esc(i['title'])}"
                        + (f"<div class='muted small'>{esc(names)}</div>" if names and names != i['title'] else "")
                        + (_name_editor(i) if kind == "combine" else "")
                        + f"</td><td>{who}</td><td class='small'>{esc(i['detail'])}{look}</td></tr>")
        b.append(f"<h2>{esc(title)} <span class='muted'>({on} of {len(items)} ticked)</span></h2>"
                 f"<p class='muted small'>{intro} <a href='#' onclick=\"document.querySelectorAll('.k-{kind}')"
                 f".forEach(x=>x.checked=true);return false\">tick all</a> · <a href='#' onclick=\"document"
                 f".querySelectorAll('.k-{kind}').forEach(x=>x.checked=false);return false\">untick all</a></p>"
                 "<div class='card'><table><tr><th></th><th>Contact</th><th>In</th><th>What changes</th></tr>"
                 + "".join(rows) + "</table></div>")
    off = "" if write_mode == "on" else ("<span class='small' style='color:var(--bad);margin-left:10px'>Write mode "
                                         "isn't On yet: switch it in Settings first.</span>")
    b.append(f'<p style="margin-top:20px"><button onclick="return confirm(\'Apply the ticked changes to both '
             f'accounts? A backup is taken first.\')">Apply the ticked changes</button>{off}</p></form>')
    b.append(NAME_JS + SAVE_CHOICES_JS.replace("PLANID", str(int(plan["plan_id"]))))
    return page("Cleanup", "/cleanup", "".join(b))
