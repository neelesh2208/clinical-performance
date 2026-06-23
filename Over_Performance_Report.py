from dotenv import load_dotenv
load_dotenv()
import os, json, calendar
import pandas as pd
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus
from datetime import date, timedelta

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread_dataframe import set_with_dataframe

from config import DB_CONFIG
from queries import ACTIVE_QUERY, INACTIVE_QUERY, OPD_QUERY, PLAN_TYPE_QUERY

# ====== 1. DATABASE ======
engine = create_engine(
    f"postgresql+psycopg2://{DB_CONFIG['user']}:{quote_plus(DB_CONFIG['password'])}"
    f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
)
print("Database Connected")
QUERIES = {"active": ACTIVE_QUERY, "inactive": INACTIVE_QUERY,
           "opd": OPD_QUERY, "plan": PLAN_TYPE_QUERY}
data = {}
for name, q in QUERIES.items():
    with engine.connect() as conn:
        data[name] = pd.read_sql(text(q), conn)
    print(f"  {name}: {len(data[name])} rows")
engine.dispose()
print("Queries done")

# ====== 2. SHEET AUTH ======
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
raw = os.environ.get("GCP_CREDENTIALS")
if raw:
    creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(raw), scope)
else:
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
sheet = client.open_by_key("1MEXTTUZCkN0OH6aXa36FSqtDmL73LWvf3Aj5Cx-y450")

# ====== 3. DATES ======
yesterday = date.today() - timedelta(days=1)
month_start = yesterday.replace(day=1)
if month_start.month == 1:
    lm_year, lm_month = month_start.year - 1, 12
else:
    lm_year, lm_month = month_start.year, month_start.month - 1
lm_start = date(lm_year, lm_month, 1)
lm_end_day = min(yesterday.day, calendar.monthrange(lm_year, lm_month)[1])
lm_end = date(lm_year, lm_month, lm_end_day)
lm_active_ref = pd.Timestamp(lm_start)

# ====== 4. PREP ======
opd = data["opd"].copy(); plan = data["plan"].copy()
active = data["active"].copy(); inactive = data["inactive"].copy()
opd["opd_date"] = pd.to_datetime(opd["opd_date"], errors="coerce").dt.date
plan["enrollment_date"] = pd.to_datetime(plan["enrollment_date"], errors="coerce").dt.date
inactive["inactive_date"] = pd.to_datetime(inactive["inactive_date"], errors="coerce").dt.date
active["active_month_dt"] = pd.to_datetime(active["active_month"], format="%b-%y", errors="coerce")
BRANCH_RENAME = {"Emoneeds": "Gurgaon", "Emoneeds GK": "GK"}
for df in (opd, plan, active, inactive):
    df["hosp_name"] = df["hosp_name"].fillna("Unknown").astype(str).str.strip()
    df["hosp_name"] = df["hosp_name"].replace(BRANCH_RENAME)

CATEGORIES = ["New OPDs","F/U OPDs","New Plan","Renewals","Revivals",
              "Inactive","Active","Assessments","Suggest RPP","NO2P %"]
REVERSE = {"Inactive"}
PCT_ROWS = {"NO2P %"}

# ====== 4b. TARGET CSV ======
TARGET_MAP = {}
try:
    tdf = pd.read_csv("target.csv", dtype=str, keep_default_na=False)
    tdf["branch"] = tdf["branch"].str.strip()
    tdf["category"] = tdf["category"].str.strip()
    tdf["target"] = pd.to_numeric(tdf["target"], errors="coerce").fillna(0).astype(int)
    TARGET_MAP = {(r["branch"], r["category"]): r["target"] for _, r in tdf.iterrows()}
    print(f"Targets loaded: {len(TARGET_MAP)}")
except FileNotFoundError:
    print("target.csv not found — sirf Renewal auto target chalega")

def get_target(scope_name, category, last_month_active):
    if category == "Renewals":
        return round(last_month_active * 0.75)
    return TARGET_MAP.get((scope_name, category), None)

# ====== 5. METRICS ======
def count_range(opd, plan, active, inactive, d1, d2, active_ref):
    def oc(df, s): return len(df[(df["opd_date"]>=d1)&(df["opd_date"]<=d2)&(df["opd_status"]==s)])
    def rc(df): return len(df[(df["opd_date"]>=d1)&(df["opd_date"]<=d2)&(df["is_suggest_rpp"]=="Yes")])
    def pc(df,p): return len(df[(df["enrollment_date"]>=d1)&(df["enrollment_date"]<=d2)&(df["plan_type"]==p)])
    def pct(a,b): return round(a/b*100,1) if b else 0.0
    r = {}
    r["New OPDs"]=oc(opd,"NEW OPD"); r["F/U OPDs"]=oc(opd,"OLD OPD")
    r["New Plan"]=pc(plan,"New Plan"); r["Renewals"]=pc(plan,"Renewal"); r["Revivals"]=pc(plan,"Revival")
    r["Inactive"]=len(inactive[(inactive["inactive_date"]>=d1)&(inactive["inactive_date"]<=d2)])
    r["Active"]=len(active[active["active_month_dt"]==active_ref])
    r["Assessments"]=0; r["Suggest RPP"]=rc(opd)
    r["NO2P %"]=pct(r["New Plan"], r["New OPDs"])
    return r

def last_month_active_count(active):
    return len(active[active["active_month_dt"]==lm_active_ref])

def arrow_pct(curr, prev, reverse=False):
    if prev==0 and curr==0: return "—","none"
    if prev==0: return "⬆️ new", ("red" if reverse else "green")
    change=(curr-prev)/prev*100; up=curr>prev
    if abs(change)<0.05: return "0%","none"
    arrow="⬆️" if up else "⬇️"
    color=("red" if up else "green") if reverse else ("green" if up else "red")
    return f"{arrow} {abs(round(change,1))}%", color

def build_df(opd, plan, active, inactive, scope_name, y_col, m_col, lm_col):
    yd  = count_range(opd,plan,active,inactive, yesterday,yesterday, pd.Timestamp(month_start))
    mtd = count_range(opd,plan,active,inactive, month_start,yesterday, pd.Timestamp(month_start))
    lm  = count_range(opd,plan,active,inactive, lm_start,lm_end, lm_active_ref)
    lm_active = last_month_active_count(active)

    rows=[]; vs_colors=[]; tgt_colors=[]
    for cat in CATEGORIES:
        rev = cat in REVERSE
        if cat in PCT_ROWS:
            if lm[cat] or mtd[cat]:
                up=mtd[cat]>lm[cat]; diff=round(mtd[cat]-lm[cat],1)
                if abs(diff)<0.05: vs_txt,vs_c="→ 0","none"
                else: vs_txt=f"{'⬆️' if up else '⬇️'} {abs(diff)}pp"; vs_c="green" if up else "red"
            else: vs_txt,vs_c="—","none"
        else:
            vs_txt,vs_c=arrow_pct(mtd[cat], lm[cat], reverse=rev)
        tgt=get_target(scope_name, cat, lm_active)
        if tgt is None:
            t_t,t_p,t_pend,t_c="","","","none"
        else:
            ach=mtd[cat]
            p=round(ach/tgt*100,1) if tgt else 0
            pending_pct=round(max(0,100-p),1)
            t_t,t_p,t_pend=tgt,f"{p}%",f"{pending_pct}%"
            t_c="green" if ach>=tgt else "red"
        if cat in PCT_ROWS:
            yv,mv,lv=f"{yd[cat]}%",f"{mtd[cat]}%",f"{lm[cat]}%"
        else:
            yv,mv,lv=yd[cat],mtd[cat],lm[cat]
        rows.append([cat,yv,mv,lv,vs_txt,t_t,t_p,t_pend])
        vs_colors.append(vs_c); tgt_colors.append(t_c)
    cols=["Category",y_col,m_col,lm_col,"vs Last Month","Target","% Achieved","Pending %"]
    return pd.DataFrame(rows,columns=cols), vs_colors, tgt_colors

y_str=f"{yesterday.day}-{yesterday.strftime('%b')}"
mtd_str=f"{month_start.day}-{yesterday.day} {yesterday.strftime('%b')}"
lm_str=f"{lm_start.day}-{lm_end.day} {lm_start.strftime('%b')}"
Y_COL=f"Yesterday ({y_str})"; M_COL=f"MTD-1 ({mtd_str})"; LM_COL=f"Last Month ({lm_str})"

overall_df, ov_vs, ov_tgt = build_df(opd,plan,active,inactive,"Overall",Y_COL,M_COL,LM_COL)
print("Overall ready")

# ====== 6. FORMAT ======
TEAL={"red":0.18,"green":0.55,"blue":0.56}; WHITE={"red":1,"green":1,"blue":1}
GRID={"red":0.55,"green":0.55,"blue":0.55}
G_TXT={"red":0.0,"green":0.5,"blue":0.0}; R_TXT={"red":0.8,"green":0.0,"blue":0.0}

def replace_ws(title, df):
    try: sheet.del_worksheet(sheet.worksheet(title))
    except gspread.exceptions.WorksheetNotFound: pass
    ws=sheet.add_worksheet(title=title, rows=str(len(df)+12), cols=str(len(df.columns)+3))
    set_with_dataframe(ws, df); return ws

def base_format(sid, n_cols, n_rows, title_text):
    req=[]
    req.append({"insertDimension":{"range":{"sheetId":sid,"dimension":"ROWS","startIndex":0,"endIndex":1},"inheritFromBefore":False}})
    req.append({"mergeCells":{"range":{"sheetId":sid,"startRowIndex":0,"endRowIndex":1,"startColumnIndex":0,"endColumnIndex":n_cols},"mergeType":"MERGE_ALL"}})
    req.append({"updateCells":{"rows":[{"values":[{"userEnteredValue":{"stringValue":title_text},"userEnteredFormat":{"backgroundColor":TEAL,"horizontalAlignment":"CENTER","verticalAlignment":"MIDDLE","textFormat":{"bold":True,"fontSize":12,"foregroundColor":WHITE}}}]}],"fields":"userEnteredValue,userEnteredFormat","start":{"sheetId":sid,"rowIndex":0,"columnIndex":0}}})
    req.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":1,"endRowIndex":2,"startColumnIndex":0,"endColumnIndex":n_cols},"cell":{"userEnteredFormat":{"backgroundColor":TEAL,"horizontalAlignment":"CENTER","verticalAlignment":"MIDDLE","textFormat":{"bold":True,"foregroundColor":WHITE},"wrapStrategy":"WRAP"}},"fields":"userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat,wrapStrategy)"}})
    req.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":2,"endRowIndex":2+n_rows,"startColumnIndex":0,"endColumnIndex":1},"cell":{"userEnteredFormat":{"horizontalAlignment":"LEFT","textFormat":{"bold":True}}},"fields":"userEnteredFormat(horizontalAlignment,textFormat)"}})
    req.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":2,"endRowIndex":2+n_rows,"startColumnIndex":1,"endColumnIndex":n_cols},"cell":{"userEnteredFormat":{"horizontalAlignment":"CENTER"}},"fields":"userEnteredFormat.horizontalAlignment"}})
    req.append({"updateBorders":{"range":{"sheetId":sid,"startRowIndex":0,"endRowIndex":2+n_rows,"startColumnIndex":0,"endColumnIndex":n_cols},"top":{"style":"SOLID","color":GRID},"bottom":{"style":"SOLID","color":GRID},"left":{"style":"SOLID","color":GRID},"right":{"style":"SOLID","color":GRID},"innerHorizontal":{"style":"SOLID","color":GRID},"innerVertical":{"style":"SOLID","color":GRID}}})
    req.append({"autoResizeDimensions":{"dimensions":{"sheetId":sid,"dimension":"COLUMNS","startIndex":0,"endIndex":n_cols}}})
    return req

def color_col(sid, colors, col_idx, data_start=2):
    req=[]
    for i,c in enumerate(colors):
        if c=="green": fg=G_TXT
        elif c=="red": fg=R_TXT
        else: continue
        req.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":data_start+i,"endRowIndex":data_start+i+1,"startColumnIndex":col_idx,"endColumnIndex":col_idx+1},"cell":{"userEnteredFormat":{"textFormat":{"bold":True,"foregroundColor":fg}}},"fields":"userEnteredFormat.textFormat"}})
    return req

# ====== 7. OVERALL ======
ws1=replace_ws("Overall_Summary", overall_df); sid1=ws1._properties["sheetId"]
nc1=len(overall_df.columns); nr1=len(overall_df)
req=base_format(sid1,nc1,nr1,"Overall Performance — MTD-1 vs Last Month + Target")
cols1=list(overall_df.columns)
req+=color_col(sid1, ov_vs, cols1.index("vs Last Month"))
req+=color_col(sid1, ov_tgt, cols1.index("% Achieved"))
sheet.batch_update({"requests":req})
print("Overall_Summary done")

# ====== 8. BRANCH ======
branches=sorted(set(opd["hosp_name"])|set(plan["hosp_name"])|set(active["hosp_name"])|set(inactive["hosp_name"]))
branches=[b for b in branches if b and b!="Unknown"]
branch_cols=["Category"]; branch_rows={c:[c] for c in CATEGORIES}
vs_color_map={}; tgt_color_map={}
for b in branches:
    bdf,bvs,btg=build_df(opd[opd["hosp_name"]==b],plan[plan["hosp_name"]==b],
                         active[active["hosp_name"]==b],inactive[inactive["hosp_name"]==b],
                         b,"Y","M","LM")
    base=len(branch_cols)
    branch_cols.extend([f"{b} ({y_str})",f"{b} ({mtd_str})",f"{b} (LM {lm_str})",
                        f"{b} vs LM",f"{b} Target",f"{b} %",f"{b} Pending%"])
    vs_color_map[base+3]=bvs
    tgt_color_map[base+5]=btg
    for ri,cat in enumerate(CATEGORIES):
        branch_rows[cat].extend(bdf.iloc[ri].tolist()[1:])
branch_df=pd.DataFrame([branch_rows[c] for c in CATEGORIES], columns=branch_cols)

ws2=replace_ws("Branch_Summary", branch_df); sid2=ws2._properties["sheetId"]
nc2=len(branch_df.columns); nr2=len(branch_df)
req=base_format(sid2,nc2,nr2,"Branch-wise Performance — MTD-1 vs Last Month + Target")
for idx,cols in vs_color_map.items(): req+=color_col(sid2,cols,idx)
for idx,cols in tgt_color_map.items(): req+=color_col(sid2,cols,idx)
sheet.batch_update({"requests":req})
print(f"Branch_Summary done ({len(branches)} branches)")
print("ALL DONE")

# ============================================================
# 9. NEW PLAN DURATION (branch-wise: Gurgaon, GK, Total)
# ============================================================

# duration buckets: number -> label (image jaisa order)
DURATION_BUCKETS = [(1,"1 Month"),(2,"2 Month"),(3,"3 Month"),
                    (6,"6 Month"),(9,"9 Month"),(12,"1 Year")]

# sirf New Plan wale rows, with valid duration
npd = plan[plan["plan_type"] == "New Plan"].copy()
npd["total_service_months"] = pd.to_numeric(
    npd["total_service_months"], errors="coerce")

# safety check: agar koi duration in 6 buckets ke bahar ho to warn
_known = {m for m, _ in DURATION_BUCKETS}
_other = npd[~npd["total_service_months"].isin(_known)]
if len(_other):
    print(f"⚠️  {len(_other)} New Plans with other durations:",
          sorted(_other["total_service_months"].dropna().unique()))

dur_branches = ["Gurgaon", "GK"]   # fixed order

def duration_table(d1, d2):
    """Ek period ke liye duration x branch count table banao."""
    sub = npd[(npd["enrollment_date"] >= d1) & (npd["enrollment_date"] <= d2)]
    rows = []
    for months, label in DURATION_BUCKETS:
        row = [label]
        total = 0
        for b in dur_branches:
            c = len(sub[(sub["hosp_name"] == b) &
                        (sub["total_service_months"] == months)])
            row.append(c); total += c
        row.append(total)
        rows.append(row)
    cols = ["New Plan Duration Time"] + dur_branches + ["Total"]
    return pd.DataFrame(rows, columns=cols)

# Yesterday aur MTD-1 dono
npd_yday = duration_table(yesterday, yesterday)
npd_mtd  = duration_table(month_start, yesterday)
print("New Plan Duration tables ready")


# ---- New_Plan_Duration tab (Yesterday + MTD-1, dono) ----
def write_duration_block(ws, df, start_row, title_text):
    """Ek duration table ko diye gaye row se likho (heading + table)."""
    sid = ws._properties["sheetId"]
    nc = len(df.columns)
    # data daalo (heading ke neeche)
    set_with_dataframe(ws, df, row=start_row + 2, col=1)
    req = []
    # heading merge + teal
    req.append({"mergeCells":{"range":{"sheetId":sid,"startRowIndex":start_row,"endRowIndex":start_row+1,"startColumnIndex":0,"endColumnIndex":nc},"mergeType":"MERGE_ALL"}})
    req.append({"updateCells":{"rows":[{"values":[{"userEnteredValue":{"stringValue":title_text},"userEnteredFormat":{"backgroundColor":TEAL,"horizontalAlignment":"CENTER","verticalAlignment":"MIDDLE","textFormat":{"bold":True,"fontSize":12,"foregroundColor":WHITE}}}]}],"fields":"userEnteredValue,userEnteredFormat","start":{"sheetId":sid,"rowIndex":start_row,"columnIndex":0}}})
    # column header row teal
    hdr = start_row + 1
    req.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":hdr,"endRowIndex":hdr+1,"startColumnIndex":0,"endColumnIndex":nc},"cell":{"userEnteredFormat":{"backgroundColor":TEAL,"horizontalAlignment":"CENTER","verticalAlignment":"MIDDLE","textFormat":{"bold":True,"foregroundColor":WHITE},"wrapStrategy":"WRAP"}},"fields":"userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat,wrapStrategy)"}})
    # first col bold-left, data center
    nr = len(df)
    req.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":hdr+1,"endRowIndex":hdr+1+nr,"startColumnIndex":0,"endColumnIndex":1},"cell":{"userEnteredFormat":{"horizontalAlignment":"LEFT","textFormat":{"bold":True}}},"fields":"userEnteredFormat(horizontalAlignment,textFormat)"}})
    req.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":hdr+1,"endRowIndex":hdr+1+nr,"startColumnIndex":1,"endColumnIndex":nc},"cell":{"userEnteredFormat":{"horizontalAlignment":"CENTER"}},"fields":"userEnteredFormat.horizontalAlignment"}})
    # Total column bold
    req.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":hdr,"endRowIndex":hdr+1+nr,"startColumnIndex":nc-1,"endColumnIndex":nc},"cell":{"userEnteredFormat":{"textFormat":{"bold":True}}},"fields":"userEnteredFormat.textFormat"}})
    # borders
    req.append({"updateBorders":{"range":{"sheetId":sid,"startRowIndex":start_row,"endRowIndex":hdr+1+nr,"startColumnIndex":0,"endColumnIndex":nc},"top":{"style":"SOLID","color":GRID},"bottom":{"style":"SOLID","color":GRID},"left":{"style":"SOLID","color":GRID},"right":{"style":"SOLID","color":GRID},"innerHorizontal":{"style":"SOLID","color":GRID},"innerVertical":{"style":"SOLID","color":GRID}}})
    return req

# tab banao (purana delete karke fresh)
try: sheet.del_worksheet(sheet.worksheet("New_Plan_Duration"))
except gspread.exceptions.WorksheetNotFound: pass
ws3 = sheet.add_worksheet(title="New_Plan_Duration", rows="40", cols="8")

req = []
req += write_duration_block(ws3, npd_yday, 0,
        f"New Plan Duration — Yesterday ({y_str})")
# doosra table thoda neeche (pehle table = heading + header + 6 rows + gap)
req += write_duration_block(ws3, npd_mtd, 11,
        f"New Plan Duration — MTD-1 ({mtd_str})")
req.append({"autoResizeDimensions":{"dimensions":{"sheetId":ws3._properties["sheetId"],"dimension":"COLUMNS","startIndex":0,"endIndex":4}}})
sheet.batch_update({"requests": req})
print("New_Plan_Duration tab done")


# ============================================================
# 10. EMAIL — Overall + Branch report (HTML body)
# ============================================================

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")

# abhi test ke liye sirf khud ko; baad me team add karo
RECIPIENTS = [GMAIL_USER]


# ---- HTML TABLE HELPER (arrow colors ke saath) ----
def df_to_html(df, title):
    """DataFrame ko styled HTML email table banao (teal header + arrow colors)."""
    html = f'<h3 style="font-family:Arial;color:#07333B;margin:14px 0 6px;">{title}</h3>'
    html += ('<table style="border-collapse:collapse;font-family:Arial;'
             'font-size:13px;">')
    # header row
    html += '<tr>'
    for col in df.columns:
        html += (f'<th style="background:#028090;color:#ffffff;'
                 f'padding:8px 12px;border:1px solid #dddddd;'
                 f'text-align:center;">{col}</th>')
    html += '</tr>'
    # data rows
    for _, row in df.iterrows():
        html += '<tr>'
        for col in df.columns:
            val = row[col]
            color = "#000000"
            if "vs" in str(col).lower():
                if "⬆️" in str(val):
                    color = "#1a7f37"
                elif "⬇️" in str(val):
                    color = "#c0392b"
            html += (f'<td style="padding:7px 12px;border:1px solid #dddddd;'
                     f'text-align:center;color:{color};">{val}</td>')
        html += '</tr>'
    html += '</table>'
    return html


# ---- PLAIN HTML TABLE (duration table ke liye, no color logic) ----
def df_to_html_plain(df, title):
    """Simple count table — teal header, bold Total column."""
    html = f'<h3 style="font-family:Arial;color:#07333B;margin:14px 0 6px;">{title}</h3>'
    html += ('<table style="border-collapse:collapse;font-family:Arial;'
             'font-size:13px;">')
    html += '<tr>'
    for col in df.columns:
        html += (f'<th style="background:#028090;color:#ffffff;'
                 f'padding:8px 12px;border:1px solid #dddddd;'
                 f'text-align:center;">{col}</th>')
    html += '</tr>'
    last_col = df.columns[-1]
    for _, row in df.iterrows():
        html += '<tr>'
        for col in df.columns:
            bold = "font-weight:bold;" if col == last_col else ""
            align = "left" if col == df.columns[0] else "center"
            html += (f'<td style="padding:7px 12px;border:1px solid #dddddd;'
                     f'text-align:{align};{bold}">{row[col]}</td>')
        html += '</tr>'
    html += '</table>'
    return html


# ---- EMAIL BODY ----
_y_str  = yesterday.strftime("%d %b %Y")
_m_str  = month_start.strftime("%d %b")
_lm_str = f"{lm_start.strftime('%d %b')} – {lm_end.strftime('%d %b %Y')}"

# Header glossary (professional English)
legend_html = f'''
<table style="border-collapse:collapse;font-family:Arial;font-size:12px;
              margin:6px 0 16px;background:#f4fbfa;border:1px solid #cfe8e6;">
  <tr><td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">
      <b style="color:#07333B;">Yesterday</b></td>
      <td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">
      Single-day performance for {yesterday.strftime('%d %b %Y')}.</td></tr>
  <tr><td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">
      <b style="color:#07333B;">MTD-1</b></td>
      <td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">
      Month-to-Date — cumulative figures from the 1st of the month up to
      yesterday ({_m_str} – {yesterday.strftime('%d %b')}).</td></tr>
  <tr><td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">
      <b style="color:#07333B;">Last Month</b></td>
      <td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">
      The same period in the previous month, shown for a fair like-for-like
      comparison ({_lm_str}).</td></tr>
  <tr><td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">
      <b style="color:#07333B;">vs Last Month</b></td>
      <td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">
      Change in MTD-1 against the same period last month.
      ⬆️ green = improvement, ⬇️ red = decline.</td></tr>
  <tr><td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">
      <b style="color:#07333B;">Target / % Achieved / Pending&nbsp;%</b></td>
      <td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">
      The monthly goal, the percentage achieved as of MTD-1, and the
      percentage still remaining.</td></tr>
  <tr><td style="padding:6px 12px;"><b style="color:#07333B;">NO2P&nbsp;%</b></td>
      <td style="padding:6px 12px;">New-OPD to New-Plan conversion rate — the
      share of new OPD patients who enrolled in a plan
      (New&nbsp;Plan ÷ New&nbsp;OPD × 100).</td></tr>
</table>
'''

html_body = f'''
<html><body style="font-family:Arial;color:#222;">

<p>Dear Tanmay,</p>

<p>Please find the <b>Overall Performance Report</b> for
<b>{yesterday.strftime('%d %b %Y')}</b> below, covering both the single-day
figures and the month-to-date (MTD) cumulative summary, with a branch-wise
breakdown and target progress.</p>

<p style="margin-bottom:4px;"><b style="color:#07333B;">How to read this report:</b></p>
{legend_html}

{df_to_html(overall_df, f"Overall Summary ({_m_str} – {yesterday.strftime('%d %b %Y')})")}

<br>

{df_to_html(branch_df, f"Branch-wise Summary ({_m_str} – {yesterday.strftime('%d %b %Y')})")}

<br>

{df_to_html_plain(npd_mtd, f"New Plan Duration — MTD-1 ({_m_str} – {yesterday.strftime('%d %b')})")}

<p style="margin-top:18px;">For quick reference, favourable movements appear in
<span style="color:#1a7f37;"><b>green</b></span> and unfavourable ones in
<span style="color:#c0392b;"><b>red</b></span>. Targets achieved are shown in green,
shortfalls in red.</p>

<p>This report is generated automatically and refreshes every day. Please reach out
if you would like any additional metric or a different breakdown.</p>

<p>Best regards,<br>
<b>Neelesh</b><br>
Data Analyst, Emoneeds</p>

<p style="font-family:Arial;font-size:11px;color:#999;border-top:1px solid #eee;
          padding-top:8px;margin-top:14px;">
This is an automated report. Figures are based on data available up to
{yesterday.strftime('%d %b %Y')}.</p>

</body></html>
'''

# ---- EMAIL BHEJO ----
msg = MIMEMultipart("alternative")
msg["Subject"] = f"Overall Performance Report — {_y_str}"
msg["From"] = GMAIL_USER
msg["To"] = ", ".join(RECIPIENTS)
msg.attach(MIMEText(html_body, "html"))

with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    server.sendmail(GMAIL_USER, RECIPIENTS, msg.as_string())

print(f"Email bheji gayi: {', '.join(RECIPIENTS)}")