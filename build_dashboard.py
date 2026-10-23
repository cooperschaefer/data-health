"""
build_dashboard.py

Converts static_dashboard/data_health_dashboard_central.html (mock-data
mockup, the design source of truth) into dashboard.html (API-connected live
version, generated, gitignored). Never hand-edit dashboard.html.

Every replacement goes through _replace_once / _replace_text, which fail
loudly if an anchor isn't found instead of silently shipping stale JS — so a
design edit to the mockup that moves an anchor breaks the build, not the page.

Data flow on the built page:
  prod      → /api/data-health/dashboard with the id_token as a Bearer
              header; the API serves the refresh Lambda's S3 cache, or runs
              live if the cache is missing or unreadable. Never /cache/*
              directly: that path is outside the sign-in gate.
  ?tmonth=  → live API with that Targets month (not cached)
  ?tpdays=  → after the default load, the 3rd-party rates panel re-runs
              /api/data-health/checks/third_party?tp_days=N (the picker
              in that panel does the same and keeps this param in step)
  local dev → http://localhost:8000/api/data-health/dashboard
              (override the API origin with ?api=http://127.0.0.1:8001)

The concept / heatmap-window filters stay client-side: the payload is
network-wide, so Apply re-renders from data already loaded (the mockup's own
Apply behaviour) and syncs the URL so Share links match.

Dev mode:
    python build_dashboard.py
    python -m uvicorn api.main:app --reload
    python -m http.server 8080      # then open http://127.0.0.1:8080/dashboard.html
"""

import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

SRC = Path(__file__).parent / "static_dashboard" / "data_health_dashboard_central.html"
DEST = Path(__file__).parent / "dashboard.html"

# Cognito hosted-UI auth. After the CloudFront function redirects an
# unauthenticated viewer to the Cognito /login page, Cognito redirects back
# to the dashboard with a ?code=. The browser must exchange that code for
# tokens and drop an id_token cookie — otherwise the CloudFront function
# (which only checks for that cookie) bounces the viewer straight back to
# login. redirect_uri must match the CloudFront function exactly:
# window.location.origin (no trailing slash).
#
# Shared TEG pool (Google Workspace SSO), see infra/cognito.tf. COGNITO_CLIENT
# is this dashboard's app client on that pool (`terraform output
# cognito_client_id`); it only changes if the client is ever recreated.
COGNITO_DOMAIN = "https://teg-dashboards.auth.us-east-2.amazoncognito.com"
COGNITO_CLIENT = "33inu5tngqgn4n878ghm9avd2u"

AUTH_SCRIPT = f"""\
<script>
// Cognito auth ────────────────────────────────────────────────────────────────
const COGNITO_DOMAIN   = '{COGNITO_DOMAIN}';
const COGNITO_CLIENT   = '{COGNITO_CLIENT}';
const COGNITO_REDIRECT = window.location.origin;  // must match the CloudFront function's redirect_uri

async function _handleAuth() {{
  // Local dev (file://, localhost, or 127.0.0.1) skips auth entirely.
  if (_isLocalDev()) return true;

  const params = new URLSearchParams(window.location.search);
  const code = params.get('code');

  if (code) {{
    const resp = await fetch(COGNITO_DOMAIN + '/oauth2/token', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
      body: new URLSearchParams({{
        grant_type:   'authorization_code',
        client_id:    COGNITO_CLIENT,
        code:         code,
        redirect_uri: COGNITO_REDIRECT,
      }}),
    }});
    if (resp.ok) {{
      const tokens = await resp.json();
      const exp = new Date(Date.now() + tokens.expires_in * 1000).toUTCString();
      document.cookie = 'id_token=' + tokens.id_token + '; expires=' + exp + '; path=/; SameSite=Lax';
      window.history.replaceState({{}}, '', '/');  // strip ?code= from the URL
      return true;
    }}
    console.error('Token exchange failed', await resp.text());
    return false;
  }}

  const hasCookie = document.cookie.split(';').some(c => c.trim().startsWith('id_token='));
  if (!hasCookie) {{
    // Match the CloudFront function: /oauth2/authorize + identity_provider=Google
    // so the shared-pool viewer is sent straight to Google (silent SSO), not the
    // hosted-UI chooser. This path only fires if CloudFront let the request
    // through (e.g. a cookie expired client-side mid-session).
    window.location.href = COGNITO_DOMAIN + '/oauth2/authorize?client_id=' + COGNITO_CLIENT +
      '&response_type=code&scope=openid+email+profile&identity_provider=Google' +
      '&redirect_uri=' + encodeURIComponent(COGNITO_REDIRECT);
    return false;  // navigating away
  }}
  return true;
}}

async function _bootAfterAuth() {{
  const authed = await _handleAuth();
  if (!authed) return;
  initDashboard();
}}

document.addEventListener('DOMContentLoaded', _bootAfterAuth);
</script>"""


def _replace_once(html: str, pattern: str, replacement: str, label: str, flags=re.DOTALL) -> str:
    new_html, n = re.subn(pattern, lambda _m: replacement, html, count=1, flags=flags)
    if n == 0:
        sys.exit(f"ERROR: could not find anchor for step '{label}' — has the mockup's structure changed?")
    return new_html


def _replace_text(html: str, old: str, new: str, label: str, count: int = 1) -> str:
    """Literal replacement that must match exactly `count` times."""
    found = html.count(old)
    if found != count:
        sys.exit(f"ERROR: step '{label}' expected {count} match(es), found {found} — has the mockup's structure changed?")
    return html.replace(old, new)


def _kpis(html: str, marker: str, new_id: str, label: str) -> str:
    """Swap a hard-coded <div class="kpis"> block (one kpi per line) for an
    empty container the live script fills. `marker` picks which block."""
    block = re.compile(r'<div class="kpis"[^>]*>\n(?:[ \t]*<div class="kpi">.*\n)+[ \t]*</div>')
    for m in block.finditer(html):
        if marker in m.group(0):
            return html[:m.start()] + f'<div class="kpis" id="{new_id}"></div>' + html[m.end():]
    sys.exit(f"ERROR: could not find the KPI block for step '{label}' (marker {marker!r})")


# ─────────────────────────────────────────────────────────────────────────────
# Step 1: mock data arrays → live `let` globals, plus _isLocalDev()
# ─────────────────────────────────────────────────────────────────────────────
DATA_GLOBALS = r"""/* ---------- data: live globals, filled by _applyPayload() from the API / cache ---------- */
// Local dev = file://, localhost, 127.0.0.1 or ::1. Checking only 'localhost'
// sends anyone testing via 127.0.0.1 into a real Cognito redirect.
function _isLocalDev(){
  const h = location.hostname;
  return location.protocol === 'file:' || h === 'localhost' || h === '127.0.0.1' || h === '::1' || h === '[::1]';
}
let D = {};   // the whole payload, for the panels that read it directly
let ATTN = [], TABLES = [], STORES = [], MAPPROB = [];
let NET = {d:[], actual:[], base:[], ratio:[]}, RECON = {d:[], ratio:[], trail:[]};
let REV = {google:{MISSING:[],MISMATCH:[],NODATA:[],STALE:{}}, tripadvisor:{MISSING:[],MISMATCH:[],NODATA:[],STALE:{}}, yelp:{MISSING:[],MISMATCH:[],NODATA:[],STALE:{}}};
let UNMAPPED = {google:{rows:[]}, tripadvisor:{rows:[]}, yelp:{rows:[]}};
const DX = {NOTIN:'ID not in mapping: add a row', MISMATCH:'ID is in mapping but its ticker differs from dim_stores_master (case/typo)', MATCH:'ID in mapping and ticker matches: check the downstream join',
  STALE:'Already keyed to a store in the platform table; fct_stores_all_reviews has not re-keyed it (full refresh)'};
const _e = v => String(v ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const _MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const _md = iso => iso ? _MON[+iso.slice(5,7)-1] + ' ' + (+iso.slice(8,10)) : '—';
const _usd = v => v == null ? '—' : '$' + Math.round(v).toLocaleString();
"""

# ─────────────────────────────────────────────────────────────────────────────
# Step 2: null-safety patches (the mockup's data never divides by zero)
# ─────────────────────────────────────────────────────────────────────────────
LINECHART_NULLSAFE = [
    ("const all = series.flatMap(s=>s.v).concat(opts.band ? opts.band.flat() : []);",
     "const all = series.flatMap(s=>s.v).concat(opts.band ? opts.band.filter(Boolean).flat() : []).filter(v => v != null && isFinite(v));"),
    ("const x = i => L + (i/(labels.length-1))*(W-L-R), y = v => T + (1-(v-mn)/(mx-mn))*(H-T-B);",
     "if (!all.length) { el.innerHTML = '<div class=\"note\" style=\"margin:0\">No data for this window.</div>'; return; }\n"
     "  const x = i => L + (i/Math.max(labels.length-1,1))*(W-L-R), y = v => T + (1-(v-mn)/((mx-mn)||1))*(H-T-B);"),
    ("const up = opts.band.map((b,i)=>`${x(i)},${y(b[1])}`).join(' '), lo = opts.band.map((b,i)=>`${x(i)},${y(b[0])}`).reverse().join(' ');",
     "const up = opts.band.map((b,i)=>b ? `${x(i)},${y(b[1])}` : '').filter(Boolean).join(' '), lo = opts.band.map((b,i)=>b ? `${x(i)},${y(b[0])}` : '').filter(Boolean).reverse().join(' ');"),
    ("points=\"${s.v.map((v,i)=>`${x(i)},${y(v)}`).join(' ')}\"",
     "points=\"${s.v.map((v,i)=> v == null ? '' : `${x(i)},${y(v)}`).filter(Boolean).join(' ')}\""),
    ("const dots = (opts.flag||[]).map(i => `",
     "const dots = (opts.flag||[]).filter(i => series[0].v[i] != null).map(i => `"),
]

CHARTS_LIVE = r"""function renderCharts(){
const k = v => '$' + Math.round(v/1000) + 'k';
// Network ratio comes from the server (null when there is no baseline), so no div-by-zero here.
const flagNet = NET.ratio.map((r,i)=> r != null && r < .65 ? i : -1).filter(i=>i>=0);
lineChart($('#chartNet'), NET.d, [
  {name:'Actual', v:NET.actual, color:'var(--accent)'},
  {name:'Same-weekday baseline (4 wk avg)', v:NET.base, color:'var(--muted)', dash:true, w:1.5}
], {fmt:k, min:0, flag:flagNet});
lineChart($('#chartRecon'), RECON.d, [
  {name:'Aggregate / booking net paid', v:RECON.ratio, color:'var(--accent)'},
  {name:'28-day average', v:RECON.trail, color:'var(--muted)', dash:true, w:1.5}
], {fmt:v=>v.toFixed(2), min:.84, max:1.02, band:RECON.trail.map(t=> t == null ? null : [t-.05,t+.05])});
}
"""

# ─────────────────────────────────────────────────────────────────────────────
# Step 3: server-computed results rendered as-is (heatmap statuses, tiles)
# ─────────────────────────────────────────────────────────────────────────────
TILES_LIVE = r"""function renderTiles(){
const gaps = Object.values(D.COV_GAPS || {}).reduce((a,v) => a + v.map + v.stale, 0);
const bi = D.BOOK_INT || {}, flags = ['duplicate_ids','null_store','null_amount_paid','refund_exceeds_paid','orphan_store_rows'].filter(k => bi[k]).length;
const sellers = D.SELLERS || [];
const BADGE = {
  agg_stores_day: [D.OUTLIERS.length ? D.OUTLIERS.length + ' low store' + (D.OUTLIERS.length>1?'s':'') : 'OK', D.OUTLIERS.length ? 'YELLOW' : 'GREEN', NET.ratio.filter(v => v != null)],
  fct_ops_bookings_adjustments: [flags ? flags + ' integrity flag' + (flags>1?'s':'') : 'OK', bi.s || 'GREEN', null],
  fct_nps_details: [D.NPS_UNMAPPED_N ? D.NPS_UNMAPPED_N + ' unmapped' : 'OK', D.NPS_UNMAPPED_N ? 'YELLOW' : 'GREEN', null],
  fct_stores_all_reviews: [gaps ? gaps + ' coverage gaps' : 'OK', D.COV_S || 'GREEN', null],
  dim_stores_master: [sellers.length ? sellers.length + ' seller' + (sellers.length>1?'s':'') + ' not set up' : 'OK', sellers.some(s => s.booking_rows) ? 'RED' : sellers.length ? 'YELLOW' : 'GREEN', null],
};
$('#tiles').innerHTML = TABLES.map(t => { const [badge, bs, spark] = BADGE[t.name] || ['OK','GREEN',null]; return `
  <div class="tile">
    <div class="name"><span class="dot ${t.s}"></span>${t.name}</div>
    <div class="hrs">${t.hrs}<small> h since refresh (${t.at})</small></div>
    <div class="meta">${_e(t.meta)}</div>
    <div style="margin-top:8px"><span class="chip ${bs}">${badge}</span></div>
    ${sparkSvg(spark && spark.length > 1 ? spark : null)}
  </div>`; }).join('');
}
"""

HEATMAP_LIVE = r"""function renderHeatmap(){
  const all = (D.HM && D.HM.days) || [], days = Math.min(state.win, all.length), from = all.length - days;
  const rows = STORES.filter(s => (state.concept==='ALL' || s.c===state.concept) && D.HM.cells[s.n]);
  const labels = all.slice(from).map(d => (+d.slice(5,7)) + '/' + (+d.slice(8,10)));
  const WHY = {OK:'normal', LOW:'under 40% of usual', ZERO:'zero where revenue is expected', HIGH:'over 2.5x usual', NA:'no baseline'};
  let html = '<thead><tr><th class="store">Store</th>' + labels.map(l=>`<th>${l.split('/')[1]}</th>`).join('') + '</tr></thead><tbody>';
  rows.forEach(s => {
    html += `<tr><th class="store">${_e(s.n)}</th>`;
    D.HM.cells[s.n].slice(from).forEach((c,i) => {
      const [st, rev, base] = c;
      const tip = `${s.n} ${labels[i]}: ${_usd(rev)}` + (base != null ? ` vs ${_usd(base)} usual` : '') + ` (${WHY[st] || st})`;
      html += `<td><div class="c ${st}" title="${_e(tip)}"></div></td>`;
    });
    html += '</tr>';
  });
  $('#hm').innerHTML = html + '</tbody>';
}
"""

# ─────────────────────────────────────────────────────────────────────────────
# Step 5 (part): the live renderers for panels that were static HTML in the
# mockup, the payload loader, and initDashboard().
# ─────────────────────────────────────────────────────────────────────────────
LIVE_SCRIPT = r"""<style>.dx.STALE { color: var(--blue); }</style>
<script>
// Live data ───────────────────────────────────────────────────────────────────
const API_BASE = _isLocalDev() ? (new URLSearchParams(location.search).get('api') || 'http://localhost:8000') : '';
const _kpi = (v, label, color) => `<div class="kpi"><b${color ? ` style="color:var(--${color})"` : ''}>${v}</b><span>${label}</span></div>`;
const _sevColor = s => ({RED:'red', YELLOW:'yellow', GREEN:'green'})[s] || null;
const _words = ['No','One','Two','Three','Four','Five','Six','Seven','Eight','Nine','Ten'];

function _applyPayload(d){
  D = d;
  ATTN = d.ATTN; TABLES = d.TABLES; STORES = d.STORES; MAPPROB = d.MAPPROB;
  NET = d.NET; RECON = d.RECON; REV = d.REV; UNMAPPED = d.UNMAPPED;
  TTYPES = d.TTYPES; TG = d.TG; ORPHAN_T = d.ORPHAN_T; SG = d.SG;
  SLOT_ISSUES = d.SLOT_ISSUES; SLOT_MISMATCH = d.SLOT_MISMATCH; SHEET_COV = d.SHEET_COV; SHEET_COV_CLEAN = d.SHEET_COV_CLEAN; MONTHLY = d.MONTHLY;
  TP = d.TP;
}

function renderStatic(){
  const S = D.SUMMARY, M = D.META, T = D.TARGET;
  const gen = M.generated_at || '';
  const hh = +gen.slice(11,13), mm = gen.slice(14,16);
  const checked = gen ? `${_md(gen.slice(0,10))}, ${(hh % 12) || 12}:${mm} ${hh < 12 ? 'AM' : 'PM'} CT` : '—';
  $('#heroH1').innerHTML = S.h1;
  $('#heroP').textContent = S.para;
  $('#badgeScope').innerHTML = `TEG + GBGS &middot; ${M.store_count} active stores`;
  $('#badgeChecked').textContent = 'Checked ' + checked;
  document.title = `Data Health | ${S.red} red, ${S.yellow} yellow`;
  $('#storeN').textContent = STORES.length;

  const head = {RED:'Action needed', YELLOW:'Needs attention', GREEN:'All clear'}[S.status];
  $('#banner').style.borderLeftColor = `var(--${_sevColor(S.status)})`;
  $('#banner').innerHTML = `<div><div class="big"><span class="dot ${S.status}"></span>${head}</div>
      <div style="color:var(--muted);font-size:12px">${_e(S.para)}</div></div>
    <div class="stat"><b>${S.build}</b><span>Last successful model build (CT)</span></div>
    <div class="stat"><b style="color:var(--red)">${S.red}</b><span>Red issues</span></div>
    <div class="stat"><b style="color:var(--yellow)">${S.yellow}</b><span>Yellow issues</span></div>
    <div class="stat"><b style="color:var(--green)">${S.fresh} / ${TABLES.length}</b><span>Tables fresh (&lt;24h)</span></div>`;
  $('#cats').innerHTML = S.cats.map(c => `<div class="cat"><h3><span class="dot ${c.s}"></span>${_e(c.name)}</h3><div class="v">${_e(c.v)}</div><div class="d">${_e(c.d)}</div></div>`).join('');

  // revenue tab
  $('#outTbl').innerHTML = D.OUTLIERS.length ? D.OUTLIERS.map(o => `<tr><td>${_e(o.store)}</td><td>${o.concept}</td><td>${_usd(o.rev)}</td><td>${_usd(o.base)}</td><td>${o.ratio == null ? '—' : o.ratio.toFixed(2)}</td></tr>`).join('')
    : '<tr><td colspan="5" class="hintcell">No store was under 40% of its same-weekday average yesterday.</td></tr>';
  const U = D.UNKNOWN;
  $('#unkKpis').innerHTML = _kpi(U.pct.toFixed(1) + '%', 'Unknown share of Revel revenue, last 7 days', _sevColor(U.s) === 'green' ? null : _sevColor(U.s))
    + _kpi(U.stores.length, `Store${U.stores.length === 1 ? '' : 's'} with unknown revenue &gt; $0` + (U.stores.length ? ': ' + U.stores.slice(0,3).map(x => _e(x.store) + ' ' + _usd(x.rev)).join(', ') : ''));

  // NPS tab
  NPS_UNM = D.NPS_UNMAPPED.map(r => ({...r, items: r.items || []})); npsSel = -1;
  renderNpsUnmapped();
  $('#npsNone').innerHTML = D.NPS_NONE.length
    ? `<div class="tbl-wrap"><table><thead><tr><th>Store</th><th>Concept</th><th>Bookings (14 days)</th><th>NPS responses (30 days)</th></tr></thead><tbody>${D.NPS_NONE.map(r => `<tr><td>${_e(r.store)}</td><td>${r.concept}</td><td>${r.bookings_14d}</td><td><span class="chip YELLOW">0</span></td></tr>`).join('')}</tbody></table></div>`
    : '<div class="note" style="margin:0">No stores flagged.' + (D.NO_BOOKINGS.length ? ` (${D.NO_BOOKINGS.length} active store${D.NO_BOOKINGS.length>1?'s have':' has'} no bookings at all in 14 days: ${D.NO_BOOKINGS.map(r => _e(r.store)).join(', ')}.)` : '') + '</div>';
  renderNpsWeekly();

  // integrity tab
  const SEL = D.SELLERS;
  $('#sellerTbl tbody').innerHTML = SEL.length ? SEL.map(s => `<tr>
      <td><span class="chip ${s.s}">${s.booking_rows ? 'Taking bookings' : 'No bookings yet'}</span></td>
      <td class="mono">${_e(s.x_seller_id)}<br><button class="copyid" data-id="${_e(s.x_seller_id)}" onclick="navigator.clipboard &amp;&amp; navigator.clipboard.writeText(this.dataset.id).then(function(){ toast('Seller ID copied'); })">Copy ID</button></td>
      <td>${_e(s.seller_name)}<br><span style="color:var(--muted);font-size:12px">${_e(s.street_address)}</span></td>
      <td>${_md(s.created_in_xola)}</td>
      <td>${s.booking_rows ? `${s.booking_rows} rows, created ${_md(s.first_created)} to ${s.first_created && s.last_created && s.first_created.slice(5,7) === s.last_created.slice(5,7) ? +s.last_created.slice(8,10) : _md(s.last_created)}` : '0'}</td>
      <td>${_md(s.first_arrival)}</td><td>${_usd(s.paid)}</td><td>${_e(s.possible_sheet_row || '(no match by city)')}</td></tr>`).join('')
    : '<tr><td colspan="8" class="hintcell">Every Xola seller is in the store sheet.</td></tr>';
  const P = D.PLACEHOLDER;
  $('#phKpis').innerHTML = _kpi(P.total, 'Placeholder rows in the sheet')
    + _kpi(P.with_bookings.length, 'Placeholder rows with bookings, last 60 days', P.with_bookings.length ? 'yellow' : 'green')
    + _kpi(P.unknown_sellers.length, 'Booking seller IDs unknown to Xola, last 30 days', P.unknown_sellers.length ? 'yellow' : 'green');
  const B = D.BOOK_INT;
  $('#bkKpis').innerHTML = _kpi((B.rows_checked || 0).toLocaleString(), 'Rows checked')
    + _kpi(B.duplicate_ids, 'Duplicate IDs', B.duplicate_ids ? 'red' : null)
    + _kpi(B.null_store, 'Null store', B.null_store ? 'red' : null)
    + _kpi(B.null_amount_paid, 'Null amount paid', B.null_amount_paid ? 'yellow' : null)
    + _kpi(B.refund_exceeds_paid, 'Refund exceeds amount paid', B.refund_exceeds_paid ? 'yellow' : null)
    + _kpi(B.orphan_store_rows, 'Store key not in dim_stores_master' + (B.orphans.length ? ` (${B.orphans.length} key${B.orphans.length>1?'s':''})` : ''), B.orphan_store_rows ? 'red' : null);
  const A = D.AGG_INT;
  $('#aggKpis').innerHTML = _kpi(A.duplicate_store_days, 'Duplicate store-days in agg_stores_day', A.duplicate_store_days ? 'red' : null)
    + _kpi(A.null_revenue_rows_7d, 'Null-revenue rows, last 7 days', A.null_revenue_rows_7d ? 'yellow' : null);

  // targets tab
  const custom = new URLSearchParams(location.search).get('tmonth');
  $('#tgTitle').textContent = 'Targets loaded for ' + T.label;
  $('#tgHint').textContent = `Check 14. Which stores are missing each target type for ${custom ? 'the selected month' : 'next month'}. `
    + (T.days_to > 0 ? `${T.short} 1 is ${T.days_to} day${T.days_to === 1 ? '' : 's'} away; a type that isn't complete turns red 7 days before the month starts.` : `${T.label} has started, so anything missing is red.`)
    + ' Click a card to list only the stores missing that type.';

  // sheet upkeep tab
  $('#slotKpis').innerHTML = _kpi(SLOT_ISSUES.length, 'Open stores with 0 slots in the aggregate', SLOT_ISSUES.length ? 'red' : 'green')
    + _kpi(SLOT_MISMATCH.length, 'Stores where Team Info and the sheet disagree', SLOT_MISMATCH.length ? 'yellow' : 'green')
    + _kpi(D.SLOT_NEXT_BLANK, 'Stores with next month blank in the sheet', D.SLOT_NEXT_BLANK ? 'yellow' : 'green');
}

function renderNpsWeekly(){
  const W = D.NPS_WEEKS, rows = STORES.filter(s => (state.concept==='ALL' || s.c===state.concept) && W.cells[s.n]);
  const labels = W.weeks.map(w => (+w.slice(5,7)) + '/' + (+w.slice(8,10)));
  const WHY = {OK:'normal', LOW:'under 40% of the trailing 4-week average', ZERO:'no responses', NA:'no baseline yet, or the current partial week'};
  let html = '<thead><tr><th class="store">Store</th>' + labels.map((l,i) => `<th style="min-width:34px">${l}${i === labels.length-1 ? '*' : ''}</th>`).join('') + '</tr></thead><tbody>';
  rows.forEach(s => {
    html += `<tr><th class="store">${_e(s.n)}</th>` + W.cells[s.n].map((c,i) => {
      const [st, n, avg] = c, tip = `${s.n}, week of ${labels[i]}: ${n} responses` + (avg != null ? ` (trailing avg ${avg})` : '') + ` — ${WHY[st]}`;
      return `<td><div class="c ${st}" style="width:30px;font-size:10px;line-height:16px;text-align:center;color:var(--text)" title="${_e(tip)}">${n}</div></td>`; }).join('') + '</tr>';
  });
  $('#npsHm').innerHTML = html + '</tbody>';
}

function renderReviews(){
  const LBL = {google:'Google', tripadvisor:'TripAdvisor', yelp:'Yelp'};
  $$('#srcs .srcbtn').forEach(b => {
    const u = UNMAPPED[b.dataset.src] || {rows:[], n:0}, n = u.n || 0, L = u.rows.length;
    b.innerHTML = `<b${n ? ' style="color:var(--yellow)"' : ''}>${n}</b><span>${LBL[b.dataset.src]}${L ? ` (${L} listing${L>1?'s':''})` : ''}</span>`;
  });
  showSource(['yelp','tripadvisor','google'].find(p => UNMAPPED[p].rows.length) || 'yelp');
  $('#mapProblems tbody').innerHTML = MAPPROB.length ? MAPPROB.map(r => `<tr><td>${r[0]}</td><td class="mono">${_e(r[1])}</td><td class="mono">${_e(r[2])}</td><td class="mono">${_e(r[3])}</td><td><span class="chip ${r[4]==='Case mismatch'?'RED':'YELLOW'}">${r[4]}</span></td></tr>`).join('')
    : '<tr><td colspan="5" class="hintcell">Every mapping row has a platform ID and a ticker that joins to the store dimension.</td></tr>';
}

function renderAll(){
  renderStatic();
  renderTiles();
  renderAttn();
  renderCharts();
  renderHeatmap();
  renderMatrix();
  renderReviews();
  renderTargets();
  $('#orphanT').innerHTML = ORPHAN_T.length ? ORPHAN_T.map(r => `<tr><td class="mono">${_e(r[0])}</td><td>${_e(r[1])}</td></tr>`).join('')
    : '<tr><td colspan="2" class="hintcell">Every target row uses a ticker that is in dim_stores_master.</td></tr>';
  renderSuggestions();
  renderUpkeep();
  renderThirdParty();
}

// 3rd-party rates lookback (Check 20). The payload carries the 90-day
// default; any other window re-runs just that check live. A failure keeps
// the table that's showing and says so in the panel.
async function loadThirdParty(days){
  const sel = $('#tpDays');
  sel.disabled = true;
  $('#tpStatus').textContent = `Checking the last ${days === 365 ? 'year' : days + ' days'}…`;
  try {
    const url = API_BASE + '/api/data-health/checks/third_party?tp_days=' + days;
    const res = await fetch(url, {cache: 'no-cache', headers: _authHeaders()});
    if (res.status === 401) { _reauthenticate(); return; }
    if (!res.ok) {
      let detail = ''; try { detail = (await res.json()).detail || ''; } catch (_) {}
      throw new Error(`HTTP ${res.status}${detail ? ': ' + detail : ''}`);
    }
    TP = (await res.json()).TP;
    const q = new URLSearchParams(location.search);
    if (TP.days === TP.default) q.delete('tpdays'); else q.set('tpdays', TP.days);
    history.replaceState(null, '', location.pathname + (q.toString() ? '?' + q.toString() : ''));
    renderThirdParty();
  } catch (err) {
    console.error('[loadThirdParty]', err);
    sel.value = String(TP.days);
    $('#tpStatus').innerHTML = `<span style="color:var(--red)">Could not load ${days} days (${_e(err.message)}). Still showing ${TP.days} days.</span>`;
  } finally {
    sel.disabled = false;
  }
}

// Loading overlay ────────────────────────────────────────────────────────────
function _showOverlay(msg){
  let el = document.getElementById('_api-loader');
  if (!el) {
    el = document.createElement('div');
    el.id = '_api-loader';
    el.setAttribute('role', 'status');
    el.style.cssText = 'position:fixed;inset:0;z-index:99999;background:rgba(18,18,60,.72);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;font-family:var(--font-body);color:#fff;padding:16px;text-align:center';
    el.innerHTML = '<div id="_api-msg" style="font-size:16px;font-weight:600"></div><div id="_api-detail" style="font-size:13px;color:#d6d8ff;max-width:560px;line-height:1.5"></div>';
    document.body.appendChild(el);
  }
  document.getElementById('_api-msg').textContent = msg;
}
function _setDetail(msg, isError){
  const el = document.getElementById('_api-detail');
  if (el) { el.textContent = msg; el.style.color = isError ? '#ffb4c0' : '#d6d8ff'; }
}
function _hideOverlay(){ const el = document.getElementById('_api-loader'); if (el) el.remove(); }

// Auth ───────────────────────────────────────────────────────────────────────
// Every /api call carries the Cognito id_token; the API rejects anything else.
// Local dev has no token and the API skips the check.
function _authHeaders(){
  const m = document.cookie.match(/(?:^|;\s*)id_token=([^;]+)/);
  return m ? { Authorization: 'Bearer ' + m[1] } : {};
}
// Token rejected (expired, or a cookie that outlived it): drop the cookie and
// reload, which sends the browser back through sign-in. At most once a minute,
// so a misconfigured API shows an error instead of reload-looping.
function _reauthenticate(){
  let last = 0;
  try { last = Number(sessionStorage.getItem('dhReauthAt')) || 0; } catch (_) {}
  if (Date.now() - last < 60000) {
    _showOverlay('Could not load the data health checks');
    _setDetail('Your sign-in was not accepted. Reload the page to try again.', true);
    return;
  }
  try { sessionStorage.setItem('dhReauthAt', String(Date.now())); } catch (_) {}
  document.cookie = 'id_token=; path=/; max-age=0; SameSite=Lax';
  location.reload();
}

// Boot ───────────────────────────────────────────────────────────────────────
async function initDashboard(){
  _showOverlay('Loading data health checks…');
  const tmonth = new URLSearchParams(location.search).get('tmonth');
  const url = API_BASE + '/api/data-health/dashboard' + (tmonth ? '?target_month=' + encodeURIComponent(tmonth) : '');
  // The API serves the pre-baked cache for the default view (behind the same
  // sign-in check as everything else) and only runs the checks live for a
  // non-default month, a cache miss, or local dev.
  let data = null, error = '';
  try {
    _setDetail(tmonth || _isLocalDev() ? 'Running the checks against Snowflake (this can take 20-30 seconds)…' : 'Reading the latest results…');
    const res = await fetch(url, {cache: 'no-cache', headers: _authHeaders()});
    if (res.status === 401) { _reauthenticate(); return; }
    if (!res.ok) {
      let detail = ''; try { detail = (await res.json()).detail || ''; } catch (_) {}
      throw new Error(`${url} returned HTTP ${res.status}${detail ? ': ' + detail : ''}`);
    }
    data = await res.json();
  } catch (err) {
    console.error('[initDashboard]', err);
    error = err.message;
  }
  if (!data) {
    _showOverlay('Could not load the data health checks');
    _setDetail(error + (_isLocalDev() ? ' — is the API running? (python -m uvicorn api.main:app --reload)' : ''), true);
    return;
  }
  try {
    _applyPayload(data);
    renderAll();
    initDataCentral();
    _hideOverlay();
    const tpd = +new URLSearchParams(location.search).get('tpdays');
    if (tpd && tpd !== TP.days && [...$('#tpDays').options].some(o => +o.value === tpd)) loadThirdParty(tpd);
  } catch (err) {
    console.error('[initDashboard] render', err);
    _showOverlay('The dashboard could not render this data');
    _setDetail(err.message, true);
  }
}
</script>"""

# ─────────────────────────────────────────────────────────────────────────────
# Data Central tiles: the same 21 tiles, numbers from the payload. Copy and
# viz helpers stay the mockup's; only hard-coded values change.
# ─────────────────────────────────────────────────────────────────────────────
DC_TILES_LIVE = r"""  // ---- numbers computed from the same data the dashboard tabs use
  const revCnt = p => { const d = REV[p]; return {map: d.MISSING.length + d.MISMATCH.length + d.NODATA.length, stale: Object.keys(d.STALE).length}; };
  const plats = ['google','tripadvisor','yelp'].map(revCnt);
  const mapProblems = plats.reduce((a,p) => a + p.map, 0), stale = plats.reduce((a,p) => a + p.stale, 0), gaps = mapProblems + stale, cells = STORES.length * 3;
  const unmBy = p => UNMAPPED[p].n || 0, unm = unmBy('google') + unmBy('tripadvisor') + unmBy('yelp');
  const unmStale = ['tripadvisor','yelp'].flatMap(p => UNMAPPED[p].rows).filter(r => r.dx === 'STALE').reduce((a,r) => a + r.reviews, 0);
  const unmCase = ['tripadvisor','yelp'].flatMap(p => UNMAPPED[p].rows).filter(r => r.dx === 'MISMATCH').reduce((a,r) => a + r.reviews, 0);
  const blank = MAPPROB.filter(r => r[4] === 'Blank platform ID').length, mism = MAPPROB.filter(r => r[4] === 'Case mismatch').length;
  const ratioNet = NET.ratio.filter(v => v != null), lastNet = ratioNet.length ? ratioNet[ratioNet.length-1] : null;
  const oldest = TABLES.reduce((a,b) => b.hrs > a.hrs ? b : a, TABLES[0] || {name:'—', hrs:0});
  const fresh = TABLES.filter(t => t.s === 'GREEN').length;
  const worst = arr => arr.includes('RED') ? 'RED' : arr.includes('YELLOW') ? 'YELLOW' : 'GREEN';
  const lagMax = Math.max(0, ...D.LAG.map(x => x.days ?? 99));
  const aggLag = D.LAG.find(x => x.source.startsWith('Aggregate')) || {}, others = D.LAG.filter(x => !x.source.startsWith('Aggregate') && x.latest).map(x => x.latest).sort();
  const typeState = TTYPES.map(t => { const app = STORES.filter(s => tstatus(s,t) !== 'NA'); const ok = app.filter(s => tstatus(s,t) === 'LOADED').length; return app.length && ok === app.length ? 'GREEN' : ok === 0 ? 'GREY' : 'YELLOW'; });
  const typesDone = typeState.filter(x => x === 'GREEN').length, typesPartial = typeState.filter(x => x === 'YELLOW').length, typesNone = typeState.filter(x => x === 'GREY').length;
  const missCounts = STORES.map(s => ({n: s.n, k: TTYPES.filter(t => ['MISSING','NEVER'].includes(tstatus(s,t))).length})).sort((a,b) => b.k - a.k || a.n.localeCompare(b.n));
  const storesMissing = missCounts.filter(x => x.k > 0).length;
  const red = ATTN.filter(a => a.sev === 'RED').length, yellow = ATTN.filter(a => a.sev === 'YELLOW').length;
  const TG_ = D.TARGET, SEL = D.SELLERS, taking = SEL.filter(s => s.booking_rows), U = D.UNKNOWN, B = D.BOOK_INT, A = D.AGG_INT, O = D.OUTLIERS;
  const bkFlags = (B.refund_exceeds_paid||0) + (B.orphan_store_rows||0) + (B.null_amount_paid||0) + (B.duplicate_ids||0) + (B.null_store||0);
  const covStores = [...new Set(SHEET_COV.flatMap(x => x.missing))];
  const covWorst = covStores.map(n => ({n, k: SHEET_COV.filter(c => c.missing.includes(n)).length})).sort((a,b) => b.k - a.k || a.n.localeCompare(b.n));
  const behind = MONTHLY.filter(x => x.s === 'YELLOW');
  const slotKinds = k => SLOT_ISSUES.filter(x => x.kind === k).length;
  const nullLoc = D.NPS_NULL_LOC, npsN = D.NPS_UNMAPPED_N;
  const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
  const days = TG_.days_to > 0 ? `${TG_.short} 1 is ${TG_.days_to} day${TG_.days_to === 1 ? '' : 's'} away` : `${TG_.label} has started`;
  const tpZero = TP.rows.reduce((a,r) => a + r.fully_discounted, 0), tpFull = TP.rows.reduce((a,r) => a + r.full_price, 0);
  const tpByProg = {}; TP.rows.forEach(r => tpByProg[r.sheet_column] = (tpByProg[r.sheet_column] || 0) + r.bookings);
  const tpTop = Object.keys(tpByProg).sort((a,b) => tpByProg[b] - tpByProg[a]);

  // tab = which tab opens; at = panel heading prefix (or #id) to scroll to
  const sgc = {xola:0, pattern:0, confirm:0}; if (SG) Object.values(SG.sug).forEach(x => sgc[x.c]++);
  const T = [
    {cat:'Pipeline', title:'Table freshness', s:worst(TABLES.map(t => t.s)), num:`${fresh} / ${TABLES.length}`, unit:'tables refreshed in the last 24 hours', viz:segs(TABLES.map(t => t.s)), detail:`Oldest is ${oldest.name}, ${oldest.hrs} hours ago`, tab:'overview', at:'#tiles', dest:'Overview'},
    {cat:'Pipeline', title:'Data lag by source', s:worst(D.LAG.map(x => x.s)), num:String(lagMax), unit:`day${lagMax === 1 ? '' : 's'} behind at most`, viz:segs(D.LAG.map(x => x.s)), detail:`Aggregate runs through ${_md(aggLag.latest)}; bookings, NPS and reviews through ${_md(others[0])}${others.length && others[0] !== others[others.length-1] ? ' or later' : ''}`, tab:'overview', at:'#tiles', dest:'Overview'},
    {cat:'Revenue and volume', title:'Network revenue vs baseline', s:D.NET_S, num:lastNet == null ? '—' : lastNet.toFixed(2) + '×', unit:'vs same-weekday average', viz:ratioNet.length > 1 ? spark(ratioNet, 0.4, 1.6) : '<span class="dc-viz"></span>', detail:'Yellow below 0.65×, red below 0.5×', tab:'revenue', at:'Network revenue', dest:'Revenue & volume'},
    {cat:'Revenue and volume', title:'Bookings vs aggregate', s:RECON.s, num:RECON.last == null ? '—' : RECON.last.toFixed(3), unit:'aggregate ÷ booking net paid', viz:RECON.ratio.filter(v => v != null).length > 1 ? spark(RECON.ratio.filter(v => v != null), 0.86, 1.0) : '<span class="dc-viz"></span>', detail:RECON.drift == null ? 'No overlapping days' : `${Math.abs(RECON.drift) <= .05 ? 'Within 5 points of' : (Math.abs(RECON.drift)*100).toFixed(1) + ' points off'} the 28-day average`, tab:'revenue', at:'Bookings vs aggregate', dest:'Revenue & volume'},
    {cat:'Revenue and volume', title:'Low-revenue stores', s:O.length ? 'YELLOW' : 'GREEN', num:String(O.length), unit:'stores under 40% of usual yesterday', viz:O.length ? rowbars(O.slice(0,6).map(o => o.ratio || 0.02)) : check, detail:O.length ? `${O[0].store} lowest at ${(O[0].ratio ?? 0).toFixed(2)} of its weekday average` : 'Every store within its usual range', tab:'revenue', at:"Yesterday's outliers", dest:'Revenue & volume'},
    {cat:'Revenue and volume', title:'Unattributed revenue', s:U.s, num:U.pct.toFixed(1) + '%', unit:'of Revel revenue is unknown', viz:bar([Math.max(U.pct, 0.4),'YELLOW'],[100 - Math.max(U.pct, 0.4),'GREY']), detail:`${plural(U.stores.length, 'store')} with unknown revenue above $0`, tab:'revenue', at:'Unattributed revenue', dest:'Revenue & volume'},
    {cat:'Store setup', title:'Sellers not in the store sheet', s:taking.length ? 'RED' : SEL.length ? 'YELLOW' : 'GREEN', num:String(taking.length || SEL.length), unit:taking.length ? `Xola seller${taking.length>1?'s':''} taking bookings` : 'Xola sellers missing from the sheet', viz:SEL.length ? pills([`${SEL[0].booking_rows} booking rows`, _usd(SEL[0].paid)]) : check, detail:SEL.length ? `${SEL[0].seller_name}; add its ID to the ${SG ? SG.store + ' row' : 'store sheet'}` : 'Every Xola seller is in the store sheet', tab:'integrity', at:'Xola sellers not set up', dest:'Integrity'},
    {cat:'Store setup', title:'Sheet fill-in suggestions', s:SG ? 'YELLOW' : 'GREEN', num:String(SG ? Object.keys(SG.sug).length : 0), unit:SG ? `values suggested for the ${SG.store} row` : 'rows waiting to be filled in', viz:SG ? bar([sgc.xola,'GREEN'],[sgc.pattern,'BLUE'],[sgc.confirm,'YELLOW']) : check, detail:SG ? `${sgc.xola} from Xola, ${sgc.pattern} from sheet patterns, ${sgc.confirm} to confirm. Select and copy them` : 'Nothing to fill in', tab:'integrity', at:'Suggested values for', dest:'Integrity'},
    {cat:'Integrity', title:'Aggregate table integrity', s:A.s, num:String((A.duplicate_store_days||0) + (A.null_revenue_rows_7d||0)), unit:'duplicate store-days or null revenue rows', viz:A.s === 'GREEN' ? check : bar([A.duplicate_store_days,'RED'],[A.null_revenue_rows_7d,'YELLOW']), detail:'agg_stores_day, last 7 days', tab:'integrity', at:'Aggregate integrity', dest:'Integrity'},
    {cat:'Integrity', title:'Bookings integrity', s:B.s, num:String(bkFlags), unit:'rows flagged in 30 days', viz:bkFlags ? bar([B.refund_exceeds_paid,'YELLOW'],[B.orphan_store_rows,'RED'],[B.null_amount_paid + B.duplicate_ids + B.null_store,'ORANGE']) : check, detail:`${B.refund_exceeds_paid} refunds larger than amount paid, ${B.orphan_store_rows} rows with an orphan store key`, tab:'integrity', at:'Bookings integrity', dest:'Integrity'},
    {cat:'Reviews', title:'Review coverage', s:D.COV_S, num:String(gaps), unit:'store and platform gaps', viz:bar([mapProblems,'RED'],[stale,'YELLOW'],[cells-gaps,'GREEN']), detail:`${mapProblems} need a mapping fix, ${stale} stopped receiving reviews`, tab:'reviews', at:'Review coverage matrix', dest:'Reviews'},
    {cat:'Reviews', title:'Mapping sheet problems', s:mism ? 'RED' : MAPPROB.length ? 'YELLOW' : 'GREEN', num:String(MAPPROB.length), unit:"rows that don't join to a store", viz:MAPPROB.length ? bar([mism,'RED'],[MAPPROB.length - mism,'YELLOW']) : check, detail:`${blank} blank platform IDs, ${mism} ticker casing mismatches`, tab:'reviews', at:'Mapping sheet rows', dest:'Reviews'},
    {cat:'Reviews', title:'Reviews without a store', s:unm ? 'YELLOW' : 'GREEN', num:String(unm), unit:'reviews in 30 days have no store', viz:pills(['Google ' + unmBy('google'), 'TripAdvisor ' + unmBy('tripadvisor'), 'Yelp ' + unmBy('yelp')]), detail:unm ? [unmStale && `${unmStale} need the all-reviews model refreshed`, unmCase && `${unmCase} from ticker casing`].filter(Boolean).join(', ') || 'See the listings behind them' : 'Every review has a store', tab:'reviews', at:'Reviews that landed without a store', dest:'Reviews'},
    {cat:'Surveys', title:'NPS without a store', s:npsN ? 'YELLOW' : 'GREEN', num:String(npsN), unit:'responses in 30 days have no store', viz:npsN ? bar([nullLoc,'YELLOW'],[npsN - nullLoc,'ORANGE']) : check, detail:npsN ? `${nullLoc} never captured a location; ${npsN - nullLoc} name one` : 'Every response has a store', tab:'nps', at:'NPS responses that landed', dest:'NPS'},
    {cat:'Surveys', title:'Stores with no NPS', s:D.NPS_NONE.length ? 'YELLOW' : 'GREEN', num:String(D.NPS_NONE.length), unit:'stores with bookings but no responses', viz:D.NPS_NONE.length ? bar([D.NPS_NONE.length,'YELLOW'],[STORES.length - D.NPS_NONE.length,'GREEN']) : check, detail:`Checked across all ${STORES.length} stores, last 30 days`, tab:'nps', at:'Stores with bookings but no NPS', dest:'NPS'},
    {cat:`Targets (${TG_.label.split(' ')[0]})`, title:'Target types complete', s:TG_.s, num:`${typesDone} / ${TTYPES.length}`, unit:'types loaded for every store', viz:segs(typeState), detail:`${typesPartial} partly loaded, ${typesNone} not loaded. ${days}`, tab:'targets', at:'Targets loaded for', dest:'Targets'},
    {cat:`Targets (${TG_.label.split(' ')[0]})`, title:'Stores missing targets', s:storesMissing ? TG_.s : 'GREEN', num:`${storesMissing}`, unit:`of ${STORES.length} stores miss at least one`, viz:bar([STORES.length-storesMissing,'GREEN'],[storesMissing,'YELLOW']), detail:storesMissing ? `Most missing: ${missCounts[0].n}${missCounts[1] && missCounts[1].k === missCounts[0].k ? ' and ' + missCounts[1].n + ', ' + missCounts[0].k + ' each' : ', ' + missCounts[0].k}` : 'Every store has every target', tab:'targets', at:'Store × target type', dest:'Targets'},
    {cat:`Targets (${TG_.label.split(' ')[0]})`, title:'Unrecognised target tickers', s:'INFO', num:String(ORPHAN_T.length), unit:"tickers that aren't stores", viz:'<span class="dc-viz"></span>', detail:ORPHAN_T.length ? 'Placeholders such as ' + ORPHAN_T.slice(0,2).map(r => r[0]).join(', ') + ', or stores not yet open' : 'Every target ticker is a store', tab:'targets', at:'Target rows under a ticker', dest:'Targets'},
    {cat:'Sheet upkeep', title:'Slots upkeep', s:D.SLOT_S, num:String(SLOT_ISSUES.length), unit:'open stores show 0 slots', viz:SLOT_ISSUES.length ? bar([slotKinds('missing') + slotKinds('named') + slotKinds('blank'),'RED'],[slotKinds('unpicked'),'ORANGE']) : check, detail:`${slotKinds('missing') + slotKinds('named') + slotKinds('blank')} missing from the sheet, ${slotKinds('unpicked')} the aggregate is not picking up. ${SLOT_MISMATCH.length} more where Team Info disagrees`, tab:'upkeep', at:'Slots upkeep', dest:'Sheet upkeep'},
    {cat:'Sheet upkeep', title:'Stores missing from sheets', s:covStores.length ? 'YELLOW' : 'GREEN', num:String(covStores.length), unit:'stores missing from at least one sheet', viz:segs(SHEET_COV.map(x => 'YELLOW').concat(SHEET_COV_CLEAN.map(x => 'GREEN'))), detail:`${SHEET_COV.reduce((a,x) => a + x.missing.length, 0)} gaps across ${SHEET_COV.length} of ${SHEET_COV.length + SHEET_COV_CLEAN.length} sheets.` + (covWorst.length ? ' Worst: ' + covWorst.slice(0,3).map(x => x.n).join(', ') : ''), tab:'upkeep', at:'New-store checklist', dest:'Sheet upkeep'},
    {cat:'Sheet upkeep', title:'Monthly sheets behind', s:behind.length ? 'YELLOW' : 'GREEN', num:String(behind.length), unit:'monthly sheets behind where they should be', viz:segs(MONTHLY.map(x => x.s === 'INFO' ? 'GREY' : x.s)), detail:behind.length ? behind.slice(0,3).map(x => `${x.sheet} (${x.latest})`).join(', ') : 'Every sheet is where it should be', tab:'upkeep', at:'Month-by-month sheets', dest:'Sheet upkeep'},
    {cat:'Sheet upkeep', title:'3rd-party rates missing', s:TP.s, num:String(TP.stores), unit:`store${TP.stores === 1 ? '' : 's'} took 3rd-party bookings with no rate`, viz:TP.rows.length ? bar([tpZero,'RED'],[tpFull,'YELLOW'],[TP.bookings - tpZero - tpFull,'GREY']) : check, detail:TP.rows.length ? `${plural(TP.bookings, 'booking')} in ${TP.days} days, mostly ${tpTop[0]}${tpTop[1] ? ' and ' + tpTop[1] : ''}` : `Every 3rd-party booking in ${TP.days} days found a rate`, tab:'upkeep', at:'3rd-party bookings', dest:'Sheet upkeep'}
  ];
"""


def main() -> None:
    if not SRC.exists():
        sys.exit(f"Source not found: {SRC}\n"
                 f"Drop the mockup at static_dashboard/data_health_dashboard_central.html "
                 f"or point SRC above at it.")

    html = SRC.read_text(encoding="utf-8")
    steps: list[str] = []

    # ── Step 1: mock data → live globals ────────────────────────────────────
    html = _replace_once(
        html,
        r"/\* ---------- data \(replace with live queries\) ---------- \*/\n.*?\nconst MAPPROB = \[.*?\n\];\n",
        DATA_GLOBALS,
        "mock data block (ATTN … MAPPROB)",
    )
    html = _replace_once(html, r"const TTYPES = \[.*?\n\];\n", "let TTYPES = [];\n", "TTYPES config → from the API")
    html = _replace_once(
        html,
        r"/\* L = loaded, M = missing but had one this month, N = missing both months\..*?\nfunction tflags\(store\)\{.*?\n\}\n",
        "/* L = loaded, M = missing but had one this month, N = missing both months. From Check 14 via the API. */\n"
        "let TG = {}, ORPHAN_T = [];\nfunction tflags(store){ return TG[store.n] || {}; }\n",
        "target flags (TG_*) → from the API",
    )
    html = _replace_once(html, r"const SG = \{\"row\".*?\};\n", "let SG = null;\n", "SG suggestions → from the API")
    html = _replace_once(
        html,
        r"const SLOT_ISSUES = \[.*?\nconst MONTHLY = \[.*?\n\];\n",
        "let SLOT_ISSUES = [], SLOT_MISMATCH = [], SHEET_COV = [], SHEET_COV_CLEAN = [], MONTHLY = [];\n",
        "sheet upkeep arrays → from the API",
    )
    steps.append("Rewired:   mock data arrays → live `let` globals (+ _isLocalDev, DX.STALE)")

    # ── Step 2: null-safety in the line chart + network ratio from the API ──
    for old, new in LINECHART_NULLSAFE:
        html = _replace_text(html, old, new, "lineChart null-safety")
    html = _replace_once(
        html,
        r"const k = v => '\$' \+ Math\.round\(v/1000\) \+ 'k';\n.*?band:RECON\.trail\.map\(t=>\[t-\.05,t\+\.05\]\)\}\);\n",
        CHARTS_LIVE,
        "network / reconciliation charts",
    )
    steps.append("Patched:   lineChart skips nulls and empty series; network ratio uses the API's null-safe ratio")

    # ── Step 3: render server results instead of re-deriving them ───────────
    html = _replace_once(html, r"\$\('#tiles'\)\.innerHTML = TABLES\.map\(t => `.*?`\)\.join\(''\);\n", TILES_LIVE, "freshness tiles")
    html = _replace_once(html, r"function renderHeatmap\(\)\{.*?\n\}\nrenderHeatmap\(\);\n", HEATMAP_LIVE, "store x day heatmap (seeded random → Check 4)")
    html = _replace_text(
        html,
        "const ta = statusFor('tripadvisor', s.n), ye = statusFor('yelp', s.n);\n    return {store:s.n, ta, ye, score: SCORE[ta[0]] + SCORE[ye[0]]};",
        "const go = statusFor('google', s.n), ta = statusFor('tripadvisor', s.n), ye = statusFor('yelp', s.n);\n"
        "    return {store:s.n, go, ta, ye, score: SCORE[go[0]] + SCORE[ta[0]] + SCORE[ye[0]]};",
        "review matrix: Google column from Check 10",
    )
    html = _replace_text(html, "<td>${cell(['OK'])}</td><td>${cell(r.ta)}</td>", "<td>${cell(r.go)}</td><td>${cell(r.ta)}</td>", "review matrix Google cell")
    html = _replace_text(html, "  if (d.STALE[name]) return ['STALE', d.STALE[name]];", "  if (name in d.STALE) return ['STALE', d.STALE[name]];", "stale lookup (0 days is valid)")
    steps.append("Rewired:   tiles, heatmap and review matrix render the API's statuses (Google column now live)")

    # ── Step 4: Apply handler, Share link, top-level render calls ───────────
    html = _replace_text(
        html,
        "$('#btnApply').onclick = () => { state.concept = pending.concept; state.win = pending.win; markDirty(); renderHeatmap(); renderMatrix(); if (typeof renderTargets==='function') renderTargets(); };",
        "$('#btnApply').onclick = () => {\n"
        "  state.concept = pending.concept; state.win = pending.win; markDirty();\n"
        "  // the payload is network-wide, so filters re-render client-side; keep the URL in step for Share links\n"
        "  const q = new URLSearchParams(location.search); q.set('concept', state.concept); q.set('win', state.win);\n"
        "  history.replaceState(null, '', location.pathname + '?' + q.toString());\n"
        "  if (!D.STORES) { toast('Data has not loaded yet'); return; }\n"
        "  renderHeatmap(); renderMatrix(); renderTargets(); renderNpsWeekly();\n"
        "};",
        "Apply handler",
    )
    html = _replace_text(
        html,
        "const p = new URLSearchParams({concept:state.concept, win:state.win});",
        "const p = new URLSearchParams({concept:state.concept, win:state.win});\n"
        "  const tm = new URLSearchParams(location.search).get('tmonth'); if (tm) p.set('tmonth', tm);\n"
        "  if (TP.days !== TP.default) p.set('tpdays', TP.days);",
        "Share URL keeps a non-default target month",
    )
    html = _replace_text(html, "renderAttn();\n\n/* ---------- line charts", "\n/* ---------- line charts", "defer renderAttn()")
    html = _replace_text(html, "renderMatrix();\n\n/* ---------- unmapped reviews", "\n/* ---------- unmapped reviews", "defer renderMatrix()")
    html = _replace_once(html, r"showSource\('yelp'\);\n", "", "defer review drill-down")
    html = _replace_once(html, r"const NPS_UNM = \[\n.*?\n\];\n", "let NPS_UNM = [];   // from the API (Check 11c + its responses)\n", "unmapped NPS mock → from the API")
    html = _replace_text(html, "}\nrenderNpsUnmapped();\n", "}\n", "defer unmapped NPS render")
    html = _replace_once(html, r"\$\('#mapProblems tbody'\)\.innerHTML = [^\n]*\n", "", "defer mapping table")
    html = _replace_once(
        html,
        r"const TP = \{days:90.*?\n\]\};\n",
        "let TP = {days:90, default:90, max:365, cap:200, stores:0, bookings:0, s:'GREEN', rows:[]};   // from the API (Check 20 + its bookings)\n",
        "3rd-party rates mock → from the API",
    )
    html = _replace_once(
        html,
        r"\$\('#tpDays'\)\.onchange = [^\n]*\nrenderThirdParty\(\);\n",
        "$('#tpDays').onchange = () => loadThirdParty(+$('#tpDays').value);\n",
        "3rd-party lookback picker → live re-query (and defer its render)",
    )
    html = _replace_once(html, r"\$\('#orphanT'\)\.innerHTML = [^\n]*\nrenderTargets\(\);\n", "", "defer targets render")
    html = _replace_once(
        html,
        r"\(function\(\)\{\n  const sug = SG\.sug, cols = SG\.cols",
        "function renderSuggestions(){\n"
        "  if (!SG) {\n"
        "    $('#sgTitle').textContent = 'Suggested values for new store rows';\n"
        "    $$('#sgPanel .sg-legend, #sgPanel .sg-tools, #sgPanel .sg-strip, #sgPanel .sg-info, #sgPanel .sg-groups').forEach(e => e.style.display = 'none');\n"
        "    $('#sgNeeds').textContent = 'No Xola sellers are missing from the store sheet, so there is nothing to fill in.';\n"
        "    return;\n"
        "  }\n"
        "  $('#sgTitle').textContent = `Suggested values for the ${SG.store} row`;\n"
        "  SG.row = SG.row || '?';\n"
        "  const sug = SG.sug, cols = SG.cols",
        "suggestion strip → renderSuggestions()",
    )
    html = _replace_once(html, r"  render\(\); update\(\);\n\}\)\(\);\n", "  render(); update();\n}\n", "close renderSuggestions()")
    html = _replace_text(html, "in the file you uploaded", "in the sheet as of its last sync", "suggestion copy: sheet, not upload", count=3)
    html = _replace_text(html, "keep their value from the uploaded file", "keep their value from the sheet's last sync", "suggestion copy: range toast")
    html = _replace_text(
        html,
        "' is the Denver row in the sheet as of its last sync; check the row number if the sheet has changed.'",
        "' is the ' + SG.store + ' row in the store sheet as of its last sync; check the row number if the sheet has changed.'",
        "suggestion note: live store name",
    )
    html = _replace_once(html, r"\(function\(\)\{\n  \$\('#slotsIssues tbody'\)", "function renderUpkeep(){\n  $('#slotsIssues tbody')", "sheet upkeep → renderUpkeep()")
    html = _replace_text(html, "\n})();\n\n/* ---------- 3rd-party rates", "\n}\n\n/* ---------- 3rd-party rates", "close renderUpkeep()")
    html = _replace_text(
        html,
        "<script>\n(function(){\n  const dc = document.getElementById('dc')",
        "<script>\nfunction initDataCentral(){\n  const dc = document.getElementById('dc')",
        "Data Central → initDataCentral()",
    )
    html = _replace_once(html, r"  // ---- numbers computed from the same data the dashboard tabs use\n.*?\n  \];\n(?=  const WORD)", DC_TILES_LIVE, "Data Central tile numbers")
    html = _replace_text(
        html,
        "  if (new URLSearchParams(location.search).get('view') === 'central') open();\n})();",
        "  if (new URLSearchParams(location.search).get('view') === 'central') open();\n}",
        "close initDataCentral()",
    )
    steps.append("Deferred:  every top-level render into renderAll() / initDataCentral(), run after data loads")

    # ── Static HTML that held mock numbers → containers the live script fills ─
    html = _replace_once(
        html,
        r"<h1>Everything is on time\.<br>.*?</h1>\s*<p>The last build finished.*?</p>",
        '<h1 id="heroH1">Checking the data&hellip;</h1>\n    <p id="heroP">Loading the latest check results.</p>',
        "hero headline",
    )
    html = _replace_once(
        html,
        r'<span class="badge">TEG \+ GBGS &middot; 90 active stores</span> <span class="badge">TEG_PROD\.DW</span> <span class="badge">Checked [^<]*</span>',
        '<span class="badge" id="badgeScope">TEG + GBGS</span> <span class="badge">TEG_PROD.DW</span> <span class="badge" id="badgeChecked">Checking&hellip;</span>',
        "hero badges",
    )
    html = _replace_once(html, r'<div class="banner" id="banner">.*?\n  </div>\n(?=\n  <!-- CATEGORY STRIP -->)', '<div class="banner" id="banner"></div>\n', "status banner")
    html = _replace_once(html, r'<div class="cats">\n.*?\n  </div>\n(?=\n  <!-- TABS -->)', '<div class="cats" id="cats"></div>\n', "category strip")
    html = _replace_once(html, r"<tbody>\s*<tr><td>GBGS Schaumburg</td>.*?</tbody>", '<tbody id="outTbl"></tbody>', "yesterday's outliers table")
    html = _kpis(html, "Unknown share of Revel revenue", "unkKpis", "unattributed revenue KPIs")
    html = _replace_text(html, '<div class="note" style="margin:0">No stores flagged.</div>', '<div id="npsNone"></div>', "stores with no NPS")
    html = _replace_once(
        html,
        r'<div class="hint">Check 12a\. In the build:.*?</div>\s*<div class="note" style="margin:0">Placeholder\..*?</div>',
        '<div class="hint">Check 12a. Responses per store per week for the last 10 weeks, zero-filled. A full week under 40% of the store\'s trailing 4-week average is yellow and a week with none is red. The current week (*) is partial and never flagged.</div>\n'
        '      <div class="legend">\n'
        '        <span><i style="background:var(--cell-ok)"></i>Normal</span>\n'
        '        <span><i style="background:var(--yellow)"></i>Under 40% of the trailing average</span>\n'
        '        <span><i style="background:var(--red)"></i>No responses</span>\n'
        '        <span><i style="background:var(--cell-na)"></i>No baseline yet, or the current week</span>\n'
        '      </div>\n'
        '      <div class="hm-scroll"><table class="hm" id="npsHm"></table></div>',
        "weekly NPS placeholder → Check 12a heatmap",
    )
    html = _replace_once(html, r'<tbody>\s*<tr>\s*<td><span class="chip RED">Taking bookings</span></td>.*?</tbody>', "<tbody></tbody>", "Xola sellers table")
    html = _replace_text(html, "<h2>Suggested values for the Denver row</h2>", '<h2 id="sgTitle">Suggested values for the new store\'s row</h2>', "suggestions heading")
    html = _kpis(html, "Placeholder rows in the sheet", "phKpis", "placeholder KPIs")
    html = _kpis(html, "Rows checked", "bkKpis", "bookings integrity KPIs")
    html = _kpis(html, "Duplicate store-days", "aggKpis", "aggregate integrity KPIs")
    html = _kpis(html, "Open stores with 0 slots", "slotKpis", "slots KPIs")
    html = _replace_once(
        html,
        r"<h2>Targets loaded for [^<]*</h2>\s*<div class=\"hint\">Check 14\.[^<]*</div>",
        '<h2 id="tgTitle">Targets loaded for next month</h2>\n      <div class="hint" id="tgHint">Check 14.</div>',
        "targets heading",
    )
    html = _replace_text(html, '<input type="checkbox" id="showAll"> Show all 90 stores', '<input type="checkbox" id="showAll"> Show all <span id="storeN">90</span> stores', "show-all label")
    steps.append("Replaced:  hard-coded hero, banner, categories, tables and KPI blocks with live containers")

    # ── Step 5: live loader + initDashboard(), then the Cognito gate ─────────
    if "</body>" not in html:
        sys.exit("ERROR: could not find </body> — is this the right source file?")
    html = html.replace("</body>", LIVE_SCRIPT + "\n" + AUTH_SCRIPT + "\n</body>", 1)
    steps.append("Injected:  live loader (authenticated API, cache served by the API, visible errors) + initDashboard()")
    steps.append("Injected:  Cognito auth-gate script before </body>")

    leftovers = [s for s in ("mulberry(si", "const ATTN = [", "const NPS_UNM = [", "const TP = {", "TG_EXCEPT", "Fifteen things", "NEWSTORES.has") if s in html]
    if leftovers:
        sys.exit(f"ERROR: mock data still present after the build: {leftovers}")

    DEST.write_text(html, encoding="utf-8")

    src_kb = SRC.stat().st_size // 1024
    dest_kb = DEST.stat().st_size // 1024

    print(f"\nSource:  {SRC.name}  ({src_kb} KB)")
    print(f"Output:  {DEST.name}  ({dest_kb} KB)")
    print()
    for s in steps:
        print(f"  {s}")
    print()
    print("Next steps:")
    print("  1. cp .env.example .env  # fill in Snowflake key-pair credentials")
    print("  2. pip install -r requirements.txt")
    print("  3. python -m uvicorn api.main:app --reload")
    print(f"  4. python -m http.server 8080, then open http://127.0.0.1:8080/{DEST.name}")
    if COGNITO_CLIENT.startswith("REPLACE"):
        print()
        print("  After the first `terraform apply`, copy the real Cognito client id from")
        print("  `terraform output cognito_client_id` into COGNITO_CLIENT at the top of")
        print("  this script and rebuild.")


if __name__ == "__main__":
    main()
