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

# ---- AMOUNT cleanup ----
# OPD amount: numeric banao, 0 ya NULL/blank ko 1500 maan lo
opd["amount"] = pd.to_numeric(opd["amount"], errors="coerce").fillna(0)
opd.loc[opd["amount"] == 0, "amount"] = 1500
# Plan amount: numeric banao (New Plan revenue ke liye)
plan["amount"] = pd.to_numeric(plan["amount"], errors="coerce").fillna(0)

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

def get_revenue_target(scope_name):
    """target.csv se branch ka Revenue target (na ho to None)."""
    return TARGET_MAP.get((scope_name, "Revenue"), None)

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
GRID={"red":0.3,"green":0.3,"blue":0.3}
G_TXT={"red":0.0,"green":0.5,"blue":0.0}; R_TXT={"red":0.8,"green":0.0,"blue":0.0}
BORDER_STYLE="SOLID_MEDIUM"   # patli="SOLID", moti="SOLID_MEDIUM", sabse moti="SOLID_THICK"

def replace_ws(title, df):
    try: sheet.del_worksheet(sheet.worksheet(title))
    except gspread.exceptions.WorksheetNotFound: pass
    ws=sheet.add_worksheet(title=title, rows=str(len(df)+25), cols=str(len(df.columns)+3))
    set_with_dataframe(ws, df); return ws

def base_format(sid, n_cols, n_rows, title_text):
    req=[]
    req.append({"insertDimension":{"range":{"sheetId":sid,"dimension":"ROWS","startIndex":0,"endIndex":1},"inheritFromBefore":False}})
    req.append({"mergeCells":{"range":{"sheetId":sid,"startRowIndex":0,"endRowIndex":1,"startColumnIndex":0,"endColumnIndex":n_cols},"mergeType":"MERGE_ALL"}})
    req.append({"updateCells":{"rows":[{"values":[{"userEnteredValue":{"stringValue":title_text},"userEnteredFormat":{"backgroundColor":TEAL,"horizontalAlignment":"CENTER","verticalAlignment":"MIDDLE","textFormat":{"bold":True,"fontSize":12,"foregroundColor":WHITE}}}]}],"fields":"userEnteredValue,userEnteredFormat","start":{"sheetId":sid,"rowIndex":0,"columnIndex":0}}})
    req.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":1,"endRowIndex":2,"startColumnIndex":0,"endColumnIndex":n_cols},"cell":{"userEnteredFormat":{"backgroundColor":TEAL,"horizontalAlignment":"CENTER","verticalAlignment":"MIDDLE","textFormat":{"bold":True,"foregroundColor":WHITE},"wrapStrategy":"WRAP"}},"fields":"userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat,wrapStrategy)"}})
    req.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":2,"endRowIndex":2+n_rows,"startColumnIndex":0,"endColumnIndex":1},"cell":{"userEnteredFormat":{"horizontalAlignment":"LEFT","textFormat":{"bold":True}}},"fields":"userEnteredFormat(horizontalAlignment,textFormat)"}})
    req.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":2,"endRowIndex":2+n_rows,"startColumnIndex":1,"endColumnIndex":n_cols},"cell":{"userEnteredFormat":{"horizontalAlignment":"CENTER"}},"fields":"userEnteredFormat.horizontalAlignment"}})
    req.append({"updateBorders":{"range":{"sheetId":sid,"startRowIndex":0,"endRowIndex":2+n_rows,"startColumnIndex":0,"endColumnIndex":n_cols},"top":{"style":BORDER_STYLE,"color":GRID},"bottom":{"style":BORDER_STYLE,"color":GRID},"left":{"style":BORDER_STYLE,"color":GRID},"right":{"style":BORDER_STYLE,"color":GRID},"innerHorizontal":{"style":BORDER_STYLE,"color":GRID},"innerVertical":{"style":BORDER_STYLE,"color":GRID}}})
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

# ====== 8. DURATION HELPERS (branch-wise, count + amount) ======
DURATION_BUCKETS = [(1,"1 Month"),(2,"2 Month"),(3,"3 Month"),
                    (6,"6 Month"),(9,"9 Month"),(12,"1 Year")]

# sirf New Plan rows (duration + amount ke liye)
npd_all = plan[plan["plan_type"] == "New Plan"].copy()
npd_all["total_service_months"] = pd.to_numeric(npd_all["total_service_months"], errors="coerce")

_known = {m for m, _ in DURATION_BUCKETS}
_other = npd_all[~npd_all["total_service_months"].isin(_known)]
if len(_other):
    print(f"⚠️  {len(_other)} New Plans with other durations:",
          sorted(_other["total_service_months"].dropna().unique()))

def duration_amount_table(npd_branch, d1, d2):
    """Ek branch ke New Plans ka duration-wise Count + Amount table (MTD-1 period)."""
    sub = npd_branch[(npd_branch["enrollment_date"] >= d1) &
                     (npd_branch["enrollment_date"] <= d2)]
    rows = []
    tot_count = 0; tot_amount = 0.0
    for months, label in DURATION_BUCKETS:
        seg = sub[sub["total_service_months"] == months]
        c = len(seg); a = float(seg["amount"].sum())
        rows.append([label, c, int(round(a))])
        tot_count += c; tot_amount += a
    df = pd.DataFrame(rows, columns=["New Plan Duration", "Count", "Amount"])
    return df, tot_count, int(round(tot_amount))

# ====== 9. PER-BRANCH TABS (summary + duration + revenue match) ======
branches=sorted(set(opd["hosp_name"])|set(plan["hosp_name"])|set(active["hosp_name"])|set(inactive["hosp_name"]))
branches=[b for b in branches if b and b!="Unknown"]

def write_branch_tab(b):
    """Ek branch ka apna tab: summary + duration(Count+Amount) + Revenue match."""
    # --- branch ke filtered frames ---
    b_opd=opd[opd["hosp_name"]==b]; b_plan=plan[plan["hosp_name"]==b]
    b_active=active[active["hosp_name"]==b]; b_inactive=inactive[inactive["hosp_name"]==b]

    # --- 1. summary df (Overall jaisa) ---
    bdf, bvs, btg = build_df(b_opd,b_plan,b_active,b_inactive,b,Y_COL,M_COL,LM_COL)

    # --- 2. duration (Count + Amount), MTD-1 ---
    b_npd = npd_all[npd_all["hosp_name"]==b]
    dur_df, dur_count, dur_amount = duration_amount_table(b_npd, month_start, yesterday)

    # --- 3. OPD amount (MTD-1), saare OPD, 0->1500 already PREP me ---
    opd_mtd = b_opd[(b_opd["opd_date"]>=month_start)&(b_opd["opd_date"]<=yesterday)]
    opd_amount = int(round(float(opd_mtd["amount"].sum())))

    # --- tab banao + summary likho ---
    ws = replace_ws(b, bdf)
    sid = ws._properties["sheetId"]
    nc = len(bdf.columns); nr = len(bdf)
    req = base_format(sid, nc, nr, f"{b} — Performance (MTD-1 vs Last Month + Target)")
    cols = list(bdf.columns)
    req += color_col(sid, bvs, cols.index("vs Last Month"))
    req += color_col(sid, btg, cols.index("% Achieved"))
    sheet.batch_update({"requests": req})

    # --- duration block + revenue rows (summary ke neeche) ---
    # base_format ne 1 row insert ki thi (title), to data 1 row neeche khisak gaya:
    # sheet layout: row1=title, row2=header, row3..(2+nr)=data  -> last data row = 2+nr
    dur_start = 2 + nr + 2          # summary ke baad 2 row gap (0-indexed)
    rev_target = get_revenue_target(b)

    req2 = []
    # ---- duration heading ----
    dnc = len(dur_df.columns)
    set_with_dataframe(ws, dur_df, row=dur_start + 2, col=1)   # heading+header ke neeche
    req2.append({"mergeCells":{"range":{"sheetId":sid,"startRowIndex":dur_start,"endRowIndex":dur_start+1,"startColumnIndex":0,"endColumnIndex":dnc},"mergeType":"MERGE_ALL"}})
    req2.append({"updateCells":{"rows":[{"values":[{"userEnteredValue":{"stringValue":f"New Plan Duration & Amount — MTD-1 ({mtd_str})"},"userEnteredFormat":{"backgroundColor":TEAL,"horizontalAlignment":"CENTER","verticalAlignment":"MIDDLE","textFormat":{"bold":True,"fontSize":12,"foregroundColor":WHITE}}}]}],"fields":"userEnteredValue,userEnteredFormat","start":{"sheetId":sid,"rowIndex":dur_start,"columnIndex":0}}})
    dhdr = dur_start + 1
    req2.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":dhdr,"endRowIndex":dhdr+1,"startColumnIndex":0,"endColumnIndex":dnc},"cell":{"userEnteredFormat":{"backgroundColor":TEAL,"horizontalAlignment":"CENTER","verticalAlignment":"MIDDLE","textFormat":{"bold":True,"foregroundColor":WHITE},"wrapStrategy":"WRAP"}},"fields":"userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat,wrapStrategy)"}})
    dnr = len(dur_df)
    req2.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":dhdr+1,"endRowIndex":dhdr+1+dnr,"startColumnIndex":0,"endColumnIndex":1},"cell":{"userEnteredFormat":{"horizontalAlignment":"LEFT","textFormat":{"bold":True}}},"fields":"userEnteredFormat(horizontalAlignment,textFormat)"}})
    req2.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":dhdr+1,"endRowIndex":dhdr+1+dnr,"startColumnIndex":1,"endColumnIndex":dnc},"cell":{"userEnteredFormat":{"horizontalAlignment":"CENTER"}},"fields":"userEnteredFormat.horizontalAlignment"}})

    # ---- summary rows (Total New Plan Amount, OPD Amount, Revenue target match) ----
    summary_start = dhdr + 1 + dnr + 1   # duration table ke baad 1 gap row
    if rev_target:
        ach_pct = round(dur_amount / rev_target * 100, 1) if rev_target else 0
        rev_color = G_TXT if dur_amount >= rev_target else R_TXT
        match_txt = f"{ach_pct}%"
    else:
        ach_pct = None; rev_color = None; match_txt = "(no target)"

    extra_rows = [
        ["New Plan — Total Amount", dur_amount, ""],
        ["OPD — Total Amount (0 → 1500)", opd_amount, ""],
        ["Revenue Target (target.csv)", rev_target if rev_target else "—", ""],
        ["Revenue Achieved %", match_txt, ""],
    ]
    set_with_dataframe(ws, pd.DataFrame(extra_rows, columns=["Metric","Value","_"]),
                       row=summary_start + 1, col=1, include_column_header=False)
    # in rows ko bold + left
    req2.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":summary_start,"endRowIndex":summary_start+len(extra_rows),"startColumnIndex":0,"endColumnIndex":1},"cell":{"userEnteredFormat":{"horizontalAlignment":"LEFT","textFormat":{"bold":True}}},"fields":"userEnteredFormat(horizontalAlignment,textFormat)"}})
    req2.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":summary_start,"endRowIndex":summary_start+len(extra_rows),"startColumnIndex":1,"endColumnIndex":2},"cell":{"userEnteredFormat":{"horizontalAlignment":"CENTER","textFormat":{"bold":True}}},"fields":"userEnteredFormat(horizontalAlignment,textFormat)"}})
    # Revenue Achieved % row ko green/red
    if rev_color:
        rev_row = summary_start + 3   # 4th extra row (0-indexed +3)
        req2.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":rev_row,"endRowIndex":rev_row+1,"startColumnIndex":1,"endColumnIndex":2},"cell":{"userEnteredFormat":{"textFormat":{"bold":True,"foregroundColor":rev_color}}},"fields":"userEnteredFormat.textFormat"}})

    # ---- borders for duration block + summary rows ----
    block_end = summary_start + len(extra_rows)
    req2.append({"updateBorders":{"range":{"sheetId":sid,"startRowIndex":dur_start,"endRowIndex":dhdr+1+dnr,"startColumnIndex":0,"endColumnIndex":dnc},"top":{"style":BORDER_STYLE,"color":GRID},"bottom":{"style":BORDER_STYLE,"color":GRID},"left":{"style":BORDER_STYLE,"color":GRID},"right":{"style":BORDER_STYLE,"color":GRID},"innerHorizontal":{"style":BORDER_STYLE,"color":GRID},"innerVertical":{"style":BORDER_STYLE,"color":GRID}}})
    req2.append({"updateBorders":{"range":{"sheetId":sid,"startRowIndex":summary_start,"endRowIndex":block_end,"startColumnIndex":0,"endColumnIndex":2},"top":{"style":BORDER_STYLE,"color":GRID},"bottom":{"style":BORDER_STYLE,"color":GRID},"left":{"style":BORDER_STYLE,"color":GRID},"right":{"style":BORDER_STYLE,"color":GRID},"innerHorizontal":{"style":BORDER_STYLE,"color":GRID},"innerVertical":{"style":BORDER_STYLE,"color":GRID}}})
    req2.append({"autoResizeDimensions":{"dimensions":{"sheetId":sid,"dimension":"COLUMNS","startIndex":0,"endIndex":max(nc,dnc)}}})
    sheet.batch_update({"requests": req2})

    return dict(branch=b, dur_df=dur_df, dur_amount=dur_amount,
                opd_amount=opd_amount, rev_target=rev_target)

branch_results = []
for b in branches:
    res = write_branch_tab(b)
    branch_results.append(res)
    print(f"{b} tab done — New Plan Amt: {res['dur_amount']}, OPD Amt: {res['opd_amount']}")

print(f"ALL BRANCH TABS DONE ({len(branches)} branches)")

# ====== 10. EMAIL — Overall + per-branch summary ======
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")

# === Yahan apni asli email IDs daalo ===
TO  = ["neelesh@emoneeds.com"]
CC  = ["neelesh@emoneeds.com"]
BCC = ["neeleshdwivedirgpv@gmail.com"]


def df_to_html(df, title):
    """Summary table (arrow colors) -> HTML."""
    html = f'<h3 style="font-family:Arial;color:#07333B;margin:14px 0 6px;">{title}</h3>'
    html += '<table style="border-collapse:collapse;font-family:Arial;font-size:13px;">'
    html += '<tr>'
    for col in df.columns:
        html += (f'<th style="background:#028090;color:#ffffff;padding:8px 12px;'
                 f'border:2px solid #555555;text-align:center;">{col}</th>')
    html += '</tr>'
    for _, row in df.iterrows():
        html += '<tr>'
        for col in df.columns:
            val = row[col]; color = "#000000"
            if "vs" in str(col).lower():
                if "⬆️" in str(val): color = "#1a7f37"
                elif "⬇️" in str(val): color = "#c0392b"
            html += (f'<td style="padding:7px 12px;border:2px solid #555555;'
                     f'text-align:center;color:{color};">{val}</td>')
        html += '</tr>'
    html += '</table>'
    return html


def dur_to_html(b_res):
    """Branch ka duration(Count+Amount) + revenue match -> HTML."""
    b = b_res["branch"]; df = b_res["dur_df"]
    html = f'<h3 style="font-family:Arial;color:#07333B;margin:14px 0 6px;">{b} — New Plan Duration &amp; Amount (MTD-1)</h3>'
    html += '<table style="border-collapse:collapse;font-family:Arial;font-size:13px;">'
    html += '<tr>'
    for col in df.columns:
        html += (f'<th style="background:#028090;color:#ffffff;padding:8px 12px;'
                 f'border:2px solid #555555;text-align:center;">{col}</th>')
    html += '</tr>'
    for _, row in df.iterrows():
        html += '<tr>'
        for i, col in enumerate(df.columns):
            align = "left" if i == 0 else "center"
            html += (f'<td style="padding:7px 12px;border:2px solid #555555;'
                     f'text-align:{align};">{row[col]}</td>')
        html += '</tr>'
    html += '</table>'
    # revenue match line
    rt = b_res["rev_target"]; da = b_res["dur_amount"]; oa = b_res["opd_amount"]
    if rt:
        pct = round(da/rt*100,1) if rt else 0
        col = "#1a7f37" if da>=rt else "#c0392b"
        rev_line = (f'New Plan Amount: <b>{da:,}</b> &nbsp;|&nbsp; '
                    f'Revenue Target: <b>{rt:,}</b> &nbsp;|&nbsp; '
                    f'Achieved: <b style="color:{col};">{pct}%</b>')
    else:
        rev_line = f'New Plan Amount: <b>{da:,}</b> &nbsp;|&nbsp; Revenue Target: <i>not set</i>'
    html += (f'<p style="font-family:Arial;font-size:12px;margin:6px 0 0;">{rev_line}'
             f'<br>OPD Amount (0→1500): <b>{oa:,}</b></p>')
    return html


_y_str  = yesterday.strftime("%d %b %Y")
_m_str  = month_start.strftime("%d %b")
_lm_str = f"{lm_start.strftime('%d %b')} – {lm_end.strftime('%d %b %Y')}"

legend_html = f'''
<table style="border-collapse:collapse;font-family:Arial;font-size:12px;
              margin:6px 0 16px;background:#f4fbfa;border:1px solid #cfe8e6;">
  <tr><td style="padding:6px 12px;border-bottom:1px solid #e0eeed;"><b style="color:#07333B;">Yesterday</b></td>
      <td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">Single-day performance for {yesterday.strftime('%d %b %Y')}.</td></tr>
  <tr><td style="padding:6px 12px;border-bottom:1px solid #e0eeed;"><b style="color:#07333B;">MTD-1</b></td>
      <td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">Month-to-Date — cumulative figures from the 1st up to yesterday ({_m_str} – {yesterday.strftime('%d %b')}).</td></tr>
  <tr><td style="padding:6px 12px;border-bottom:1px solid #e0eeed;"><b style="color:#07333B;">Last Month</b></td>
      <td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">Same period last month for a like-for-like comparison ({_lm_str}).</td></tr>
  <tr><td style="padding:6px 12px;border-bottom:1px solid #e0eeed;"><b style="color:#07333B;">vs Last Month</b></td>
      <td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">Change in MTD-1 vs same period last month. ⬆️ green = improvement, ⬇️ red = decline.</td></tr>
  <tr><td style="padding:6px 12px;border-bottom:1px solid #e0eeed;"><b style="color:#07333B;">Target / % Achieved / Pending&nbsp;%</b></td>
      <td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">Monthly goal, % achieved as of MTD-1, % remaining.</td></tr>
  <tr><td style="padding:6px 12px;border-bottom:1px solid #e0eeed;"><b style="color:#07333B;">Amount</b></td>
      <td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">New Plan revenue per duration. Revenue Target (target.csv) se compare hota hai.</td></tr>
  <tr><td style="padding:6px 12px;"><b style="color:#07333B;">OPD Amount</b></td>
      <td style="padding:6px 12px;">Total OPD revenue; jahan amount 0 tha use 1500 maana gaya.</td></tr>
</table>
'''

branch_html = ""
for res in branch_results:
    # har branch ka summary dobara banao email ke liye
    b=res["branch"]
    bdf,_,_ = build_df(opd[opd["hosp_name"]==b],plan[plan["hosp_name"]==b],
                       active[active["hosp_name"]==b],inactive[inactive["hosp_name"]==b],
                       b,Y_COL,M_COL,LM_COL)
    branch_html += df_to_html(bdf, f"{b} — Summary ({_m_str} – {yesterday.strftime('%d %b %Y')})")
    branch_html += "<br>"
    branch_html += dur_to_html(res)
    branch_html += "<br><br>"

html_body = f'''
<html><body style="font-family:Arial;color:#222;">
<p>Dear Tanmay,</p>
<p>Please find the <b>Overall Performance Report</b> for
<b>{yesterday.strftime('%d %b %Y')}</b> below, with the consolidated summary,
each branch separately, New Plan duration &amp; amount, and revenue progress
against target.</p>

<p style="margin-bottom:4px;"><b style="color:#07333B;">How to read this report:</b></p>
{legend_html}

{df_to_html(overall_df, f"Overall Summary ({_m_str} – {yesterday.strftime('%d %b %Y')})")}
<br><br>
{branch_html}

<p style="margin-top:8px;">Favourable movements appear in
<span style="color:#1a7f37;"><b>green</b></span>, unfavourable in
<span style="color:#c0392b;"><b>red</b></span>.</p>

<p>This report is generated automatically and refreshes every day.</p>

<p>Best regards,<br><b>Neelesh</b><br>Data Analyst, Emoneeds</p>

<p style="font-family:Arial;font-size:11px;color:#999;border-top:1px solid #eee;
          padding-top:8px;margin-top:14px;">
This is an automated report. Figures are based on data up to {yesterday.strftime('%d %b %Y')}.</p>
</body></html>
'''

msg = MIMEMultipart("alternative")
msg["Subject"] = f"Overall Performance Report — {_y_str}"
msg["From"] = GMAIL_USER
msg["To"]  = ", ".join(TO)
msg["Cc"]  = ", ".join(CC)
msg.attach(MIMEText(html_body, "html"))

all_recipients = TO + CC + BCC
with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    server.sendmail(GMAIL_USER, all_recipients, msg.as_string())

print(f"Email bheji gayi — To: {len(TO)}, CC: {len(CC)}, BCC: {len(BCC)}")