"""Server-rendered pages."""
import html
import time

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
.p-mute{color:var(--mute);background:var(--mutebg)}.muted{color:var(--muted)}.small{font-size:13px}
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
    links = [("/", "Dashboard"), ("/report", "Cleanup report"), ("/backups", "Backups"), ("/settings", "Settings")]
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
<p class="muted small" style="margin-top:40px">contact-sync {VERSION} · read-only build: it reads and backs up,
it never changes either account.</p></main>{js}</body></html>"""


def login_page(setup, error="", nxt="/"):
    if setup:
        body = f"""<h1>Choose a password</h1><p class="sub">First visit: pick the password for this site.</p>
{f'<div class="banner err">{esc(error)}</div>' if error else ''}
<form method="post" action="/setup"><label>Password</label><input type="password" name="password" autofocus
autocomplete="new-password"><label>Again</label><input type="password" name="password2" autocomplete="new-password">
<p><button>Save</button></p></form>"""
    else:
        body = f"""<h1>Log in</h1>{f'<div class="banner err">{esc(error)}</div>' if error else ''}
<form method="post" action="/login"><input type="hidden" name="next" value="{esc(nxt)}">
<label>Password</label><input type="password" name="password" autofocus autocomplete="current-password">
<p><button>Log in</button></p></form>"""
    return page("Log in", "", body, nav_on=False)


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
    if not api_on:
        b.append('<p class="muted small">API key: off (set API_KEY to enable /api/ access for scripts).</p>')
    return page("Dashboard", "/", "".join(b), refresh=job["running"])


def _tiles(r):
    c = r["counts"]
    t = [("Microsoft 365", c["microsoft"], ""), ("Google", c["google"], ""), ("People", c["people"], ""),
         ("Paired 1 : 1", c["paired"], ""), ("Only in Microsoft", c["only_ms"], "#only_ms"),
         ("Only in Google", c["only_google"], "#only_google"),
         ("Duplicate groups", c["dup_groups"], "#duplicates"), ("Extra copies", c["dup_extra"], "#duplicates"),
         ("Possible matches", c["possible"], "#possible"), ("Junk", c["junk"], "#junk"),
         ("Phone fixes", c["phone_fixes"], "#phones"), ("Unreadable phones", c["phone_unknown"], "#phones"),
         ("Name fixes", c["name_fixes"], "#names"), ("Old sync-tool notes", c["sync_tool_contacts"], "#synctool")]
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


def settings_page(s, saved=False, error=""):
    chk = lambda k: " checked" if s[k] else ""
    body = f"""<h1>Settings</h1>{'<div class="banner">Saved.</div>' if saved else ''}
{f'<div class="banner err">{esc(error)}</div>' if error else ''}
<form method="post" action="/settings">
<label class="check"><input type="checkbox" name="backup_enabled"{chk('backup_enabled')}> Nightly backup and report</label>
<label>Time</label><input type="time" name="backup_time" value="{esc(s['backup_time'])}">
<label>Keep backups for (days)</label><input type="number" name="keep_days" min="3" max="365" value="{s['keep_days']}">
<label>Country for phone numbers without a code</label><input type="text" name="default_region" maxlength="2"
value="{esc(s['default_region'])}"><div class="hint">Two letters, e.g. US, CA, GB, AU.</div>
<p><button>Save</button></p></form>
<h2>Change password</h2><form method="post" action="/password">
<label>Current password</label><input type="password" name="current" autocomplete="current-password">
<label>New password</label><input type="password" name="password" autocomplete="new-password">
<label>Again</label><input type="password" name="password2" autocomplete="new-password">
<p><button>Change password</button></p></form>
<h2>Sign-out</h2><p class="small">Forget the saved sign-in for one account (it stays allowed on the account
side until you remove the app there).</p>
<form method="post" action="/signout/microsoft" class="inline"><button class="ghost">Forget Microsoft sign-in</button></form>
<form method="post" action="/signout/google" class="inline"><button class="ghost">Forget Google sign-in</button></form>"""
    return page("Settings", "/settings", body)
