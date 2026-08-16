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
active["active_month_dt"] = pd.to_datetime(active["active_date"], errors="coerce").dt.to_period("M").dt.to_timestamp()

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

# ---- working days (Sunday chhod ke) — MTD-1 target ke liye ----
def _working_days(d1, d2):
    """d1..d2 (inclusive) me Sunday chhod ke kitne din."""
    if d2 < d1:
        return 0
    cnt = 0; cur = d1
    while cur <= d2:
        if cur.weekday() != 6:   # 6 = Sunday
            cnt += 1
        cur += timedelta(days=1)
    return cnt

# mahine ke total working din (poora mahina) aur ab tak (kal tak) ke working din
_days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]
_month_end = month_start.replace(day=_days_in_month)
_wd_total = _working_days(month_start, _month_end)          # poore mahine ke working din
_wd_sofar = _working_days(month_start, yesterday)           # 1 se kal tak ke working din
_mtd_ratio = (_wd_sofar / _wd_total) if _wd_total else 0
print(f"Working days: {_wd_sofar}/{_wd_total} (Sunday chhod ke), MTD ratio={_mtd_ratio:.2f}")

# ---- BEST MONTH benchmark (har category ka apna best) ----
# Jan-2026 se har purane mahine ka "pehle _wd_sofar working-din tak" ka data,
# har category ka MAX = us category ka best-month benchmark (fair same-period).
BEST_FROM = date(2026, 1, 1)   # yahi se best dhoondo

def _nth_working_day_date(m_start, n):
    """Mahine ki 1 tareekh se n-va working din (Sunday chhod ke) ki date."""
    if n <= 0:
        return m_start
    cnt = 0; cur = m_start
    last = cur
    _dim = calendar.monthrange(m_start.year, m_start.month)[1]
    _mend = m_start.replace(day=_dim)
    while cur <= _mend:
        if cur.weekday() != 6:
            cnt += 1; last = cur
            if cnt >= n:
                return cur
        cur += timedelta(days=1)
    return last  # agar mahine me utne working din nahi to aakhri

def compute_best_month(opd_f, plan_f, active_f, inactive_f, assess_f):
    """Har category ka best-month: same-period aur full-month, saath me kaunsa month tha (naam)."""
    best_sp = {c: 0 for c in CATEGORIES}; best_sp_m = {c: "" for c in CATEGORIES}
    best_fm = {c: 0 for c in CATEGORIES}; best_fm_m = {c: "" for c in CATEGORIES}
    m = BEST_FROM
    while m < month_start:
        _dim = calendar.monthrange(m.year, m.month)[1]
        _mend = m.replace(day=_dim)
        _sp_end = _nth_working_day_date(m, _wd_sofar)
        _mname = m.strftime("%b-%y")   # jaise Mar-26
        rr_sp, _a = count_range(opd_f, plan_f, active_f, inactive_f, assess_f,
                                m, _sp_end, pd.Timestamp(m))
        rr_fm, _a2 = count_range(opd_f, plan_f, active_f, inactive_f, assess_f,
                                 m, _mend, pd.Timestamp(m))
        for c in CATEGORIES:
            if rr_sp.get(c, 0) > best_sp[c]:
                best_sp[c] = rr_sp[c]; best_sp_m[c] = _mname
            if rr_fm.get(c, 0) > best_fm[c]:
                best_fm[c] = rr_fm[c]; best_fm_m[c] = _mname
        if m.month == 12:
            m = date(m.year + 1, 1, 1)
        else:
            m = date(m.year, m.month + 1, 1)
    return best_sp, best_fm, best_sp_m, best_fm_m

# ---- Overall table: Category | Total Target | MTD-1 Target | Best Month | Achieved ----
# graphs ke liye alag se overall ke achieved + dono best store karte hain
OV_ACHIEVED = {}; OV_BEST_SP = {}; OV_BEST_FM = {}
OV_BEST_SP_M = {}; OV_BEST_FM_M = {}   # kaunsa month best tha (naam)

def build_target_vs_achieved(opd_f, plan_f, active_f, inactive_f, assess_f, scope_name, _store=False):
    """MTD achieved vs Total Target, MTD-1 Target (Sunday-adj), aur Best Month (same-period)."""
    r, _amt = count_range(opd_f, plan_f, active_f, inactive_f, assess_f,
                          month_start, yesterday, pd.Timestamp(month_start))
    best_sp, best_fm, best_sp_m, best_fm_m = compute_best_month(opd_f, plan_f, active_f, inactive_f, assess_f)
    if _store:
        for c in CATEGORIES:
            OV_ACHIEVED[c] = r.get(c, 0)
            OV_BEST_SP[c]  = best_sp.get(c, 0);  OV_BEST_SP_M[c] = best_sp_m.get(c, "")
            OV_BEST_FM[c]  = best_fm.get(c, 0);  OV_BEST_FM_M[c] = best_fm_m.get(c, "")
    lm_active = last_month_active_count(active_f)
    rows = []; tgt_colors = []
    for cat in CATEGORIES:
        if cat in PCT_ROWS:
            continue
        ach = r[cat]
        bm = best_sp.get(cat, 0)
        tgt = get_target(scope_name, cat, lm_active)
        if tgt is None:
            rows.append([cat, "", "", bm, ach]); tgt_colors.append("none")
        else:
            mtd_tgt = int(round(tgt * _mtd_ratio))
            rows.append([cat, tgt, mtd_tgt, bm, ach])
            tgt_colors.append("green" if ach >= mtd_tgt else "red")
    df = pd.DataFrame(rows, columns=["Category","Total Target","MTD-1 Target","Best Month","Achieved"])
    return df, tgt_colors

overall_simple, ov_simple_c = build_target_vs_achieved(
    opd, plan, active, inactive, assess_df, "Overall", _store=True)
print("Overall (Total + MTD-1 + Best Month vs Achieved) ready")

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

# ====== 7. OVERALL (simple: Category | Target | Achieved) ======
ws1=replace_ws("Overall_Summary", overall_simple); sid1=ws1._properties["sheetId"]
nc1=len(overall_simple.columns); nr1=len(overall_simple)
req=base_format(sid1,nc1,nr1,f"Overall — Target vs Achieved (MTD: {mtd_str})")
# Achieved column ko green/red (target poora hua ya nahi)
req+=color_col(sid1, ov_simple_c, list(overall_simple.columns).index("Achieved"))
sheet.batch_update({"requests":req})
print("Overall_Summary done (Target vs Achieved)")

# ====== 8. DURATION HELPERS ======
npd_all = plan[plan["plan_type"] == "New Plan"].copy()
npd_all["total_service_months"] = pd.to_numeric(npd_all["total_service_months"], errors="coerce")
# 30 din se kam (0 ya NaN) wale plan ko bhi 1 month maano — koi plan chhute nahi
npd_all["total_service_months"] = npd_all["total_service_months"].fillna(1)
npd_all.loc[npd_all["total_service_months"] < 1, "total_service_months"] = 1

# label banane ka helper (12 -> "1 Year", baaki "N Month")
def _dur_label(m):
    if m == 12: return "1 Year"
    return f"{int(m)} Month"

# DYNAMIC buckets: data me jo bhi durations hain unke rows banenge.
# preferred order pehle, phir baaki jo bhi mile (numeric order me).
_PREFERRED = [1, 2, 3, 6, 9, 12]
_present = sorted(int(m) for m in npd_all["total_service_months"].dropna().unique())
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

# ====== 9c. LEAD-SOURCE WISE MTD-1 TABLE (Overall + branch-wise) ======
# image jaisा: rows = lead sources, cols = New OPD..NO2P%, neeche Total row.
# Late Renewal chhod diya; Total Renewal = plan_type "Renewal" ka count.
LEAD_ORDER = ["Practo","GMB","1 MG","Website","Referral","Organic","Google/ IVR Leads",
              "Whatsapp","Internal Referral","Client Referral","TATA AIA","Primus","Walk-in",
              "Facebook","Digital Leads","Docgenie","Instagram","Practo-Insta","Team Referral"]

# lead source normalize: PRACTO/Practo Book/Practo Call -> "Practo" (par Practo-Insta alag)
def _norm_lead(x):
    s = str(x).strip()
    if not s or s.lower() in ("none","nan",""):
        return "Unknown"
    sl = s.lower()
    if "practo" in sl and "insta" not in sl:   # Practo, PRACTO, Practo Book, Practo Call
        return "Practo"
    if "practo" in sl and "insta" in sl:        # Practo-Insta
        return "Practo-Insta"
    return s

def lead_source_table(opd_f, plan_f, d1, d2):
    """Lead-source wise MTD-1 counts -> DataFrame. Saare LEAD_ORDER dikhao (0 bhi)."""
    o = opd_f[(opd_f["opd_date"]>=d1)&(opd_f["opd_date"]<=d2)].copy()
    p = plan_f[(plan_f["enrollment_date"]>=d1)&(plan_f["enrollment_date"]<=d2)].copy()
    o["_ls"] = o["lead_source"].apply(_norm_lead) if "lead_source" in o.columns else "Unknown"
    p["_ls"] = p["lead_source"].apply(_norm_lead) if "lead_source" in p.columns else "Unknown"

    # HAMESHA poori LEAD_ORDER list, + koi extra jo data me mila par list me nahi
    present = set(o["_ls"]) | set(p["_ls"])
    extra = sorted(present - set(LEAD_ORDER) - {"Unknown"})
    sources = LEAD_ORDER + extra
    if "Unknown" in present:
        sources = sources + ["Unknown"]

    rows = []
    for ls in sources:
        oo = o[o["_ls"]==ls]; pp = p[p["_ls"]==ls]
        new_opd = int((oo["opd_status"]=="NEW OPD").sum())
        old_opd = int((oo["opd_status"]=="OLD OPD").sum())
        total_opd = new_opd + old_opd
        rpp = int((oo["is_suggest_rpp"]=="Yes").sum())
        new_plan = int((pp["plan_type"]=="New Plan").sum())
        total_ren = int((pp["plan_type"]=="Renewal").sum())
        revival = int((pp["plan_type"]=="Revival").sum())
        no2p = round(new_plan/new_opd*100,1) if new_opd else 0.0
        rows.append([ls, new_opd, old_opd, total_opd, rpp, new_plan,
                     total_ren, revival, "", f"{no2p}%"])
    df = pd.DataFrame(rows, columns=["Lead Source","New OPD","OLD OPD","Total OPD",
        "Suggest RPP","Plan","Total Renewal","Revival","Assessment","NO2P %"])
    # Total row
    if len(df):
        tot_new = df["New OPD"].sum(); tot_plan = df["Plan"].sum()
        tot_no2p = round(tot_plan/tot_new*100,1) if tot_new else 0.0
        total_row = ["Total", df["New OPD"].sum(), df["OLD OPD"].sum(), df["Total OPD"].sum(),
                     df["Suggest RPP"].sum(), df["Plan"].sum(), df["Total Renewal"].sum(),
                     df["Revival"].sum(), "", f"{tot_no2p}%"]
        df.loc[len(df)] = total_row
    return df

def lead_table_html(df, title):
    """Lead-source table -> HTML, dark-blue header, purple shading Total OPD & Total Renewal."""
    PURPLE = "#E6E0F0"   # halka purple
    NAVY = "#2E5496"
    shade_cols = {"Total OPD","Total Renewal"}
    html = f'<h3 style="font-family:Arial;color:#07333B;margin:16px 0 6px;">{title}</h3>'
    html += '<table style="border-collapse:collapse;font-family:Arial;font-size:12px;">'
    # header
    html += '<tr>'
    for col in df.columns:
        html += (f'<th style="background:{NAVY};color:#fff;padding:7px 10px;'
                 f'border:1px solid #666;text-align:center;">{col}</th>')
    html += '</tr>'
    last = len(df) - 1
    for ridx, (_, row) in enumerate(df.iterrows()):
        is_total = (ridx == last)
        rbg = "#DCE6F1" if is_total else "#ffffff"
        wt = "bold" if is_total else "normal"
        html += '<tr>'
        for col in df.columns:
            val = row[col]
            align = "left" if col == "Lead Source" else "center"
            bg = rbg
            if col in shade_cols and not is_total:
                bg = PURPLE
            elif col in shade_cols and is_total:
                bg = "#C9BFE0"
            fw = "bold" if (col == "Lead Source" or is_total) else wt
            html += (f'<td style="background:{bg};padding:6px 10px;border:1px solid #666;'
                     f'text-align:{align};font-weight:{fw};">{val}</td>')
        html += '</tr>'
    html += '</table>'
    return html

# Overall + branch-wise lead-source tables (MTD-1)
# Lead-Source Overall — Total OPD descending (Total row neeche rakhna)
lead_overall = lead_source_table(opd, plan, month_start, yesterday)
# Total row alag rakho, baaki sort karo
if len(lead_overall) > 1:
    _total_row = lead_overall.iloc[[-1]]   # last row = Total
    _data_rows = lead_overall.iloc[:-1].copy()
    _data_rows["_sort"] = pd.to_numeric(_data_rows["Total OPD"], errors="coerce").fillna(0)
    _data_rows = _data_rows.sort_values("_sort", ascending=False).drop(columns=["_sort"])
    lead_overall = pd.concat([_data_rows, _total_row], ignore_index=True)
# Branch-wise lead tables band hain (sirf Overall chahiye)
print("Lead-Source tables ready (overall only, Total OPD descending)")

# ====== 10. GRAPHS ======
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as _np

# graph ke 8 categories (aapke diye naam)
GRAPH_CATS = ["New OPD","OLD OPD","Suggest RPP","Plan","Total Renewal","Revival","Assessment"]

def graph_values(opd_f, plan_f, active_f, inactive_f, assess_f, d1, d2):
    """Ek scope ke values (d1..d2 range) -> categories ki list (Total OPD ke bina)."""
    r, _amt = count_range(opd_f, plan_f, active_f, inactive_f, assess_f,
                          d1, d2, pd.Timestamp(month_start))
    return [
        r["New OPDs"],
        r["F/U OPDs"],
        r["Suggest RPP"],
        r["New Plan"],
        r["Renewals"],
        r["Revivals"],
        r["Assessments"],
    ]

_mtd_lbl = f"{month_start.strftime('%d-%b')} to {yesterday.strftime('%d-%b-%Y')}"
_yday_lbl = yesterday.strftime("%d-%b-%Y")

# professional muted palette
CLR_GURGAON = "#4C72B0"   # soft corporate blue
CLR_GK      = "#C44E52"   # muted brick red
CLR_TITLE   = "#2A3F5F"   # dark slate
CLR_GRID    = "#D5D9E0"
CLR_HEADER  = "#3B4A6B"   # table header slate
CLR_ALT     = "#F4F6FA"   # zebra row

def make_branch_graph(filename, d1, d2, title):
    """Branch-wise grouped bar graph + neeche clean totals table (professional)."""
    branch_list = [b for b in ["Gurgaon","GK"] if b in branches] or branches
    data = {b: graph_values(opd[opd["hosp_name"]==b], plan[plan["hosp_name"]==b],
                            active[active["hosp_name"]==b], inactive[inactive["hosp_name"]==b],
                            filter_assess(assess_df, b), d1, d2) for b in branch_list}
    colors = {"Gurgaon": CLR_GURGAON, "GK": CLR_GK}

    x = _np.arange(len(GRAPH_CATS)); n = len(branch_list); width = 0.36

    # figure: upar graph, neeche table — 2 rows, height ratio se spacing
    fig, (ax, ax_t) = plt.subplots(
        2, 1, figsize=(12, 8.6), dpi=150,
        gridspec_kw={"height_ratios": [2.6, 1], "hspace": 0.55})

    # ---- BARS ----
    for i, b in enumerate(branch_list):
        offset = (i - (n-1)/2) * width
        bars = ax.bar(x + offset, data[b], width, label=b,
                      color=colors.get(b), edgecolor="white", linewidth=0.8, zorder=3)
        ax.bar_label(bars, fontsize=9.5, fontweight="bold",
                     color=CLR_TITLE, padding=4)

    ax.set_title(title, fontsize=16, fontweight="bold", color=CLR_TITLE, pad=18)
    ax.set_xticks(x)
    ax.set_xticklabels(GRAPH_CATS, fontsize=10.5, color="#333")
    ax.tick_params(axis="x", length=0, pad=8)
    ax.tick_params(axis="y", labelcolor="#666", length=0)
    ax.set_ylim(0, max(max(v) for v in data.values()) * 1.22 or 1)
    ax.grid(axis="y", linestyle="-", linewidth=0.7, color=CLR_GRID, zorder=0)
    ax.set_axisbelow(True)
    for spine in ["top","right","left"]:
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color(CLR_GRID)
    leg = ax.legend(loc="upper right", frameon=True, fontsize=11,
                    edgecolor=CLR_GRID, framealpha=1)
    leg.get_frame().set_linewidth(0.8)

    # ---- TABLE (neeche, clean) — sirf Category + Total, Total OPD ke bina ----
    ax_t.axis("off")
    TABLE_CATS = ["New OPD","OLD OPD","Suggest RPP","Plan","Total Renewal","Revival","Assessment"]
    col_labels = ["Category", "Total"]
    cell_text = []
    for cat in TABLE_CATS:
        i = GRAPH_CATS.index(cat)
        total = sum(data[b][i] for b in branch_list)
        cell_text.append([cat, str(total)])

    tbl = ax_t.table(cellText=cell_text, colLabels=col_labels,
                     cellLoc="center", loc="center",
                     colWidths=[0.32, 0.16])
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(10.5)
    tbl.scale(1, 1.65)

    ncols = len(col_labels)
    for (rr, cc), cell in tbl.get_celld().items():
        cell.set_edgecolor("#E2E6ED")
        cell.set_linewidth(0.8)
        if rr == 0:   # header
            cell.set_facecolor(CLR_HEADER)
            cell.set_text_props(color="white", fontweight="bold")
            cell.set_height(0.16)
        else:
            cell.set_facecolor("#FFFFFF" if rr % 2 else CLR_ALT)   # zebra
            if cc == 0:   # category name left + bold
                cell.set_text_props(ha="left", fontweight="bold", color=CLR_TITLE)
                cell.PAD = 0.04
            else:         # Total value bold
                cell.set_text_props(fontweight="bold", color=CLR_TITLE)

    fig.savefig(filename, bbox_inches="tight", facecolor="white", pad_inches=0.3)
    plt.close(fig)
    return filename

# MTD branch graph + Yesterday branch graph (dono me neeche clean totals table)
g_mtd = make_branch_graph("graph_mtd.png", month_start, yesterday,
                          f"Branch Wise Performance — MTD ({_mtd_lbl})")
# g_yday band hai (Yesterday graph nahi chahiye)
print("Graphs banaye (MTD branch-wise)")

# ---- New Plan Duration graph: MTD + Yesterday ek hi image me (2 subplots) ----
def make_duration_graph(filename):
    branch_list = [b for b in ["Gurgaon","GK"] if b in branches] or branches
    labels = [lbl for _, lbl in DURATION_BUCKETS]
    colors = {"Gurgaon": CLR_GURGAON, "GK": CLR_GK}

    def _dur_counts(d1, d2):
        out = {b: [] for b in branch_list}
        for months, _lbl in DURATION_BUCKETS:
            for b in branch_list:
                seg = npd_all[(npd_all["hosp_name"]==b)&
                              (npd_all["enrollment_date"]>=d1)&(npd_all["enrollment_date"]<=d2)&
                              (npd_all["total_service_months"]==months)]
                out[b].append(len(seg))
        return out

    d_mtd  = _dur_counts(month_start, yesterday)
    d_yday = _dur_counts(yesterday, yesterday)

    x = _np.arange(len(labels)); n = len(branch_list); width = 0.36
    fig, (axm, axy) = plt.subplots(2, 1, figsize=(11, 9), dpi=150,
                                   gridspec_kw={"hspace": 0.45})

    def _draw(ax, data, title):
        for i, b in enumerate(branch_list):
            off = (i - (n-1)/2) * width
            bars = ax.bar(x + off, data[b], width, label=b,
                          color=colors.get(b), edgecolor="white", linewidth=0.8, zorder=3)
            ax.bar_label(bars, fontsize=9, fontweight="bold", color=CLR_TITLE, padding=3)
        ax.set_title(title, fontsize=14, fontweight="bold", color=CLR_TITLE, pad=12)
        ax.set_xticks(x); ax.set_xticklabels(labels, fontsize=10, color="#333")
        ax.tick_params(axis="x", length=0, pad=6); ax.tick_params(axis="y", labelcolor="#666", length=0)
        _mx = max([max(v) for v in data.values()] + [1])
        ax.set_ylim(0, _mx * 1.25)
        ax.grid(axis="y", linestyle="-", linewidth=0.7, color=CLR_GRID, zorder=0); ax.set_axisbelow(True)
        for s in ["top","right","left"]: ax.spines[s].set_visible(False)
        ax.spines["bottom"].set_color(CLR_GRID)
        ax.legend(loc="upper right", frameon=True, fontsize=10, edgecolor=CLR_GRID, framealpha=1)

    _draw(axm, d_mtd,  f"New Plan Duration — MTD ({_mtd_lbl})")
    _draw(axy, d_yday, f"New Plan Duration — Yesterday ({_yday_lbl})")
    fig.savefig(filename, bbox_inches="tight", facecolor="white", pad_inches=0.3)
    plt.close(fig)
    return filename

g_dur = make_duration_graph("graph_duration.png")
print("New Plan Duration graph banaya (MTD + Yesterday)")

# ---- Active / Inactive branch-wise + Total cards (ek image) ----
def make_active_inactive_graph(filename):
    branch_list = [b for b in ["Gurgaon","GK"] if b in branches] or branches
    # har branch ka active + inactive (MTD-1)
    act = {}; inact = {}
    for b in branch_list:
        rr, _a = count_range(opd[opd["hosp_name"]==b], plan[plan["hosp_name"]==b],
                             active[active["hosp_name"]==b], inactive[inactive["hosp_name"]==b],
                             filter_assess(assess_df, b), month_start, yesterday,
                             pd.Timestamp(month_start))
        act[b] = rr["Active"]; inact[b] = rr["Inactive"]
    tot_act = sum(act.values()); tot_inact = sum(inact.values())

    fig = plt.figure(figsize=(11, 5.5), dpi=150)
    # left: grouped bars (branch-wise active/inactive), right: 2 cards
    ax = fig.add_axes([0.07, 0.12, 0.58, 0.76])
    x = _np.arange(len(branch_list)); width = 0.36
    b1 = ax.bar(x - width/2, [act[b] for b in branch_list], width, label="Active",
                color="#55A868", edgecolor="white", zorder=3)
    b2 = ax.bar(x + width/2, [inact[b] for b in branch_list], width, label="Inactive",
                color="#C44E52", edgecolor="white", zorder=3)
    ax.bar_label(b1, fontsize=10, fontweight="bold", color=CLR_TITLE, padding=3)
    ax.bar_label(b2, fontsize=10, fontweight="bold", color=CLR_TITLE, padding=3)
    ax.set_title(f"Active vs Inactive — Branch Wise (MTD: {_mtd_lbl})",
                 fontsize=14, fontweight="bold", color=CLR_TITLE, pad=14)
    ax.set_xticks(x); ax.set_xticklabels(branch_list, fontsize=11, color="#333")
    ax.tick_params(axis="both", length=0); ax.tick_params(axis="y", labelcolor="#666")
    _mx = max([act[b] for b in branch_list] + [inact[b] for b in branch_list] + [1])
    ax.set_ylim(0, _mx*1.25)
    ax.grid(axis="y", linestyle="-", linewidth=0.7, color=CLR_GRID, zorder=0); ax.set_axisbelow(True)
    for s in ["top","right","left"]: ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(CLR_GRID)
    ax.legend(loc="upper right", frameon=True, fontsize=10, edgecolor=CLR_GRID, framealpha=1)

    # right side: 2 cards (Total Active / Total Inactive)
    def _card(x0, y0, w, h, value, label, color):
        cax = fig.add_axes([x0, y0, w, h]); cax.axis("off")
        cax.add_patch(plt.Rectangle((0,0),1,1, transform=cax.transAxes,
                      facecolor=color, edgecolor="none", zorder=1))
        cax.text(0.5, 0.62, str(value), ha="center", va="center", transform=cax.transAxes,
                 fontsize=34, fontweight="bold", color="white", zorder=2)
        cax.text(0.5, 0.22, label, ha="center", va="center", transform=cax.transAxes,
                 fontsize=12, fontweight="bold", color="white", zorder=2)
    _card(0.70, 0.52, 0.26, 0.36, tot_act,   "Total Active",   "#3E7C46")
    _card(0.70, 0.12, 0.26, 0.36, tot_inact, "Total Inactive", "#A83A3E")

    fig.savefig(filename, bbox_inches="tight", facecolor="white", pad_inches=0.3)
    plt.close(fig)
    return filename

g_actinact = make_active_inactive_graph("graph_actinact.png")
print("Active/Inactive graph banaya (branch-wise + total cards)")

# ---- Benchmark graph: Achieved vs MTD-1 Target vs Best Month (Overall) ----
def make_attainment_graph(filename, best_dict, best_month_dict, title):
    """Horizontal bars: Achieved vs Target(best). Target bar pe 'number (Mon-yy)', group me Attainment %.
       Active/Inactive/Suggest RPP hata ke."""
    _skip = {"Active","Inactive","Suggest RPP"}
    cats = [c for c in CATEGORIES if c not in PCT_ROWS and c not in _skip]
    ach  = [int(OV_ACHIEVED.get(c, 0)) for c in cats]
    best = [int(best_dict.get(c, 0)) for c in cats]
    bmon = [best_month_dict.get(c, "") for c in cats]

    y = _np.arange(len(cats)); height = 0.38
    fig, ax = plt.subplots(figsize=(11, max(4, len(cats)*0.85)), dpi=150)
    bt = ax.barh(y + height/2, best, height, label="Target",
                 color="#C9CCD2", edgecolor="white", zorder=3)
    ba = ax.barh(y - height/2, ach,  height, label="Achieved",
                 color="#2e6fdb", edgecolor="white", zorder=3)
    # target bar pe: number (best-month naam)
    for i, rect in enumerate(bt):
        _lbl = f"{best[i]} ({bmon[i]})" if bmon[i] else f"{best[i]}"
        ax.text(rect.get_width(), rect.get_y() + rect.get_height()/2, f" {_lbl}",
                va="center", ha="left", fontsize=8.5, fontweight="bold", color="#555")
    ax.bar_label(ba, fontsize=9, fontweight="bold", color=CLR_TITLE, padding=3)

    _mx = max(ach + best + [1])
    for i, c in enumerate(cats):
        pct = round(ach[i] / best[i] * 100) if best[i] else 0
        ax.text(_mx * 1.22, y[i], f"{pct}%", va="center", ha="right",
                fontsize=10, fontweight="bold",
                color=("#1a7f37" if pct >= 100 else "#c0392b"))

    ax.set_yticks(y); ax.set_yticklabels(cats, fontsize=11, color="#333")
    ax.invert_yaxis()
    ax.set_xlim(0, _mx * 1.30)
    ax.tick_params(axis="both", length=0); ax.tick_params(axis="x", labelcolor="#666")
    ax.set_title(title, fontsize=14.5, fontweight="bold", color=CLR_TITLE, pad=16)
    ax.grid(axis="x", linestyle="-", linewidth=0.7, color=CLR_GRID, zorder=0); ax.set_axisbelow(True)
    for s in ["top","right","left"]: ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(CLR_GRID)
    ax.text(_mx * 1.22, -0.7, "Attainment", va="center", ha="right",
            fontsize=9.5, fontweight="bold", color="#555")
    ax.legend(loc="lower right", frameon=True, fontsize=10, edgecolor=CLR_GRID, framealpha=1)
    fig.savefig(filename, bbox_inches="tight", facecolor="white", pad_inches=0.3)
    plt.close(fig)
    return filename

# date range label (1 Aug 2026 – 12 Aug 2026) — Windows-safe
try:
    _range_lbl = f"{month_start.strftime('%-d %b %Y')} – {yesterday.strftime('%-d %b %Y')}"
except ValueError:
    # Windows me %-d nahi chalta -> %d (leading zero) use karo
    _range_lbl = f"{month_start.strftime('%d %b %Y')} – {yesterday.strftime('%d %b %Y')}"

# Graph 1: MTD same-period best | Graph 2: full-month best
g_bench_mtd = make_attainment_graph("graph_bench_mtd.png", OV_BEST_SP, OV_BEST_SP_M,
                f"Achieved vs Target — {_range_lbl}")
g_bench_full = make_attainment_graph("graph_bench_full.png", OV_BEST_FM, OV_BEST_FM_M,
                f"Achieved vs Target (Full Month Best) — {month_start.strftime('%b %Y')}")
print("Benchmark graphs banaye (MTD-1 + Full Month, Achieved vs Target)")


# ============================================================
# 12. WHATSAPP — saari images 1-1 personal number pe (Cloud API)
# .env / GitHub secrets: WA_TOKEN, WA_PHONE_ID, WA_RECIPIENTS
# WA_PHONE_ID abhi test number ka: 1250365724826358
# ============================================================
import requests as _wa_http

WA_TOKEN     = os.environ.get("WA_TOKEN")
WA_PHONE_ID  = os.environ.get("WA_PHONE_ID")
WA_RECIPIENTS = [n.strip() for n in os.environ.get("WA_RECIPIENTS", "").split(",") if n.strip()]

# ---- table (DataFrame) ko image banane ka helper ----
def df_to_png(df, title, filename, shade_cols=None):
    """DataFrame -> PNG image (dark-blue header, purple shading optional)."""
    shade_cols = shade_cols or set()
    nr, ncol = df.shape
    fig_w = max(7, ncol * 1.5)
    fig_h = max(2.2, (nr + 2) * 0.42)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=150)
    ax.axis("off")
    ax.set_title(title, fontsize=13, fontweight="bold", color="#1a3a6b", pad=12)
    tbl = ax.table(cellText=df.values.tolist(), colLabels=df.columns.tolist(),
                   cellLoc="center", loc="center")
    tbl.auto_set_font_size(False); tbl.set_fontsize(8.5); tbl.scale(1, 1.4)
    cols = list(df.columns)
    last = nr - 1
    for (rr, cc), cell in tbl.get_celld().items():
        cell.set_edgecolor("#888"); cell.set_linewidth(0.6)
        if rr == 0:   # header
            cell.set_facecolor("#2E5496"); cell.set_text_props(color="white", fontweight="bold")
        else:
            ridx = rr - 1
            is_total = (ridx == last)
            colname = cols[cc]
            if is_total:
                cell.set_facecolor("#DCE6F1"); cell.set_text_props(fontweight="bold")
            elif colname in shade_cols:
                cell.set_facecolor("#E6E0F0")
            else:
                cell.set_facecolor("#ffffff")
            if cc == 0:
                cell.set_text_props(ha="left", fontweight="bold")
    fig.savefig(filename, bbox_inches="tight", facecolor="white", pad_inches=0.25)
    plt.close(fig)
    return filename

# ---- WhatsApp upload + send ----
def wa_upload(filepath):
    url = f"https://graph.facebook.com/v20.0/{WA_PHONE_ID}/media"
    with open(filepath, "rb") as f:
        files = {"file": (os.path.basename(filepath), f, "image/png")}
        data = {"messaging_product": "whatsapp", "type": "image/png"}
        headers = {"Authorization": f"Bearer {WA_TOKEN}"}
        resp = _wa_http.post(url, headers=headers, files=files, data=data, timeout=60)
    resp.raise_for_status()
    return resp.json()["id"]

def wa_send_image(to_number, media_id, caption=""):
    url = f"https://graph.facebook.com/v20.0/{WA_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to_number, "type": "image",
               "image": {"id": media_id, "caption": caption}}
    resp = _wa_http.post(url, headers=headers, json=payload, timeout=60)
    if resp.status_code >= 400:
        print(f"  WA {to_number}: {resp.status_code} {resp.text[:250]}")
        return False
    return True

if WA_TOKEN and WA_PHONE_ID and WA_RECIPIENTS:
    print("WhatsApp: images bhej rahe hain...")
    # lead-source + overall tables ko image banao
    _shade = {"Total OPD", "Total Renewal"}
    img_lead_ov = df_to_png(lead_overall, "Lead-Source Wise MTD-1 — Overall",
                            "wa_lead_overall.png", _shade)

    # sirf 5 cheezein WhatsApp pe
    wa_items = [
        (f"Branch Wise Performance — MTD ({_mtd_lbl})",          g_mtd),
        (f"Achieved vs Best Month — MTD ({_range_lbl})",          g_bench_mtd),
        (f"Achieved vs Best Month — Full Month ({month_start.strftime('%b %Y')})", g_bench_full),
        (f"Active & Inactive — Branch Wise (MTD: {_mtd_lbl})",   g_actinact),
        ("Lead-Source Wise MTD-1 — Overall",                      img_lead_ov),
    ]

    try:
        for num in WA_RECIPIENTS:
            for cap, path in wa_items:
                mid = wa_upload(path)
                wa_send_image(num, mid, caption=cap)
            print(f"  {num} ko {len(wa_items)} images bhej di")
        print(f"WhatsApp report bheji: {len(WA_RECIPIENTS)} number(s)")
    except Exception as e:
        print(f"WhatsApp error: {e}")
else:
    print("WhatsApp: WA_TOKEN/WA_PHONE_ID/WA_RECIPIENTS .env me nahi — skip")