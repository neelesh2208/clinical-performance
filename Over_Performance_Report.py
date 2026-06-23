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

# ====== 2b. ASSESSMENTS SHEET (external, manual data) ======
# NOTE: yeh sheet service account email ke saath SHARE honi chahiye (Viewer/Editor)
ASSESS_SHEET_KEY = "1pdSu8sbIpTpj8Fbc3UDqyWug34vaLl8V7qo9zdT7PZ0"
assess_df = pd.DataFrame(columns=["date","center","assess_no","cost"])
try:
    a_sheet = client.open_by_key(ASSESS_SHEET_KEY)
    a_ws = a_sheet.sheet1   # pehli tab (gid=0)
    a_records = a_ws.get_all_records()   # header row ko keys maan ke dicts
    a_raw = pd.DataFrame(a_records)
    # column naam normalize (case/space safe)
    a_raw.columns = [str(c).strip().lower() for c in a_raw.columns]

    # column dhoondhne ka safe helper: pehla match jo maujood ho, warna empty Series
    def pick_col(*candidates):
        for c in candidates:
            if c in a_raw.columns:
                return a_raw[c]
        return pd.Series([None] * len(a_raw))   # missing -> empty column (scalar nahi)

    # sheet me spelling "assesment" (single s) hai — dono variants handle
    s_date   = pick_col("date")
    s_center = pick_col("center")
    s_no     = pick_col("assesment's no.", "assessment's no.", "assesments no.", "assessments no.")
    s_cost   = pick_col("cost")

    assess_df = pd.DataFrame({
        "date":      pd.to_datetime(s_date, errors="coerce").dt.date,
        "center":    s_center.astype(str).str.strip(),
        "assess_no": pd.to_numeric(s_no, errors="coerce").fillna(0),
        "cost":      pd.to_numeric(s_cost, errors="coerce").fillna(0),
    })
    # branch naam normalize (sheet me Gurgaon/GK jaisa hi hai, bas safe rename)
    A_BRANCH_RENAME = {"emoneeds":"Gurgaon","emoneeds gk":"GK","gurgaon":"Gurgaon","gk":"GK"}
    assess_df["center"] = assess_df["center"].str.lower().map(A_BRANCH_RENAME).fillna(assess_df["center"])
    assess_df = assess_df.dropna(subset=["date"])
    print(f"Assessments sheet: {len(assess_df)} rows loaded "
          f"(total no.={int(assess_df['assess_no'].sum())}, total cost={int(assess_df['cost'].sum())})")
except Exception as e:
    print(f"⚠️  Assessments sheet load nahi hui ({e}) — Assessments 0 rahega")

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
opd["amount"] = pd.to_numeric(opd["amount"], errors="coerce").fillna(0)
opd.loc[opd["amount"] == 0, "amount"] = 1500
plan["amount"] = pd.to_numeric(plan["amount"], errors="coerce").fillna(0)

BRANCH_RENAME = {"Emoneeds": "Gurgaon", "Emoneeds GK": "GK"}
for df in (opd, plan, active, inactive):
    df["hosp_name"] = df["hosp_name"].fillna("Unknown").astype(str).str.strip()
    df["hosp_name"] = df["hosp_name"].replace(BRANCH_RENAME)

CATEGORIES = ["New OPDs","F/U OPDs","New Plan","Renewals","Revivals",
              "Inactive","Active","Assessments","Suggest RPP","NO2P %"]
REVERSE = {"Inactive"}
PCT_ROWS = {"NO2P %"}
AMOUNT_CATS = {"New OPDs","F/U OPDs","New Plan","Renewals","Revivals","Assessments"}

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
    return TARGET_MAP.get((scope_name, "Revenue"), None)

# ====== 5. METRICS ======
def count_range(opd, plan, active, inactive, assess, d1, d2, active_ref):
    def oc(df, s): return len(df[(df["opd_date"]>=d1)&(df["opd_date"]<=d2)&(df["opd_status"]==s)])
    def rc(df): return len(df[(df["opd_date"]>=d1)&(df["opd_date"]<=d2)&(df["is_suggest_rpp"]=="Yes")])
    def pc(df,p): return len(df[(df["enrollment_date"]>=d1)&(df["enrollment_date"]<=d2)&(df["plan_type"]==p)])
    def pct(a,b): return round(a/b*100,1) if b else 0.0
    def oa(df, s):
        sub=df[(df["opd_date"]>=d1)&(df["opd_date"]<=d2)&(df["opd_status"]==s)]
        return float(sub["amount"].sum())
    def pa(df, p):
        sub=df[(df["enrollment_date"]>=d1)&(df["enrollment_date"]<=d2)&(df["plan_type"]==p)]
        return float(sub["amount"].sum())
    # assessments: ASSESSMENT'S NO. ka sum (count), COST ka sum (amount)
    a_sub = assess[(assess["date"]>=d1)&(assess["date"]<=d2)] if len(assess) else assess
    assess_count = int(a_sub["assess_no"].sum()) if len(a_sub) else 0
    assess_amt   = float(a_sub["cost"].sum()) if len(a_sub) else 0.0

    r = {}
    r["New OPDs"]=oc(opd,"NEW OPD"); r["F/U OPDs"]=oc(opd,"OLD OPD")
    r["New Plan"]=pc(plan,"New Plan"); r["Renewals"]=pc(plan,"Renewal"); r["Revivals"]=pc(plan,"Revival")
    r["Inactive"]=len(inactive[(inactive["inactive_date"]>=d1)&(inactive["inactive_date"]<=d2)])
    r["Active"]=len(active[active["active_month_dt"]==active_ref])
    r["Assessments"]=assess_count; r["Suggest RPP"]=rc(opd)
    r["NO2P %"]=pct(r["New Plan"], r["New OPDs"])

    amt = {}
    amt["New OPDs"]=oa(opd,"NEW OPD"); amt["F/U OPDs"]=oa(opd,"OLD OPD")
    amt["New Plan"]=pa(plan,"New Plan"); amt["Renewals"]=pa(plan,"Renewal"); amt["Revivals"]=pa(plan,"Revival")
    amt["Assessments"]=assess_amt
    amt["_total_revenue"]=(amt["New OPDs"]+amt["F/U OPDs"]+amt["New Plan"]
                           +amt["Renewals"]+amt["Revivals"]+amt["Assessments"])
    # NOTE: Assessments ko Revenue me nahi jodna ho to upar line se +amt["Assessments"] hata do
    return r, amt

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

def fmt_amt(v):
    return f"{int(round(v)):,}" if v else ""

def filter_assess(assess, branch=None):
    if branch is None: return assess
    return assess[assess["center"]==branch] if len(assess) else assess

def build_df(opd, plan, active, inactive, assess, scope_name, y_col, m_col, lm_col):
    yd,  yd_amt  = count_range(opd,plan,active,inactive,assess, yesterday,yesterday, pd.Timestamp(month_start))
    mtd, mtd_amt = count_range(opd,plan,active,inactive,assess, month_start,yesterday, pd.Timestamp(month_start))
    lm,  lm_amt  = count_range(opd,plan,active,inactive,assess, lm_start,lm_end, lm_active_ref)
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
        amt_val = fmt_amt(mtd_amt[cat]) if cat in AMOUNT_CATS else ""
        rows.append([cat,yv,mv,lv,vs_txt,amt_val,t_t,t_p,t_pend])
        vs_colors.append(vs_c); tgt_colors.append(t_c)

    # ---- REVENUE row ----
    rev_y, rev_m, rev_l = yd_amt["_total_revenue"], mtd_amt["_total_revenue"], lm_amt["_total_revenue"]
    rev_vs_txt, rev_vs_c = arrow_pct(rev_m, rev_l, reverse=False)
    rev_tgt = get_revenue_target(scope_name)
    if rev_tgt:
        rp = round(rev_m/rev_tgt*100,1) if rev_tgt else 0
        rev_t_t, rev_t_p, rev_t_pend = f"{rev_tgt:,}", f"{rp}%", f"{round(max(0,100-rp),1)}%"
        rev_t_c = "green" if rev_m>=rev_tgt else "red"
    else:
        rev_t_t, rev_t_p, rev_t_pend, rev_t_c = "", "", "", "none"
    rows.append(["Revenue", fmt_amt(rev_y), fmt_amt(rev_m), fmt_amt(rev_l),
                 rev_vs_txt, fmt_amt(rev_m), rev_t_t, rev_t_p, rev_t_pend])
    vs_colors.append(rev_vs_c); tgt_colors.append(rev_t_c)

    cols=["Category",y_col,m_col,lm_col,"vs Last Month","Amount","Target","% Achieved","Pending %"]
    return pd.DataFrame(rows,columns=cols), vs_colors, tgt_colors

y_str=f"{yesterday.day}-{yesterday.strftime('%b')}"
mtd_str=f"{month_start.day}-{yesterday.day} {yesterday.strftime('%b')}"
lm_str=f"{lm_start.day}-{lm_end.day} {lm_start.strftime('%b')}"
Y_COL=f"Yesterday ({y_str})"; M_COL=f"MTD-1 ({mtd_str})"; LM_COL=f"Last Month ({lm_str})"

overall_df, ov_vs, ov_tgt = build_df(opd,plan,active,inactive,assess_df,"Overall",Y_COL,M_COL,LM_COL)
print("Overall ready")

# ====== 6. FORMAT ======
TEAL={"red":0.18,"green":0.55,"blue":0.56}; WHITE={"red":1,"green":1,"blue":1}
GRID={"red":0.3,"green":0.3,"blue":0.3}
G_TXT={"red":0.0,"green":0.5,"blue":0.0}; R_TXT={"red":0.8,"green":0.0,"blue":0.0}
BORDER_STYLE="SOLID_MEDIUM"

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
    rev_row = 2 + n_rows - 1
    req.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":rev_row,"endRowIndex":rev_row+1,"startColumnIndex":0,"endColumnIndex":n_cols},"cell":{"userEnteredFormat":{"backgroundColor":{"red":0.88,"green":0.96,"blue":0.96},"textFormat":{"bold":True}}},"fields":"userEnteredFormat(backgroundColor,textFormat)"}})
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
req=base_format(sid1,nc1,nr1,"Overall Performance — MTD-1 vs Last Month + Target + Revenue")
cols1=list(overall_df.columns)
req+=color_col(sid1, ov_vs, cols1.index("vs Last Month"))
req+=color_col(sid1, ov_tgt, cols1.index("% Achieved"))
sheet.batch_update({"requests":req})
print("Overall_Summary done")

# ====== 8. DURATION HELPERS ======
npd_all = plan[plan["plan_type"] == "New Plan"].copy()
npd_all["total_service_months"] = pd.to_numeric(npd_all["total_service_months"], errors="coerce")

# label banane ka helper (12 -> "1 Year", baaki "N Month")
def _dur_label(m):
    if m == 12: return "1 Year"
    return f"{int(m)} Month"

# DYNAMIC buckets: data me jo bhi durations hain unke rows banenge (0 ko chhod ke).
# preferred order pehle, phir baaki jo bhi mile (numeric order me).
_PREFERRED = [1, 2, 3, 6, 9, 12]
_present = sorted(int(m) for m in npd_all["total_service_months"].dropna().unique() if int(m) != 0)
_ordered = [m for m in _PREFERRED if m in _present] + [m for m in _present if m not in _PREFERRED]
DURATION_BUCKETS = [(m, _dur_label(m)) for m in _ordered]
print(f"Duration buckets (dynamic): {[lbl for _, lbl in DURATION_BUCKETS]}")

def duration_amount_table(npd_branch, d1, d2):
    sub = npd_branch[(npd_branch["enrollment_date"] >= d1) & (npd_branch["enrollment_date"] <= d2)]
    rows = []
    for months, label in DURATION_BUCKETS:
        seg = sub[sub["total_service_months"] == months]
        rows.append([label, len(seg), int(round(float(seg["amount"].sum())))])
    return pd.DataFrame(rows, columns=["New Plan Duration", "Count", "Amount"])

def duration_combined_table(d1, d2, branches_d):
    """Combined: duration x branch COUNT + Total (image jaisa)."""
    rows = []
    for months, label in DURATION_BUCKETS:
        row = [label]; total = 0
        for b in branches_d:
            seg = npd_all[(npd_all["hosp_name"]==b)&
                          (npd_all["enrollment_date"]>=d1)&(npd_all["enrollment_date"]<=d2)&
                          (npd_all["total_service_months"]==months)]
            c = len(seg); row.append(c); total += c
        row.append(total)
        rows.append(row)
    return pd.DataFrame(rows, columns=["New Plan Duration Time"]+branches_d+["Total"])

# ====== 9. PER-BRANCH TABS ======
branches=sorted(set(opd["hosp_name"])|set(plan["hosp_name"])|set(active["hosp_name"])|set(inactive["hosp_name"]))
branches=[b for b in branches if b and b!="Unknown"]

def write_branch_tab(b):
    b_opd=opd[opd["hosp_name"]==b]; b_plan=plan[plan["hosp_name"]==b]
    b_active=active[active["hosp_name"]==b]; b_inactive=inactive[inactive["hosp_name"]==b]
    b_assess=filter_assess(assess_df, b)

    bdf, bvs, btg = build_df(b_opd,b_plan,b_active,b_inactive,b_assess,b,Y_COL,M_COL,LM_COL)

    ws = replace_ws(b, bdf); sid = ws._properties["sheetId"]
    nc = len(bdf.columns); nr = len(bdf)
    req = base_format(sid, nc, nr, f"{b} — Performance (MTD-1 vs Last Month + Target + Revenue)")
    cols = list(bdf.columns)
    req += color_col(sid, bvs, cols.index("vs Last Month"))
    req += color_col(sid, btg, cols.index("% Achieved"))
    sheet.batch_update({"requests": req})

    # duration (Count + Amount), MTD-1
    b_npd = npd_all[npd_all["hosp_name"]==b]
    dur_df = duration_amount_table(b_npd, month_start, yesterday)
    dur_start = 2 + nr + 2
    dnc = len(dur_df.columns); dnr = len(dur_df)
    set_with_dataframe(ws, dur_df, row=dur_start + 2, col=1)
    req2 = []
    req2.append({"mergeCells":{"range":{"sheetId":sid,"startRowIndex":dur_start,"endRowIndex":dur_start+1,"startColumnIndex":0,"endColumnIndex":dnc},"mergeType":"MERGE_ALL"}})
    req2.append({"updateCells":{"rows":[{"values":[{"userEnteredValue":{"stringValue":f"New Plan Duration & Amount — MTD-1 ({mtd_str})"},"userEnteredFormat":{"backgroundColor":TEAL,"horizontalAlignment":"CENTER","verticalAlignment":"MIDDLE","textFormat":{"bold":True,"fontSize":12,"foregroundColor":WHITE}}}]}],"fields":"userEnteredValue,userEnteredFormat","start":{"sheetId":sid,"rowIndex":dur_start,"columnIndex":0}}})
    dhdr = dur_start + 1
    req2.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":dhdr,"endRowIndex":dhdr+1,"startColumnIndex":0,"endColumnIndex":dnc},"cell":{"userEnteredFormat":{"backgroundColor":TEAL,"horizontalAlignment":"CENTER","verticalAlignment":"MIDDLE","textFormat":{"bold":True,"foregroundColor":WHITE},"wrapStrategy":"WRAP"}},"fields":"userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat,wrapStrategy)"}})
    req2.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":dhdr+1,"endRowIndex":dhdr+1+dnr,"startColumnIndex":0,"endColumnIndex":1},"cell":{"userEnteredFormat":{"horizontalAlignment":"LEFT","textFormat":{"bold":True}}},"fields":"userEnteredFormat(horizontalAlignment,textFormat)"}})
    req2.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":dhdr+1,"endRowIndex":dhdr+1+dnr,"startColumnIndex":1,"endColumnIndex":dnc},"cell":{"userEnteredFormat":{"horizontalAlignment":"CENTER"}},"fields":"userEnteredFormat.horizontalAlignment"}})
    req2.append({"updateBorders":{"range":{"sheetId":sid,"startRowIndex":dur_start,"endRowIndex":dhdr+1+dnr,"startColumnIndex":0,"endColumnIndex":dnc},"top":{"style":BORDER_STYLE,"color":GRID},"bottom":{"style":BORDER_STYLE,"color":GRID},"left":{"style":BORDER_STYLE,"color":GRID},"right":{"style":BORDER_STYLE,"color":GRID},"innerHorizontal":{"style":BORDER_STYLE,"color":GRID},"innerVertical":{"style":BORDER_STYLE,"color":GRID}}})
    req2.append({"autoResizeDimensions":{"dimensions":{"sheetId":sid,"dimension":"COLUMNS","startIndex":0,"endIndex":max(nc,dnc)}}})
    sheet.batch_update({"requests": req2})
    return bdf

branch_dfs = {}
for b in branches:
    branch_dfs[b] = write_branch_tab(b)
    print(f"{b} tab done")
print(f"ALL BRANCH TABS DONE ({len(branches)} branches)")

# ====== 9b. COMBINED New_Plan_Duration TAB (Yesterday + MTD-1) ======
def write_combined_block(ws, df, start_row, title_text):
    sid = ws._properties["sheetId"]; nc = len(df.columns)
    set_with_dataframe(ws, df, row=start_row + 2, col=1)
    req = []
    req.append({"mergeCells":{"range":{"sheetId":sid,"startRowIndex":start_row,"endRowIndex":start_row+1,"startColumnIndex":0,"endColumnIndex":nc},"mergeType":"MERGE_ALL"}})
    req.append({"updateCells":{"rows":[{"values":[{"userEnteredValue":{"stringValue":title_text},"userEnteredFormat":{"backgroundColor":TEAL,"horizontalAlignment":"CENTER","verticalAlignment":"MIDDLE","textFormat":{"bold":True,"fontSize":12,"foregroundColor":WHITE}}}]}],"fields":"userEnteredValue,userEnteredFormat","start":{"sheetId":sid,"rowIndex":start_row,"columnIndex":0}}})
    hdr = start_row + 1; nr = len(df)
    req.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":hdr,"endRowIndex":hdr+1,"startColumnIndex":0,"endColumnIndex":nc},"cell":{"userEnteredFormat":{"backgroundColor":TEAL,"horizontalAlignment":"CENTER","verticalAlignment":"MIDDLE","textFormat":{"bold":True,"foregroundColor":WHITE},"wrapStrategy":"WRAP"}},"fields":"userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat,wrapStrategy)"}})
    req.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":hdr+1,"endRowIndex":hdr+1+nr,"startColumnIndex":0,"endColumnIndex":1},"cell":{"userEnteredFormat":{"horizontalAlignment":"LEFT","textFormat":{"bold":True}}},"fields":"userEnteredFormat(horizontalAlignment,textFormat)"}})
    req.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":hdr+1,"endRowIndex":hdr+1+nr,"startColumnIndex":1,"endColumnIndex":nc},"cell":{"userEnteredFormat":{"horizontalAlignment":"CENTER"}},"fields":"userEnteredFormat.horizontalAlignment"}})
    req.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":hdr,"endRowIndex":hdr+1+nr,"startColumnIndex":nc-1,"endColumnIndex":nc},"cell":{"userEnteredFormat":{"textFormat":{"bold":True}}},"fields":"userEnteredFormat.textFormat"}})
    req.append({"updateBorders":{"range":{"sheetId":sid,"startRowIndex":start_row,"endRowIndex":hdr+1+nr,"startColumnIndex":0,"endColumnIndex":nc},"top":{"style":BORDER_STYLE,"color":GRID},"bottom":{"style":BORDER_STYLE,"color":GRID},"left":{"style":BORDER_STYLE,"color":GRID},"right":{"style":BORDER_STYLE,"color":GRID},"innerHorizontal":{"style":BORDER_STYLE,"color":GRID},"innerVertical":{"style":BORDER_STYLE,"color":GRID}}})
    return req

comb_yday = duration_combined_table(yesterday, yesterday, branches)
comb_mtd  = duration_combined_table(month_start, yesterday, branches)
try: sheet.del_worksheet(sheet.worksheet("New_Plan_Duration"))
except gspread.exceptions.WorksheetNotFound: pass
_nb = len(DURATION_BUCKETS)              # kitne duration rows
_ncomb = len(comb_mtd.columns)           # columns (branches + label + total)
_gap = _nb + 5                           # doosra table pehle ke neeche (heading+header+rows+gap)
ws_c = sheet.add_worksheet(title="New_Plan_Duration", rows=str(_gap + _nb + 8), cols=str(_ncomb + 2))
reqc = []
reqc += write_combined_block(ws_c, comb_yday, 0, f"New Plan Duration — Yesterday ({y_str})")
reqc += write_combined_block(ws_c, comb_mtd, _gap, f"New Plan Duration — MTD-1 ({mtd_str})")
reqc.append({"autoResizeDimensions":{"dimensions":{"sheetId":ws_c._properties["sheetId"],"dimension":"COLUMNS","startIndex":0,"endIndex":_ncomb}}})
sheet.batch_update({"requests": reqc})
print("New_Plan_Duration (combined) tab done")

# ====== 10. EMAIL ======
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")

TO  = ["tanmay@emoneeds.com"]
CC  = ["neelesh@emoneeds.com"]
BCC = ["neeleshdwivedirgpv@gmail.com"]

def df_to_html(df, title):
    html = f'<h3 style="font-family:Arial;color:#07333B;margin:14px 0 6px;">{title}</h3>'
    html += '<table style="border-collapse:collapse;font-family:Arial;font-size:13px;">'
    html += '<tr>'
    for col in df.columns:
        html += (f'<th style="background:#028090;color:#ffffff;padding:8px 12px;'
                 f'border:2px solid #555555;text-align:center;">{col}</th>')
    html += '</tr>'
    last_idx = len(df) - 1
    for ridx, (_, row) in enumerate(df.iterrows()):
        rbg = "background:#e0f5f3;" if ridx == last_idx else ""
        html += f'<tr style="{rbg}">'
        for col in df.columns:
            val = row[col]; color = "#000000"
            if "vs" in str(col).lower():
                if "⬆️" in str(val): color = "#1a7f37"
                elif "⬇️" in str(val): color = "#c0392b"
            bold = "font-weight:bold;" if ridx == last_idx else ""
            html += (f'<td style="padding:7px 12px;border:2px solid #555555;'
                     f'text-align:center;color:{color};{bold}">{val}</td>')
        html += '</tr>'
    html += '</table>'
    return html

def comb_to_html(df, title):
    html = f'<h3 style="font-family:Arial;color:#07333B;margin:14px 0 6px;">{title}</h3>'
    html += '<table style="border-collapse:collapse;font-family:Arial;font-size:13px;">'
    html += '<tr>'
    for col in df.columns:
        html += (f'<th style="background:#028090;color:#ffffff;padding:8px 12px;'
                 f'border:2px solid #555555;text-align:center;">{col}</th>')
    html += '</tr>'
    for _, row in df.iterrows():
        html += '<tr>'
        for i, col in enumerate(df.columns):
            align="left" if i==0 else "center"
            bold="font-weight:bold;" if col=="Total" else ""
            html += (f'<td style="padding:7px 12px;border:2px solid #555555;'
                     f'text-align:{align};{bold}">{row[col]}</td>')
        html += '</tr>'
    html += '</table>'
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
      <td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">Month-to-Date — cumulative from the 1st up to yesterday ({_m_str} – {yesterday.strftime('%d %b')}).</td></tr>
  <tr><td style="padding:6px 12px;border-bottom:1px solid #e0eeed;"><b style="color:#07333B;">Last Month</b></td>
      <td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">Same period last month for a like-for-like comparison ({_lm_str}).</td></tr>
  <tr><td style="padding:6px 12px;border-bottom:1px solid #e0eeed;"><b style="color:#07333B;">vs Last Month</b></td>
      <td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">Change in MTD-1 vs same period last month. ⬆️ green = improvement, ⬇️ red = decline.</td></tr>
  <tr><td style="padding:6px 12px;border-bottom:1px solid #e0eeed;"><b style="color:#07333B;">Amount</b></td>
      <td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">MTD-1 revenue for OPDs, New Plan, Renewals, Revivals & Assessments. OPD me 0 ko 1500 maana gaya.</td></tr>
  <tr><td style="padding:6px 12px;border-bottom:1px solid #e0eeed;"><b style="color:#07333B;">Assessments</b></td>
      <td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">Assessments sheet se: count = Assessment's No. ka sum, amount = Cost ka sum.</td></tr>
  <tr><td style="padding:6px 12px;border-bottom:1px solid #e0eeed;"><b style="color:#07333B;">Target / % Achieved / Pending&nbsp;%</b></td>
      <td style="padding:6px 12px;border-bottom:1px solid #e0eeed;">Monthly goal, % achieved as of MTD-1, % remaining.</td></tr>
  <tr><td style="padding:6px 12px;"><b style="color:#07333B;">Revenue</b></td>
      <td style="padding:6px 12px;">OPD + New Plan + Renewal + Revival + Assessments ka total, target.csv ke Revenue target se compare.</td></tr>
</table>
'''

branch_html = ""
for b in branches:
    branch_html += df_to_html(branch_dfs[b], f"{b} — Summary ({_m_str} – {yesterday.strftime('%d %b %Y')})")
    branch_html += "<br><br>"

html_body = f'''
<html><body style="font-family:Arial;color:#222;">
<p>Dear Tanmay,</p>
<p>Please find the <b>Overall Performance Report</b> for
<b>{yesterday.strftime('%d %b %Y')}</b> below, with consolidated and branch-wise
summaries, amounts, assessments, New Plan duration and revenue progress against target.</p>

<p style="margin-bottom:4px;"><b style="color:#07333B;">How to read this report:</b></p>
{legend_html}

{df_to_html(overall_df, f"Overall Summary ({_m_str} – {yesterday.strftime('%d %b %Y')})")}
<br><br>
{branch_html}

{comb_to_html(comb_mtd, f"New Plan Duration — MTD-1 ({_m_str} – {yesterday.strftime('%d %b')})")}

<p style="margin-top:8px;">Favourable movements appear in
<span style="color:#1a7f37;"><b>green</b></span>, unfavourable in
<span style="color:#c0392b;"><b>red</b></span>. The Revenue row totals OPD, New Plan,
Renewal, Revival and Assessment amounts.</p>

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