import os
import sys
import html
import json
import snowflake.connector
import time
from datetime import datetime

# Snowflake connection
conn = snowflake.connector.connect(
    account=os.environ.get('SNOWFLAKE_ACCOUNT', 'SQUARE'),
    user=os.environ.get('SNOWFLAKE_USER', 'MTK_DASH_SYNC@SQUAREUP.COM'),
    password=os.environ['SNOWFLAKE_PASSWORD'],
    warehouse=os.environ.get('SNOWFLAKE_WAREHOUSE', 'ETL__SMALL'),
)
cur = conn.cursor()

TABLE = 'LOGICGATE_SF.PUBLIC.POLICY_DASHBOARD'
EXCL = "NAME NOT IN ('EXAMPLE - End User Training Policy Draft', 'Example - demo Draft', 'Test Document - KE Draft')"

def q(sql):
    cur.execute(sql)
    return cur.fetchall()

def q1(sql):
    return q(sql)[0][0]

def e(text):
    return html.escape(str(text))

# KPIs
kpi_active  = q1(f"SELECT COUNT(DISTINCT PWF_RECORD_ID) FROM {TABLE} WHERE WORKFLOW_STATUS = 'Published' AND {EXCL}")
kpi_intake  = q1(f"SELECT COUNT(DISTINCT PWF_RECORD_ID) FROM {TABLE} WHERE WORKFLOW_STATUS = 'New Request Draft in Progress' AND {EXCL}")
kpi_retired = q1(f"SELECT COUNT(DISTINCT PWF_RECORD_ID) FROM {TABLE} WHERE WORKFLOW_STATUS = 'Retired' AND {EXCL}")

# Approved in last 30 days
approved_rows = q(f"""
SELECT DISTINCT NAME, LEGAL_ENTITY, DOMAIN
FROM {TABLE}
WHERE WORKFLOW_STATUS = 'Published' AND {EXCL}
AND DATE_OF_FINAL_APPROVAL >= DATEADD(day, -30, CURRENT_DATE())
AND DRAFT_STATUS = 'Active'
ORDER BY NAME
""")
approved_count = len(approved_rows)
approved_html = ''
for row in approved_rows:
    name = str(row[0]).replace('\n', ' ').replace('\r', '') if row[0] else ''
    if name.endswith(' Draft'):
        name = name[:-6]
    le = html.escape(str(row[1]) if row[1] else '')
    domain = html.escape(str(row[2]) if row[2] else '')
    approved_html += '<tr><td>' + html.escape(name) + '</td><td>' + le + '</td><td>' + domain + '</td></tr>'

def doctype_filter(dt):
    if dt == 'policy':   return ' AND DOCUMENT_TYPE = 1'
    if dt == 'standard': return ' AND DOCUMENT_TYPE = 2'
    return ''

def build_dim1(status_cond):
    result = {}
    for filt in ('all', 'policy', 'standard'):
        df = doctype_filter(filt)
        # domain
        rows = q(f"""SELECT DOMAIN, COUNT(DISTINCT PWF_RECORD_ID) as cnt
                     FROM {TABLE} WHERE {status_cond} AND {EXCL}{df}
                     GROUP BY DOMAIN ORDER BY cnt DESC""")
        labels = [str(r[0]) if r[0] else 'Unknown' for r in rows]
        data   = [int(r[1]) for r in rows]
        total  = q1(f"SELECT COUNT(DISTINCT PWF_RECORD_ID) FROM {TABLE} WHERE {status_cond} AND {EXCL}{df}")
        result[f'domain.{filt}'] = {'l': labels, 'd': data, 't': int(total)}

        # tier
        rows = q(f"""SELECT TIER, COUNT(DISTINCT PWF_RECORD_ID) as cnt
                     FROM {TABLE} WHERE {status_cond} AND {EXCL}{df}
                     GROUP BY TIER ORDER BY cnt DESC""")
        labels = [str(r[0]) if r[0] else 'Unknown' for r in rows]
        data   = [int(r[1]) for r in rows]
        total  = sum(data)
        result[f'tier.{filt}'] = {'l': labels, 'd': data, 't': int(total)}

        # doctype
        rows = q(f"""SELECT CASE WHEN DOCUMENT_TYPE=1 THEN 'Policy'
                          WHEN DOCUMENT_TYPE=2 THEN 'Standard'
                          ELSE 'Procedure' END as dt,
                     COUNT(DISTINCT PWF_RECORD_ID) as cnt
                     FROM {TABLE} WHERE {status_cond} AND {EXCL}{df}
                     GROUP BY dt ORDER BY cnt DESC""")
        labels = [str(r[0]) for r in rows]
        data   = [int(r[1]) for r in rows]
        total  = sum(data)
        result[f'doctype.{filt}'] = {'l': labels, 'd': data, 't': int(total)}
    return result

def build_org(status_cond):
    result = {}
    for filt in ('all', 'policy', 'standard'):
        df = doctype_filter(filt)
        for col, key in [('BUSINESS','business'), ('LEGAL_ENTITY','legal'), ('REGION','region')]:
            rows = q(f"""SELECT {col}, COUNT(DISTINCT PWF_RECORD_ID) as cnt
                         FROM {TABLE} WHERE {status_cond} AND {EXCL}{df}
                         GROUP BY {col} ORDER BY cnt DESC""")
            labels = [str(r[0]) if r[0] else 'Unknown' for r in rows]
            data   = [int(r[1]) for r in rows]
            if len(labels) > 8:
                other_sum = sum(data[8:])
                labels = labels[:8] + ['Other']
                data   = data[:8] + [other_sum]
            m = max(data) if data else 0
            result[f'{key}.{filt}'] = {'l': labels, 'd': data, 'm': m}
    return result

A_DIM1 = build_dim1("WORKFLOW_STATUS = 'Published'")
A_ORG  = build_org("WORKFLOW_STATUS = 'Published'")
I_DIM1 = build_dim1("WORKFLOW_STATUS = 'New Request Draft in Progress'")
I_ORG  = build_org("WORKFLOW_STATUS = 'New Request Draft in Progress'")

lc_active_rows = q(f"""
SELECT DOMAIN,
  COUNT(DISTINCT CASE WHEN CURRENT_STEP='Document Drafting (Submitter)' THEN PWF_RECORD_ID END) as current_ct,
  COUNT(DISTINCT CASE WHEN CURRENT_STEP IN ('Stakeholder Review + QC','QC / Assign Reviewers') THEN PWF_RECORD_ID END) as qc,
  COUNT(DISTINCT CASE WHEN CURRENT_STEP IN ('Draft Review (Board/Committee/Leadership/Document Owner)','Draft Review (Board)','Draft Review (Committee)','Draft Review (Leadership)','Draft Review (Document Owner)','Publication') THEN PWF_RECORD_ID END) as approvals,
  COUNT(DISTINCT PWF_RECORD_ID) as total
FROM {TABLE}
WHERE WORKFLOW_STATUS='Published' AND DRAFT_STATUS='In-Progress' AND {EXCL}
GROUP BY DOMAIN ORDER BY total DESC
""")

lc_intake_rows = q(f"""
SELECT DOMAIN,
  COUNT(DISTINCT CASE WHEN CURRENT_STEP='Document Drafting (Submitter)' THEN PWF_RECORD_ID END) as new_drafting,
  COUNT(DISTINCT CASE WHEN CURRENT_STEP IN ('Stakeholder Review + QC','QC / Assign Reviewers') THEN PWF_RECORD_ID END) as qc,
  COUNT(DISTINCT CASE WHEN CURRENT_STEP IN ('Draft Review (Board/Committee/Leadership/Document Owner)','Draft Review (Board)','Draft Review (Committee)','Draft Review (Leadership)','Draft Review (Document Owner)','Publication') THEN PWF_RECORD_ID END) as approvals,
  COUNT(DISTINCT PWF_RECORD_ID) as total
FROM {TABLE}
WHERE WORKFLOW_STATUS='New Request Draft in Progress' AND {EXCL}
GROUP BY DOMAIN ORDER BY total DESC
""")

# Build lifecycle HTML tables
lc_active_html = ''
lc_active_totals = [0, 0, 0, 0]
for row in lc_active_rows:
    dom = html.escape(str(row[0]) if row[0] else 'Unknown')
    c, qc, ap, t = int(row[1]), int(row[2]), int(row[3]), int(row[4])
    lc_active_html += '<tr><td>' + dom + '</td><td>' + str(c) + '</td><td>' + str(qc) + '</td><td>' + str(ap) + '</td><td><strong>' + str(t) + '</strong></td></tr>'
    lc_active_totals[0] += c
    lc_active_totals[1] += qc
    lc_active_totals[2] += ap
    lc_active_totals[3] += t
lc_active_html += '<tr style="font-weight:700;border-top:2px solid #333"><td>Total</td><td>' + str(lc_active_totals[0]) + '</td><td>' + str(lc_active_totals[1]) + '</td><td>' + str(lc_active_totals[2]) + '</td><td>' + str(lc_active_totals[3]) + '</td></tr>'

lc_intake_html = ''
lc_intake_totals = [0, 0, 0, 0]
for row in lc_intake_rows:
    dom = html.escape(str(row[0]) if row[0] else 'Unknown')
    nd, qc, ap, t = int(row[1]), int(row[2]), int(row[3]), int(row[4])
    lc_intake_html += '<tr><td>' + dom + '</td><td>' + str(nd) + '</td><td>' + str(qc) + '</td><td>' + str(ap) + '</td><td><strong>' + str(t) + '</strong></td></tr>'
    lc_intake_totals[0] += nd
    lc_intake_totals[1] += qc
    lc_intake_totals[2] += ap
    lc_intake_totals[3] += t
lc_intake_html += '<tr style="font-weight:700;border-top:2px solid #333"><td>Total</td><td>' + str(lc_intake_totals[0]) + '</td><td>' + str(lc_intake_totals[1]) + '</td><td>' + str(lc_intake_totals[2]) + '</td><td>' + str(lc_intake_totals[3]) + '</td></tr>'

due_status_rows = q(f"""
SELECT DUE_DATE_STATUS, COUNT(DISTINCT PWF_RECORD_ID) as cnt
FROM {TABLE}
WHERE WORKFLOW_STATUS = 'Published' AND DRAFT_STATUS = 'In-Progress' AND {EXCL}
AND DUE_DATE_STATUS != 'Complete'
GROUP BY DUE_DATE_STATUS ORDER BY cnt DESC
""")
due_labels = [str(r[0]) for r in due_status_rows]
due_data   = [int(r[1]) for r in due_status_rows]
due_max    = max(due_data) if due_data else 0

due_drill_rows = q(f"""
SELECT DISTINCT NAME, LEGAL_ENTITY, DOMAIN, DUE_DATE_STATUS
FROM {TABLE}
WHERE WORKFLOW_STATUS = 'Published' AND DRAFT_STATUS = 'In-Progress' AND {EXCL}
AND DUE_DATE_STATUS IN ('Current','Coming Due','Extended','Pending Review','Past Due','Overdue','Ext. Coming Due','Overdue Past Extension')
ORDER BY DUE_DATE_STATUS, NAME
""")
due_drill = {}
for name_raw, le, domain, status in due_drill_rows:
    name = str(name_raw).replace('\n', ' ').replace('\r', '')
    if name.endswith(' Draft'):
        name = name[:-6]
    status = str(status)
    if status not in due_drill:
        due_drill[status] = []
    due_drill[status].append({
        'n': name,
        'le': str(le) if le else '',
        'd': str(domain) if domain else ''
    })

schedule_rows = q(f"""
SELECT TO_CHAR(DUE_DATE, 'YYYY-MM') as month, COUNT(DISTINCT PWF_RECORD_ID) as cnt
FROM {TABLE}
WHERE WORKFLOW_STATUS = 'Published' AND DRAFT_STATUS = 'In-Progress' AND {EXCL}
AND DUE_DATE >= '2026-01-01' AND DUE_DATE < '2027-01-01'
GROUP BY TO_CHAR(DUE_DATE, 'YYYY-MM') ORDER BY month
""")

month_map = {
    '2026-01':'Jan','2026-02':'Feb','2026-03':'Mar','2026-04':'Apr',
    '2026-05':'May','2026-06':'Jun','2026-07':'Jul','2026-08':'Aug',
    '2026-09':'Sep','2026-10':'Oct','2026-11':'Nov','2026-12':'Dec'
}
sched_labels = [month_map.get(str(r[0]), str(r[0])) for r in schedule_rows]
sched_data   = [int(r[1]) for r in schedule_rows]

updated_at = datetime.now().strftime('%b %d, %Y at %I:%M %p')
conn.close()

# ══════════════════════════════════════════════════════════════════════
# HTML GENERATION
# ══════════════════════════════════════════════════════════════════════

def js_obj(d):
    parts = []
    parts.append(f"l:{json.dumps(d['l'])}")
    parts.append(f"d:{json.dumps(d['d'])}")
    if 't' in d:
        parts.append(f"t:{d['t']}")
    if 'm' in d:
        parts.append(f"m:{d['m']}")
    return '{' + ','.join(parts) + '}'

def build_js_data(name, data_dict):
    entries = []
    for k, v in data_dict.items():
        entries.append(f"'{k}':{js_obj(v)}")
    nl = '\n'
    joiner = ',' + nl
    return f"const {name}={{{nl}{joiner.join(entries)}{nl}}};"

def build_due_drill_js(dd):
    parts = []
    bs = chr(92) + chr(92)
    sq = chr(92) + chr(39)
    q = chr(39)
    for status, rows in dd.items():
        row_strs = []
        for r in rows:
            n_esc = r['n'].replace('\n', ' ').replace('\r', '').replace(chr(92), bs).replace(chr(39), sq)
            le_esc = r['le'].replace('\n', ' ').replace('\r', '').replace(chr(92), bs).replace(chr(39), sq)
            d_esc = r['d'].replace('\n', ' ').replace('\r', '').replace(chr(92), bs).replace(chr(39), sq)
            row_strs.append("{n:" + q + n_esc + q + ",le:" + q + le_esc + q + ",d:" + q + d_esc + q + "}")
        status_esc = status.replace(chr(39), sq)
        nl = chr(10)
        joiner = ',' + nl
        parts.append(q + status_esc + q + ":[" + nl + joiner.join(row_strs) + nl + "]")
    nl = chr(10)
    joiner = ',' + nl
    return "{" + nl + joiner.join(parts) + nl + "}"

due_drill_js = build_due_drill_js(due_drill)
a_dim1_js = build_js_data('A_DIM1', A_DIM1)
a_org_js  = build_js_data('A_ORG', A_ORG)
i_dim1_js = build_js_data('I_DIM1', I_DIM1)
i_org_js  = build_js_data('I_ORG', I_ORG)
sched_labels_js = json.dumps(sched_labels)
sched_data_js   = json.dumps(sched_data)
due_labels_js = json.dumps(due_labels)
due_data_js   = json.dumps(due_data)
due_colors_js = json.dumps(['#F59E0B' if s == 'Extended' else '#F87171' if s == 'Pending Review' else '#fff' for s in due_labels])

# Read the HTML template approach - build identical structure
HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Policy Governance Reporting</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#000;color:#fff;font-family:'Inter',sans-serif;min-height:100vh}}
header{{background:#000;border-bottom:1px solid #222;padding:16px 32px;display:flex;align-items:center;gap:14px}}
.logo-svg{{width:32px;height:32px}}
.logo-text{{font-size:20px;font-weight:700;letter-spacing:1px}}
.header-divider{{width:1px;height:28px;background:#333;margin:0 8px}}
.header-title{{font-size:16px;font-weight:600;color:#ccc}}
.header-subtitle{{font-size:12px;color:#999;margin-left:auto}}
.container{{max-width:1200px;margin:0 auto;padding:24px 32px 48px}}
.main-tab-bar{{display:flex;gap:8px;margin-bottom:20px}}
.main-tab-btn{{padding:10px 24px;border-radius:24px;border:1px solid #444;background:#111;color:#aaa;font-family:'Inter',sans-serif;font-size:14px;font-weight:500;cursor:pointer;transition:all .2s}}
.main-tab-btn.active{{background:#fff;color:#000;border-color:#fff}}
.section-title{{font-size:14px;font-weight:600;color:#aaa;text-transform:uppercase;letter-spacing:1px;margin:28px 0 14px}}
.kpi-row{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:24px}}
.kpi-card{{background:#111;border:1px solid #333;border-radius:12px;padding:20px 24px;display:flex;align-items:center;gap:16px;border-left:4px solid #fff;transition:all .2s}}
.kpi-card.clickable{{cursor:pointer}}.kpi-card.clickable:hover{{border-color:#444;background:#181818}}
.kpi-card.active-sel{{border-left-color:#fff;background:#181818}}
.kpi-card.retired{{border-left-color:#777;opacity:.7}}
.kpi-icon{{width:36px;height:36px;flex-shrink:0}}
.kpi-info{{display:flex;flex-direction:column}}
.kpi-value{{font-size:28px;font-weight:700}}
.kpi-label{{font-size:12px;color:#aaa;font-weight:500}}
.grid-2{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}}
.card{{background:#111;border:1px solid #333;border-radius:12px;padding:20px;display:flex;flex-direction:column}}
.card.full-width{{grid-column:1/-1}}
.card-header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;gap:8px;flex-wrap:wrap}}
.card-title{{font-size:13px;font-weight:600;color:#ccc}}
.card-controls{{display:flex;align-items:center;gap:8px}}
.dim-select{{background:#000;color:#ccc;border:1px solid #444;border-radius:6px;padding:4px 8px;font-size:11px;font-family:'Inter',sans-serif}}
.pill-group{{display:flex;border:1px solid #444;border-radius:6px;overflow:hidden}}
.pill-btn{{padding:4px 10px;font-size:11px;background:#000;color:#aaa;border:none;cursor:pointer;font-family:'Inter',sans-serif;transition:all .15s}}
.pill-btn.active{{background:#fff;color:#000}}
.stat-tile{{display:inline-flex;align-items:center;gap:8px;background:#111;border:1px solid #222;border-radius:8px;padding:8px 14px;margin-bottom:16px;font-size:12px;color:#ccc}}
.stat-tile .val{{font-weight:700;color:#fff;font-size:16px}}
.cc{{flex:1;min-height:0}}
.status-table{{width:100%;border-collapse:collapse;font-size:13px}}
.status-table th{{text-align:left;padding:10px 12px;color:#aaa;font-weight:500;border-bottom:1px solid #222;font-size:11px;text-transform:uppercase;letter-spacing:.5px}}
.status-table td{{padding:10px 12px;border-bottom:1px solid #222;color:#ddd}}
.status-table .total-row td{{border-top:2px solid #333;font-weight:700;color:#fff}}
.due-layout{{display:flex;gap:0;flex:1;min-height:0;overflow:hidden}}
.due-chart{{flex:1;min-width:0;min-height:0;display:flex;align-items:stretch}}
.due-key{{width:320px;flex-shrink:0;border-left:1px solid #333;padding:10px 16px;display:flex;flex-direction:column;justify-content:center;gap:6px}}
.due-item{{display:flex;align-items:center;gap:6px;font-size:11px}}
.due-dot{{width:8px;height:8px;border-radius:50%;flex-shrink:0}}
.due-term{{font-weight:600;color:#ccc;min-width:110px;white-space:nowrap}}
.due-desc{{color:#999;font-size:10px}}
.hidden{{display:none!important}}

.approved-tile{{display:flex;align-items:center;gap:10px;background:#111;border:1px solid #222;border-radius:10px;padding:12px 18px;margin-top:16px;cursor:pointer;transition:all .2s;user-select:none}}
.approved-tile:hover{{background:#181818;border-color:#333}}
.approved-tile .at-icon{{flex-shrink:0}}
.approved-tile .at-label{{font-size:13px;color:#ccc}}
.approved-tile .at-val{{font-size:20px;font-weight:700;color:#fff;margin-left:auto}}
.approved-tile .at-arrow{{color:#aaa;font-size:14px;margin-left:8px;transition:transform .2s}}
.approved-tile.open .at-arrow{{transform:rotate(180deg)}}
.approved-drilldown{{max-height:0;overflow:hidden;transition:max-height .3s ease}}
.approved-drilldown.open{{max-height:400px}}
.approved-drilldown table{{width:100%;border-collapse:collapse;font-size:12px;margin-top:0}}
.approved-drilldown th{{text-align:left;padding:8px 12px;color:#999;font-weight:500;border-bottom:1px solid #222;font-size:11px;text-transform:uppercase;letter-spacing:.5px}}
.approved-drilldown td{{padding:8px 12px;border-bottom:1px solid #222;color:#ddd}}
.approved-drilldown td:first-child{{color:#fff;font-weight:500}}
.due-drilldown{{margin-top:12px;max-height:300px;overflow-y:auto;display:none}}
.due-drilldown.open{{display:block}}
.due-drilldown table{{width:100%;border-collapse:collapse;font-size:12px}}
.due-drilldown th{{text-align:left;padding:8px 12px;color:#999;font-weight:500;border-bottom:1px solid #222;font-size:11px;text-transform:uppercase;letter-spacing:.5px;position:sticky;top:0;background:#111}}
.due-drilldown td{{padding:8px 12px;border-bottom:1px solid #222;color:#ddd}}
.due-drilldown td:first-child{{color:#fff;font-weight:500}}
.due-drilldown-label{{font-size:12px;color:#aaa;margin-bottom:8px;display:flex;align-items:center;gap:6px}}
.due-drilldown-label .dd-status{{color:#fff;font-weight:600}}
.due-drilldown-label .dd-close{{cursor:pointer;margin-left:auto;color:#aaa;font-size:14px}}
.due-drilldown-label .dd-close:hover{{color:#fff}}
.dd-search{{background:#000;color:#ddd;border:1px solid #444;border-radius:6px;padding:5px 10px;font-size:12px;font-family:'Inter',sans-serif;width:220px;margin-left:12px}}
.dd-search::placeholder{{color:#777}}

</style>
</head>
<body>
<header>
<svg class="logo-svg" viewBox="0 0 32 32" fill="none"><rect x="1" y="1" width="8" height="8" rx="2" fill="#fff"/><rect x="12" y="1" width="8" height="8" rx="2" fill="#fff"/><rect x="23" y="1" width="8" height="8" rx="2" fill="#fff"/><rect x="1" y="12" width="8" height="8" rx="2" fill="#fff"/><rect x="12" y="12" width="8" height="8" rx="2" fill="#fff"/><rect x="23" y="12" width="8" height="8" rx="2" fill="#fff"/><rect x="1" y="23" width="8" height="8" rx="2" fill="#fff"/><rect x="12" y="23" width="8" height="8" rx="2" fill="#fff"/><rect x="23" y="23" width="8" height="8" rx="2" fill="#fff"/></svg>
<span class="logo-text">block</span>
<div class="header-divider"></div>
<span class="header-title">Policy Governance Reporting</span>
<div class="header-subtitle" style="text-align:right;line-height:1.5"><div>Last updated: {updated_at}</div><div style="color:#666;font-size:10px">Source: LogicGate via Snowflake</div></div>
</header>
<div class="container">
<div class="main-tab-bar">
<button class="main-tab-btn active" id="mt-inv" onclick="switchMainTab('inventory')">Inventory Overview</button>
<button class="main-tab-btn" id="mt-lc" onclick="switchMainTab('lifecycle')">Document Lifecycle Status</button>
</div>

<!-- ===== MAIN TAB 1: INVENTORY OVERVIEW ===== -->
<div id="main-tab-inventory">
<div class="section-title">Inventory Overview</div>
<div class="kpi-row">
<div class="kpi-card clickable active-sel" id="kpi-active" onclick="showActiveTab()">
<svg class="kpi-icon" viewBox="0 0 36 36" fill="none"><circle cx="18" cy="18" r="16" stroke="#fff" stroke-width="2"/><path d="M11 18l5 5 9-9" stroke="#fff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
<div class="kpi-info"><span class="kpi-value">{kpi_active}</span><span class="kpi-label">Active</span></div>
</div>
<div class="kpi-card clickable" id="kpi-intake" onclick="showIntakeTab()">
<svg class="kpi-icon" viewBox="0 0 36 36" fill="none"><path d="M18 4v18m0 0l-6-6m6 6l6-6" stroke="#fff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M6 26c0 2 2 4 4 4h16c2 0 4-2 4-4" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/></svg>
<div class="kpi-info"><span class="kpi-value">{kpi_intake}</span><span class="kpi-label">Intake</span></div>
</div>
<div class="kpi-card retired">
<svg class="kpi-icon" viewBox="0 0 36 36" fill="none"><rect x="5" y="8" width="26" height="22" rx="3" stroke="#777" stroke-width="2"/><path d="M5 14h26" stroke="#777" stroke-width="2"/><path d="M14 19h8" stroke="#777" stroke-width="2" stroke-linecap="round"/></svg>
<div class="kpi-info"><span class="kpi-value" style="color:#777">{kpi_retired}</span><span class="kpi-label">Retired</span></div>
</div>
</div>

<div class="pill-group" id="inv-pill-group" style="margin-bottom:20px;display:inline-flex">
<button class="pill-btn active" id="inv-pill-active" onclick="showActiveTab()">Active</button>
<button class="pill-btn" id="inv-pill-intake" onclick="showIntakeTab()">Intake</button>
</div>

<!-- Active sub-tab -->
<div id="tab-active">
<div class="section-title">Active Documents Breakdown</div>

<div class="grid-2">
<div class="card" style="height:300px">
<div class="card-header">
<span class="card-title">Distribution</span>
<div class="card-controls">
<select class="dim-select" id="a-dim1-sel" onchange="updateADim1()"><option value="domain">By Domain</option><option value="tier">By Tier</option><option value="doctype">By Document Type</option></select>
<div class="pill-group" id="a-dim1-pills">
<button class="pill-btn active" onclick="setAPill('dim1','all',this)">All</button>
<button class="pill-btn" onclick="setAPill('dim1','policy',this)">Policy</button>
<button class="pill-btn" onclick="setAPill('dim1','standard',this)">Standard</button>
</div>
</div>
</div>
<canvas id="a-dim1" class="cc" style="flex:1;min-height:0"></canvas>
</div>
<div class="card" style="height:300px">
<div class="card-header">
<span class="card-title">Organization</span>
<div class="card-controls">
<select class="dim-select" id="a-org-sel" onchange="updateAOrg()"><option value="business">By Business</option><option value="legal">By Legal Entity</option><option value="region">By Region</option></select>
<div class="pill-group" id="a-org-pills">
<button class="pill-btn active" onclick="setAPill('org','all',this)">All</button>
<button class="pill-btn" onclick="setAPill('org','policy',this)">Policy</button>
<button class="pill-btn" onclick="setAPill('org','standard',this)">Standard</button>
</div>
</div>
</div>
<canvas id="a-org" class="cc" style="flex:1;min-height:0"></canvas>
</div>
</div>
<div class="approved-tile" id="approved-toggle" onclick="toggleApproved()">
<svg class="at-icon" width="18" height="18" viewBox="0 0 18 18" fill="none"><circle cx="9" cy="9" r="7.5" stroke="#aaa" stroke-width="1.5"/><path d="M9 4.5v4.5l3.5 2" stroke="#aaa" stroke-width="1.5" stroke-linecap="round"/></svg>
<span class="at-label">Approved in last 30 days</span>
<span class="at-val">{approved_count}</span>
<span class="at-arrow">&#9660;</span>
</div>
<div class="approved-drilldown" id="approved-drilldown">
<table>
<thead><tr><th>Document Name</th><th>Legal Entity</th><th>Domain</th></tr></thead>
<tbody>
{approved_html}</tbody>
</table>
</div>
<div class="section-title">Document Review Schedule</div>
<div class="card full-width" style="height:250px">
<canvas id="a-schedule" class="cc" style="flex:1;min-height:0"></canvas>
</div>
</div>

<!-- Intake sub-tab -->
<div id="tab-intake" class="hidden">
<div class="section-title">Intake Documents Breakdown</div>
<div class="grid-2">
<div class="card" style="height:300px">
<div class="card-header">
<span class="card-title">Distribution</span>
<div class="card-controls">
<select class="dim-select" id="i-dim1-sel" onchange="updateIDim1()"><option value="domain">By Domain</option><option value="tier">By Tier</option><option value="doctype">By Document Type</option></select>
<div class="pill-group" id="i-dim1-pills">
<button class="pill-btn active" onclick="setIPill('dim1','all',this)">All</button>
<button class="pill-btn" onclick="setIPill('dim1','policy',this)">Policy</button>
<button class="pill-btn" onclick="setIPill('dim1','standard',this)">Standard</button>
</div>
</div>
</div>
<canvas id="i-dim1" class="cc" style="flex:1;min-height:0"></canvas>
</div>
<div class="card" style="height:300px">
<div class="card-header">
<span class="card-title">Organization</span>
<div class="card-controls">
<select class="dim-select" id="i-org-sel" onchange="updateIOrg()"><option value="business">By Business</option><option value="legal">By Legal Entity</option><option value="region">By Region</option></select>
<div class="pill-group" id="i-org-pills">
<button class="pill-btn active" onclick="setIPill('org','all',this)">All</button>
<button class="pill-btn" onclick="setIPill('org','policy',this)">Policy</button>
<button class="pill-btn" onclick="setIPill('org','standard',this)">Standard</button>
</div>
</div>
</div>
<canvas id="i-org" class="cc" style="flex:1;min-height:0"></canvas>
</div>
</div>
</div>
</div>

<!-- ===== MAIN TAB 2: DOCUMENT LIFECYCLE STATUS ===== -->
<div id="main-tab-lifecycle" class="hidden">
<div class="pill-group" style="margin-bottom:20px;display:inline-flex">
<button class="pill-btn active" id="lc-pill-active" onclick="switchLcTab('active')">Active</button>
<button class="pill-btn" id="lc-pill-intake" onclick="switchLcTab('intake')">Intake</button>
</div>

<!-- Lifecycle Active -->
<div id="lc-active">
<div class="section-title">Document Lifecycle Tracking</div>
<div class="card full-width" style="margin-bottom:16px">
<table class="status-table">
<thead><tr><th>Domain</th><th>Current</th><th>Under QC</th><th>In Approvals</th><th>Total</th></tr></thead>
<tbody>
{lc_active_html}
</tbody>
</table>
</div>
<div class="card full-width" style="height:300px;margin-bottom:16px">
<div class="card-header"><span class="card-title">By Due Date Status</span></div>
<div style="font-size:11px;color:#aaa;margin-bottom:8px;display:flex;align-items:center;gap:6px"><svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M7 1a6 6 0 100 12A6 6 0 007 1z" stroke="#aaa" stroke-width="1.2"/><path d="M6 5.5h2v4H6z" fill="#aaa"/><circle cx="7" cy="4" r=".8" fill="#aaa"/></svg>Click on a bar to view document details</div>
<div class="due-layout">
<div class="due-chart"><canvas id="lc-a-due" class="cc" style="flex:1;min-height:0"></canvas></div>
<div class="due-key">
<div class="due-item"><span class="due-dot" style="background:#fff"></span><span class="due-term">Current</span><span class="due-desc">Up to date</span></div>
<div class="due-item"><span class="due-dot" style="background:#ccc"></span><span class="due-term">Coming Due</span><span class="due-desc">Due within 90 days</span></div>
<div class="due-item"><span class="due-dot" style="background:#999"></span><span class="due-term">Pending Review</span><span class="due-desc">Past review date; within 30-day grace period</span></div>
<div class="due-item"><span class="due-dot" style="background:#777"></span><span class="due-term">Overdue</span><span class="due-desc">More than 30 days past due</span></div>
<div class="due-item"><span class="due-dot" style="background:#555"></span><span class="due-term">Extended</span><span class="due-desc">Extension granted</span></div>
<div class="due-item"><span class="due-dot" style="background:#444"></span><span class="due-term">Ext. Coming Due</span><span class="due-desc">Within 30 days of extended deadline</span></div>
<div class="due-item"><span class="due-dot" style="background:#333"></span><span class="due-term">Overdue Past Ext.</span><span class="due-desc">Beyond extended deadline</span></div>
</div>
</div>
</div>
<div class="due-drilldown" id="due-drilldown">
<div class="due-drilldown-label"><span>Showing: </span><span class="dd-status" id="dd-status-label"></span><input class="dd-search" id="dd-search" type="text" placeholder="Search documents..." oninput="filterDueDrilldown()"><span class="dd-close" onclick="closeDueDrilldown()">&#10005;</span></div>
<table>
<thead><tr><th>Document Name</th><th>Legal Entity</th><th>Domain</th></tr></thead>
<tbody id="dd-tbody"></tbody>
</table>
</div>

</div>

<!-- Lifecycle Intake -->
<div id="lc-intake" class="hidden">
<div class="section-title">Document Lifecycle Tracking</div>
<div class="card full-width" style="margin-bottom:16px">
<table class="status-table">
<thead><tr><th>Domain</th><th>New Drafting</th><th>Under QC</th><th>In Approvals</th><th>Total</th></tr></thead>
<tbody>
{lc_intake_html}
</tbody>
</table>
</div>
</div>
</div>

<script>
const DC=['#FFFFFF','#D4D4D4','#AAAAAA','#888888','#666666','#4D4D4D','#3A3A3A','#BBBBBB','#E0E0E0'];
const charts={{}};
const DUE_DRILL={due_drill_js};

/* ---- Bar value label plugin ---- */
const barPlugin={{id:'barLabels',afterDatasetsDraw(chart){{const ctx=chart.ctx;ctx.save();ctx.font='400 12px Inter';ctx.fillStyle='#aaa';chart.data.datasets.forEach((ds,di)=>{{const meta=chart.getDatasetMeta(di);meta.data.forEach((bar,i)=>{{const v=ds.data[i];if(chart.config.type==='bar'&&chart.options.indexAxis==='y'){{ctx.textAlign='left';ctx.textBaseline='middle';ctx.fillText(v,bar.x+6,bar.y)}}else if(chart.config.type==='bar'){{ctx.textAlign='center';ctx.textBaseline='bottom';ctx.fillText(v,bar.x,bar.y-4)}}}})}}); ctx.restore()}}}};
Chart.register(barPlugin);

/* ---- Chart builders ---- */
function makeDonut(id,labels,data,total){{
if(charts[id]){{charts[id].destroy();delete charts[id]}}
const ctx=document.getElementById(id);if(!ctx)return;
charts[id]=new Chart(ctx,{{type:'doughnut',data:{{labels,datasets:[{{data,backgroundColor:DC.slice(0,data.length),borderWidth:0}}]}},options:{{responsive:true,maintainAspectRatio:false,cutout:'65%',plugins:{{legend:{{position:'bottom',labels:{{color:'#aaa',font:{{size:11,family:'Inter'}},padding:10,boxWidth:12}}}},tooltip:{{enabled:true}}}}}},plugins:[{{id:'centerText',afterDraw(chart){{const{{ctx:c,width:w,height:h}}=chart;c.save();c.textAlign='center';c.textBaseline='middle';const cy=h/2-10;c.font='700 24px Inter';c.fillStyle='#fff';c.fillText(total,w/2,cy);c.font='400 10px Inter';c.fillStyle='#aaa';c.fillText('TOTAL',w/2,cy+20);c.restore()}}}},{{id:'segLabels',afterDraw(chart){{const{{ctx:c}}=chart;c.save();const meta=chart.getDatasetMeta(0);meta.data.forEach(function(arc,i){{var v=chart.data.datasets[0].data[i];if(v===0)return;var pos=arc.tooltipPosition();c.font='600 11px Inter';c.fillStyle='#000';c.textAlign='center';c.textBaseline='middle';c.fillText(v,pos.x,pos.y)}});c.restore()}}}}]}})
}}
function makeBar(id,labels,data,maxVal){{
if(charts[id]){{charts[id].destroy();delete charts[id]}}
const ctx=document.getElementById(id);if(!ctx)return;
charts[id]=new Chart(ctx,{{type:'bar',data:{{labels,datasets:[{{data,backgroundColor:'#fff',borderRadius:3,barThickness:18}}]}},options:{{responsive:true,maintainAspectRatio:false,indexAxis:'y',scales:{{x:{{display:false,max:maxVal*1.25}},y:{{ticks:{{color:'#aaa',font:{{size:11,family:'Inter'}}}},grid:{{display:false}},border:{{display:false}}}}}},plugins:{{legend:{{display:false}}}}}}}})
}}
function makeVertBar(id,labels,data){{
if(charts[id]){{charts[id].destroy();delete charts[id]}}
const ctx=document.getElementById(id);if(!ctx)return;
charts[id]=new Chart(ctx,{{type:'bar',data:{{labels,datasets:[{{data,backgroundColor:'#fff',borderRadius:3,barThickness:28}}]}},options:{{responsive:true,maintainAspectRatio:false,layout:{{padding:{{top:22}}}},scales:{{x:{{ticks:{{color:'#aaa',font:{{size:11,family:'Inter'}}}},grid:{{display:false}},border:{{display:false}}}},y:{{ticks:{{color:'#aaa',font:{{size:11,family:'Inter'}}}},grid:{{color:'#333'}},border:{{display:false}}}}}},plugins:{{legend:{{display:false}}}}}}}})
}}
function makeDueBar(id,labels,data,maxVal,colors){{
if(charts[id]){{charts[id].destroy();delete charts[id]}}
var bgColors=colors||labels.map(function(){{return '#fff'}});
var ctx=document.getElementById(id);if(!ctx)return;
charts[id]=new Chart(ctx,{{type:'bar',data:{{labels:labels,datasets:[{{data:data,backgroundColor:bgColors,borderRadius:3,barThickness:18}}]}},options:{{responsive:true,maintainAspectRatio:false,indexAxis:'y',onHover:function(e,els){{e.native.target.style.cursor=els.length>0?'pointer':'default'}},onClick:function(e,els){{if(els.length>0){{var idx=els[0].index;showDueDrilldown(labels[idx])}}}},scales:{{x:{{display:false,max:maxVal*1.25}},y:{{ticks:{{color:'#aaa',font:{{size:13,family:'Inter'}}}},grid:{{display:false}},border:{{display:false}}}}}},plugins:{{legend:{{display:false}}}}}}}})
}}

/* ---- DATA ---- */
{a_dim1_js}
{a_org_js}
{i_dim1_js}
{i_org_js}

/* ---- State ---- */
let aFilter={{dim1:'all',org:'all'}};
let iFilter={{dim1:'all',org:'all'}};

function getKey(sel,filter){{return sel+'.'+filter}}

function updateADim1(){{const s=document.getElementById('a-dim1-sel').value;const k=getKey(s,aFilter.dim1);const d=A_DIM1[k];if(d)makeDonut('a-dim1',d.l,d.d,d.t)}}
function updateAOrg(){{const s=document.getElementById('a-org-sel').value;const k=getKey(s,aFilter.org);const d=A_ORG[k];if(d)makeBar('a-org',d.l,d.d,d.m)}}
function updateIDim1(){{const s=document.getElementById('i-dim1-sel').value;const k=getKey(s,iFilter.dim1);const d=I_DIM1[k];if(d)makeDonut('i-dim1',d.l,d.d,d.t)}}
function updateIOrg(){{const s=document.getElementById('i-org-sel').value;const k=getKey(s,iFilter.org);const d=I_ORG[k];if(d)makeBar('i-org',d.l,d.d,d.m)}}

function setAPill(chart,val,btn){{
aFilter[chart]=val;
btn.parentElement.querySelectorAll('.pill-btn').forEach(b=>b.classList.remove('active'));
btn.classList.add('active');
if(chart==='dim1')updateADim1();else updateAOrg();
}}
function setIPill(chart,val,btn){{
iFilter[chart]=val;
btn.parentElement.querySelectorAll('.pill-btn').forEach(b=>b.classList.remove('active'));
btn.classList.add('active');
if(chart==='dim1')updateIDim1();else updateIOrg();
}}

/* ---- Tab switching ---- */
function switchMainTab(tab){{
document.getElementById('mt-inv').classList.toggle('active',tab==='inventory');
document.getElementById('mt-lc').classList.toggle('active',tab==='lifecycle');
document.getElementById('main-tab-inventory').classList.toggle('hidden',tab!=='inventory');
document.getElementById('main-tab-lifecycle').classList.toggle('hidden',tab!=='lifecycle');
if(tab==='inventory'){{buildInventoryCharts()}}
if(tab==='lifecycle'){{buildLcCharts()}}
}}

function showActiveTab(){{
document.getElementById('tab-active').classList.remove('hidden');
document.getElementById('tab-intake').classList.add('hidden');
document.getElementById('kpi-active').classList.add('active-sel');
document.getElementById('kpi-intake').classList.remove('active-sel');
document.getElementById('inv-pill-active').classList.add('active');
document.getElementById('inv-pill-intake').classList.remove('active');
updateADim1();updateAOrg();makeVertBar('a-schedule',{sched_labels_js},{sched_data_js});
}}
function showIntakeTab(){{
document.getElementById('tab-intake').classList.remove('hidden');
document.getElementById('tab-active').classList.add('hidden');
document.getElementById('kpi-intake').classList.add('active-sel');
document.getElementById('kpi-active').classList.remove('active-sel');
document.getElementById('inv-pill-intake').classList.add('active');
document.getElementById('inv-pill-active').classList.remove('active');
updateIDim1();updateIOrg();
}}

function switchLcTab(tab){{
document.getElementById('lc-pill-active').classList.toggle('active',tab==='active');
document.getElementById('lc-pill-intake').classList.toggle('active',tab==='intake');
document.getElementById('lc-active').classList.toggle('hidden',tab!=='active');
document.getElementById('lc-intake').classList.toggle('hidden',tab!=='intake');
buildLcCharts(tab);
}}

function buildInventoryCharts(){{
const activeVisible=!document.getElementById('tab-active').classList.contains('hidden');
if(activeVisible){{updateADim1();updateAOrg()}}
else{{updateIDim1();updateIOrg()}}
}}

function buildLcCharts(tab){{
if(!tab){{tab=document.getElementById('lc-pill-active').classList.contains('active')?'active':'intake'}}
if(tab==='active'){{
makeDueBar('lc-a-due',{due_labels_js},{due_data_js},{due_max},{due_colors_js});
closeDueDrilldown();
}}else{{
}}
}}


function toggleApproved(){{
var tile=document.getElementById('approved-toggle');
var dd=document.getElementById('approved-drilldown');
tile.classList.toggle('open');
dd.classList.toggle('open');
}}
var ddCurrentStatus='';
function showDueDrilldown(status){{
var rows=DUE_DRILL[status];
if(!rows)return;
ddCurrentStatus=status;
var search=document.getElementById('dd-search');
search.value='';
renderDueDrillRows(rows,status);
document.getElementById('due-drilldown').classList.add('open');
document.getElementById('due-drilldown').scrollIntoView({{behavior:'smooth',block:'nearest'}});
}}
function renderDueDrillRows(rows,status){{
var tbody=document.getElementById('dd-tbody');
var label=document.getElementById('dd-status-label');
label.textContent=status+' ('+rows.length+')';
tbody.innerHTML=rows.map(function(r){{return '<tr><td>'+r.n+'</td><td>'+r.le+'</td><td>'+r.d+'</td></tr>'}}).join('');
}}
function filterDueDrilldown(){{
var q=document.getElementById('dd-search').value.toLowerCase();
var rows=DUE_DRILL[ddCurrentStatus];
if(!rows)return;
var filtered=rows.filter(function(r){{return r.n.toLowerCase().indexOf(q)!==-1||r.le.toLowerCase().indexOf(q)!==-1||r.d.toLowerCase().indexOf(q)!==-1}});
renderDueDrillRows(filtered,ddCurrentStatus);
}}
function closeDueDrilldown(){{
document.getElementById('due-drilldown').classList.remove('open');
document.getElementById('dd-search').value='';
}}
/* ---- Init ---- */
document.addEventListener('DOMContentLoaded',function(){{
switchMainTab('inventory');
showActiveTab();
}});
</script>
</body>
</html>"""

# Write output
os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'site'), exist_ok=True)
out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'site', 'index.html')
with open(out_path, 'w') as f:
    f.write(HTML)

print(f"Dashboard written to {out_path}")
print(f"KPIs: Active={kpi_active}, Intake={kpi_intake}, Retired={kpi_retired}")
print(f"Approved in last 30 days: {approved_count}")
print(f"Due date statuses: {due_labels} -> {due_data}")
print(f"Review schedule: {sched_labels} -> {sched_data}")
