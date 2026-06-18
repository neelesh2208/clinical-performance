from dotenv import load_dotenv
load_dotenv()
import os, json
import pandas as pd
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread_dataframe import set_with_dataframe

from config import DB_CONFIG
from queries import (
    ACTIVE_QUERY,
    INACTIVE_QUERY,
    PLAN_QUERY,
    OPD_QUERY,
    SESSION_QUERY,
)


# ======================================
# 1. DATABASE — RUN ALL QUERIES
# ======================================

engine = create_engine(
    f"postgresql+psycopg2://{DB_CONFIG['user']}:{quote_plus(DB_CONFIG['password'])}"
    f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
)
print("Database Connected ✅")

# Har query ka tab-naam aur SQL ek jagah.
# Nayi query add karni ho to queries.py mein constant banao aur yahan ek line jodo.
REPORTS = [
    {"tab": "Active_Patients",   "query": ACTIVE_QUERY},
    {"tab": "Inactive_Patients", "query": INACTIVE_QUERY},
    {"tab": "Plan_Data",         "query": PLAN_QUERY},
    {"tab": "OPD_Data",          "query": OPD_QUERY},
    {"tab": "Session_Data",      "query": SESSION_QUERY},
]

# STEP 1: Pehle saari queries run karo aur df store karo
for r in REPORTS:
    print(f"Running {r['tab']}...")
    with engine.connect() as conn:
        r["df"] = pd.read_sql(text(r["query"]), conn)
    print(f"  {len(r['df'])} rows")
    r["df"].to_excel(f"{r['tab']}.xlsx", index=False)

print("All Queries Executed ✅")
print("Backup Excel Files Saved ✅")

engine.dispose()
print("Database Closed ✅")


# ======================================
# 2. GOOGLE SHEET AUTO UPDATE
# ======================================

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

# GitHub Actions pe GCP_CREDENTIALS secret se, local pe credentials.json file se
raw = os.environ.get("GCP_CREDENTIALS")
if raw:
    info = json.loads(raw)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(info, scope)
else:
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)

client = gspread.authorize(creds)

sheet = client.open_by_key("1MEXTTUZCkN0OH6aXa36FSqtDmL73LWvf3Aj5Cx-y450")


def replace_worksheet(sheet, title, df):
    """Delete the worksheet if it exists, recreate it sized to the data, and write it."""
    try:
        sheet.del_worksheet(sheet.worksheet(title))
    except gspread.exceptions.WorksheetNotFound:
        pass

    # Size data ke hisaab se — fixed 50000 nahi, isliye cell-limit cross nahi hoga
    n_rows = len(df) + 10
    n_cols = len(df.columns) + 2

    ws = sheet.add_worksheet(title=title, rows=str(n_rows), cols=str(n_cols))
    set_with_dataframe(ws, df)
    print(f"'{title}' updated ✅ ({len(df)} rows)")


# STEP 2: Ab har report apne tab mein
for r in REPORTS:
    replace_worksheet(sheet, r["tab"], r["df"])

print("Google Sheet Updated ✅")

# ============================================================
# 3. YESTERDAY PERFORMANCE REPORT (today - 1)
# ============================================================
import re
import numpy as np
from datetime import date, timedelta

def get_df(tab_name):
    for r in REPORTS:
        if r["tab"] == tab_name:
            return r["df"]
    raise ValueError(f"{tab_name} REPORTS me nahi mila")

active_df   = get_df("Active_Patients").copy()
inactive_df = get_df("Inactive_Patients").copy()
opd_df      = get_df("OPD_Data").copy()
plan_df     = get_df("Plan_Data").copy()
session_df  = get_df("Session_Data").copy()

yesterday = date.today() - timedelta(days=1)
month_start = yesterday.replace(day=1)

def norm_name(x):
    if x is None:
        return None
    s = str(x).strip()
    if s == "" or s.lower() in ("none", "nan"):
        return None
    s = re.sub(r"\s+", " ", s)
    return s.strip()

# name_map: tech_name -> final_name (sirf psychologist/psychiatrist pe)
_nm = pd.read_csv("name_map.csv", dtype=str, keep_default_na=False)
_nm["tech_name"] = _nm["tech_name"].astype(str).str.strip()
_nm["final_name"] = _nm["final_name"].astype(str).str.strip()
NAME_MAP = dict(zip(_nm["tech_name"], _nm["final_name"]))

def map_final(x):
    s = norm_name(x)
    if s is None:
        return None
    if s in NAME_MAP:
        fn = NAME_MAP[s]
        return fn if fn != "" else None
    return s

# A. OPD counts (yesterday)
opd_df["opd_date"] = pd.to_datetime(opd_df["opd_date"], errors="coerce").dt.date
opd_y = opd_df[opd_df["opd_date"] == yesterday].copy()
opd_y["therapist"] = opd_y["assigned_to_name"].apply(map_final)
opd_y = opd_y[opd_y["therapist"].notna()]
opd_grp = opd_y.groupby("therapist").agg(
    new_opd=("opd_status", lambda s: (s == "NEW OPD").sum()),
    fu_opd=("opd_status",  lambda s: (s == "OLD OPD").sum()),
    rpp_suggested=("is_suggest_rpp", lambda s: (s == "Yes").sum()),
).reset_index()
opd_grp["total_opd"] = opd_grp["new_opd"] + opd_grp["fu_opd"]

# B. Sessions Done (yesterday) via CSV mapping
tmap = pd.read_csv("therapist_map.csv")
tmap["user_id"] = tmap["user_id"].astype(str).str.strip()
tmap["therapist"] = tmap["therapist_name"].apply(map_final)
id_to_name = dict(zip(tmap["user_id"], tmap["therapist"]))
session_df["slot_date"] = pd.to_datetime(session_df["slot_date"], errors="coerce").dt.date
sess_y = session_df[session_df["slot_date"] == yesterday].copy()
sess_y["user_id"] = sess_y["user_id"].astype(str).str.strip()
sess_y["therapist"] = sess_y["user_id"].map(id_to_name)
sess_y = sess_y[sess_y["therapist"].notna()]
sess_grp = sess_y.groupby("therapist")["total_sessions"].sum().reset_index()
sess_grp.columns = ["therapist", "sessions_done"]

# C. Convergation (FINAL) — kal ki NEW OPD + usi patient ka plan KAL hi bana
#    Match: patient_ref_id | Therapist: OPD wala (assigned_to_name)
opd_df["opd_date"] = pd.to_datetime(opd_df["opd_date"], errors="coerce").dt.date
opd_new_y = opd_df[
    (opd_df["opd_status"] == "NEW OPD")
    & (opd_df["opd_date"] == yesterday)
].copy()
opd_new_y["therapist"] = opd_new_y["assigned_to_name"].apply(map_final)
opd_new_y["patient_ref_id"] = opd_new_y["patient_ref_id"].astype(str).str.strip()
opd_new_y = opd_new_y[opd_new_y["therapist"].notna()]

plan_df["enrollment_date"] = pd.to_datetime(plan_df["enrollment_date"], errors="coerce").dt.date
plan_y = plan_df[
    (plan_df["plan_status"] == "NEW PLAN")
    & (plan_df["enrollment_date"] == yesterday)
].copy()
plan_y["patient_ref_id"] = plan_y["patient_ref_id"].astype(str).str.strip()
converted_patients = set(plan_y["patient_ref_id"].dropna())

opd_new_y["converted"] = opd_new_y["patient_ref_id"].isin(converted_patients)
conv_rows = opd_new_y[opd_new_y["converted"]].copy()
conv_rows = conv_rows.drop_duplicates(subset=["patient_ref_id", "therapist"])
conv_grp = conv_rows.groupby("therapist").size().reset_index(name="convergation")
# D. Active count — current month (psychologist + psychiatrist)
active_df["active_month_dt"] = pd.to_datetime(active_df["active_month"], format="%b-%y", errors="coerce")
cur = pd.Timestamp(month_start)
act_m = active_df[active_df["active_month_dt"] == cur].copy()
a1 = act_m.assign(therapist=act_m["psychologist_name"].apply(map_final))
a2 = act_m.assign(therapist=act_m["psychiatrist_name"].apply(map_final))
act_long = pd.concat([a1[["therapist"]], a2[["therapist"]]], ignore_index=True)
act_long = act_long[act_long["therapist"].notna()]
active_grp = act_long.groupby("therapist").size().reset_index(name="active_client")

# E. Inactive count — current month
inactive_df["inactive_date"] = pd.to_datetime(inactive_df["inactive_date"], errors="coerce").dt.date
inact_m = inactive_df[
    (inactive_df["inactive_date"] >= month_start)
    & (inactive_df["inactive_date"] <= yesterday)
].copy()
i1 = inact_m.assign(therapist=inact_m["psychologist_name"].apply(map_final))
i2 = inact_m.assign(therapist=inact_m["psychiatrist_name"].apply(map_final))
inact_long = pd.concat([i1[["therapist"]], i2[["therapist"]]], ignore_index=True)
inact_long = inact_long[inact_long["therapist"].notna()]
inactive_grp = inact_long.groupby("therapist").size().reset_index(name="inactive_client")

# F. Merge
base = pd.DataFrame({"therapist": active_grp["therapist"]})
base = pd.concat([base, opd_grp[["therapist"]], sess_grp[["therapist"]],
                  conv_grp[["therapist"]], inactive_grp[["therapist"]]],
                 ignore_index=True).drop_duplicates()
base = base[base["therapist"].notna()].sort_values("therapist").reset_index(drop=True)

summary = base \
    .merge(opd_grp,      on="therapist", how="left") \
    .merge(conv_grp,     on="therapist", how="left") \
    .merge(sess_grp,     on="therapist", how="left") \
    .merge(active_grp,   on="therapist", how="left") \
    .merge(inactive_grp, on="therapist", how="left")

num_cols = ["new_opd","fu_opd","total_opd","rpp_suggested",
            "convergation","sessions_done","active_client","inactive_client"]
for c in num_cols:
    if c not in summary.columns:
        summary[c] = 0
    summary[c] = summary[c].fillna(0).astype(int)

summary = summary[["therapist","new_opd","fu_opd","total_opd","rpp_suggested",
                   "convergation","sessions_done","active_client","inactive_client"]]
summary.columns = ["Therapist","NEW OPD","F/U OPD","Total OPD Done","RPP Suggested",
                   "Convergation","Sessions Done","Active Client","Inactive Client"]

print(f"Yesterday ({yesterday}) summary: {len(summary)} therapists")

# G. Sheet me likho + FORMATTING + highlight
TAB = "Yesterday_Performance"
replace_worksheet(sheet, TAB, summary)
ws = sheet.worksheet(TAB)

header = list(summary.columns)
n_cols = len(header)
n_rows = len(summary)
sheet_id = ws._properties["sheetId"]

TEAL    = {"red":0.18,"green":0.55,"blue":0.56}
WHITE   = {"red":1,"green":1,"blue":1}
GREEN   = {"red":0.72,"green":0.88,"blue":0.72}
RED     = {"red":0.96,"green":0.78,"blue":0.78}
GRID    = {"red":0.55,"green":0.55,"blue":0.55}

requests = []

# 1) Title row insert
requests.append({"insertDimension": {
    "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
    "inheritFromBefore": False}})
# 2) Title merge
requests.append({"mergeCells": {
    "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
              "startColumnIndex": 0, "endColumnIndex": n_cols}, "mergeType": "MERGE_ALL"}})
# 3) Title text + style
title_text = f"Yesterday({yesterday.strftime('%d-%m-%Y')}) Clinical Performance"
requests.append({"updateCells": {
    "rows": [{"values": [{
        "userEnteredValue": {"stringValue": title_text},
        "userEnteredFormat": {"backgroundColor": TEAL, "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
            "textFormat": {"bold": True, "fontSize": 12, "foregroundColor": WHITE}}}]}],
    "fields": "userEnteredValue,userEnteredFormat",
    "start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": 0}}})

HDR_ROW = 1
DATA_START = 2

# 4) Header style
requests.append({"repeatCell": {
    "range": {"sheetId": sheet_id, "startRowIndex": HDR_ROW, "endRowIndex": HDR_ROW+1,
              "startColumnIndex": 0, "endColumnIndex": n_cols},
    "cell": {"userEnteredFormat": {"backgroundColor": TEAL, "horizontalAlignment": "CENTER",
        "verticalAlignment": "MIDDLE", "textFormat": {"bold": True, "foregroundColor": WHITE},
        "wrapStrategy": "WRAP"}},
    "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat,wrapStrategy)"}})

# 5) Data align
requests.append({"repeatCell": {
    "range": {"sheetId": sheet_id, "startRowIndex": DATA_START, "endRowIndex": DATA_START+n_rows,
              "startColumnIndex": 1, "endColumnIndex": n_cols},
    "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
    "fields": "userEnteredFormat.horizontalAlignment"}})
requests.append({"repeatCell": {
    "range": {"sheetId": sheet_id, "startRowIndex": DATA_START, "endRowIndex": DATA_START+n_rows,
              "startColumnIndex": 0, "endColumnIndex": 1},
    "cell": {"userEnteredFormat": {"horizontalAlignment": "LEFT", "textFormat": {"bold": True}}},
    "fields": "userEnteredFormat(horizontalAlignment,textFormat)"}})

# 6) Borders
requests.append({"updateBorders": {
    "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": DATA_START+n_rows,
              "startColumnIndex": 0, "endColumnIndex": n_cols},
    "top": {"style":"SOLID","color":GRID}, "bottom": {"style":"SOLID","color":GRID},
    "left": {"style":"SOLID","color":GRID}, "right": {"style":"SOLID","color":GRID},
    "innerHorizontal": {"style":"SOLID","color":GRID}, "innerVertical": {"style":"SOLID","color":GRID}}})

# 7) Highlight
highlight_cols = ["NEW OPD","F/U OPD","Total OPD Done","RPP Suggested",
                  "Convergation","Sessions Done","Active Client","Inactive Client"]
reverse_cols = {"Inactive Client"}
for col in highlight_cols:
    cidx = header.index(col)
    vals = summary[col].tolist()
    nonzero = [v for v in vals if v != 0]
    if not nonzero:
        continue
    hi, lo = max(nonzero), min(nonzero)
    green_val, red_val = (lo, hi) if col in reverse_cols else (hi, lo)
    for ridx, v in enumerate(vals):
        color = None
        if v != 0 and v == green_val:
            color = GREEN
        elif v != 0 and v == red_val and hi != lo:
            color = RED
        if color:
            requests.append({"repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": DATA_START+ridx,
                          "endRowIndex": DATA_START+ridx+1, "startColumnIndex": cidx,
                          "endColumnIndex": cidx+1},
                "cell": {"userEnteredFormat": {"backgroundColor": color}},
                "fields": "userEnteredFormat.backgroundColor"}})

# 8) Auto width + row heights
requests.append({"autoResizeDimensions": {
    "dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": n_cols}}})
requests.append({"updateDimensionProperties": {
    "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
    "properties": {"pixelSize": 30}, "fields": "pixelSize"}})
requests.append({"updateDimensionProperties": {
    "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": HDR_ROW, "endIndex": HDR_ROW+1},
    "properties": {"pixelSize": 42}, "fields": "pixelSize"}})

if requests:
    sheet.batch_update({"requests": requests})

print(f"Yesterday_Performance tab formatted ({yesterday.strftime('%d-%m-%Y')})")

# ============================================================
# 4. MTD PERFORMANCE REPORT (1st se kal tak)  -> alag tab
# ============================================================
from datetime import date as _date, timedelta as _td

_y = _date.today() - _td(days=1)
_m_start = _y.replace(day=1)

def _get(tab):
    for r in REPORTS:
        if r["tab"] == tab:
            return r["df"].copy()
    raise ValueError(tab + " nahi mila")

_active   = _get("Active_Patients")
_inactive = _get("Inactive_Patients")
_opd      = _get("OPD_Data")
_plan     = _get("Plan_Data")
_session  = _get("Session_Data")

# A. OPD (1 se kal tak)
_opd["opd_date"] = pd.to_datetime(_opd["opd_date"], errors="coerce").dt.date
_opd_r = _opd[(_opd["opd_date"] >= _m_start) & (_opd["opd_date"] <= _y)].copy()
_opd_r["therapist"] = _opd_r["assigned_to_name"].apply(map_final)
_opd_r = _opd_r[_opd_r["therapist"].notna()]
_opd_grp = _opd_r.groupby("therapist").agg(
    new_opd=("opd_status", lambda s: (s == "NEW OPD").sum()),
    fu_opd=("opd_status",  lambda s: (s == "OLD OPD").sum()),
    rpp_suggested=("is_suggest_rpp", lambda s: (s == "Yes").sum()),
).reset_index()
_opd_grp["total_opd"] = _opd_grp["new_opd"] + _opd_grp["fu_opd"]

# B. Sessions (1 se kal tak)
_session["slot_date"] = pd.to_datetime(_session["slot_date"], errors="coerce").dt.date
_sess_r = _session[(_session["slot_date"] >= _m_start) & (_session["slot_date"] <= _y)].copy()
_sess_r["user_id"] = _sess_r["user_id"].astype(str).str.strip()
_sess_r["therapist"] = _sess_r["user_id"].map(id_to_name)
_sess_r = _sess_r[_sess_r["therapist"].notna()]
_sess_grp = _sess_r.groupby("therapist")["total_sessions"].sum().reset_index()
_sess_grp.columns = ["therapist", "sessions_done"]

# C. Convergation — is range me NEW OPD + usi patient ka plan isi range me
_plan["enrollment_date"] = pd.to_datetime(_plan["enrollment_date"], errors="coerce").dt.date
_opd_new = _opd_r[_opd_r["opd_status"] == "NEW OPD"].copy()
_opd_new["patient_ref_id"] = _opd_new["patient_ref_id"].astype(str).str.strip()
_plan_r = _plan[(_plan["plan_status"] == "NEW PLAN")
                & (_plan["enrollment_date"] >= _m_start)
                & (_plan["enrollment_date"] <= _y)].copy()
_plan_r["patient_ref_id"] = _plan_r["patient_ref_id"].astype(str).str.strip()
_conv_pat = set(_plan_r["patient_ref_id"].dropna())
_opd_new["converted"] = _opd_new["patient_ref_id"].isin(_conv_pat)
_cr = _opd_new[_opd_new["converted"]].drop_duplicates(subset=["patient_ref_id", "therapist"])
_conv_grp = _cr.groupby("therapist").size().reset_index(name="convergation")

# D. Active (MTD)
_active["active_month_dt"] = pd.to_datetime(_active["active_month"], format="%b-%y", errors="coerce")
_cur = pd.Timestamp(_m_start)
_act_m = _active[_active["active_month_dt"] == _cur].copy()
_a1 = _act_m.assign(therapist=_act_m["psychologist_name"].apply(map_final))
_a2 = _act_m.assign(therapist=_act_m["psychiatrist_name"].apply(map_final))
_act_long = pd.concat([_a1[["therapist"]], _a2[["therapist"]]], ignore_index=True)
_act_long = _act_long[_act_long["therapist"].notna()]
_active_grp = _act_long.groupby("therapist").size().reset_index(name="active_client")

# E. Inactive (MTD)
_inactive["inactive_date"] = pd.to_datetime(_inactive["inactive_date"], errors="coerce").dt.date
_in_m = _inactive[(_inactive["inactive_date"] >= _m_start)
                  & (_inactive["inactive_date"] <= _y)].copy()
_i1 = _in_m.assign(therapist=_in_m["psychologist_name"].apply(map_final))
_i2 = _in_m.assign(therapist=_in_m["psychiatrist_name"].apply(map_final))
_in_long = pd.concat([_i1[["therapist"]], _i2[["therapist"]]], ignore_index=True)
_in_long = _in_long[_in_long["therapist"].notna()]
_inactive_grp = _in_long.groupby("therapist").size().reset_index(name="inactive_client")

# F. Merge
_base = pd.concat([_active_grp[["therapist"]], _opd_grp[["therapist"]],
                   _sess_grp[["therapist"]], _conv_grp[["therapist"]],
                   _inactive_grp[["therapist"]]], ignore_index=True).drop_duplicates()
_base = _base[_base["therapist"].notna()].sort_values("therapist").reset_index(drop=True)
_mtd = _base \
    .merge(_opd_grp, on="therapist", how="left") \
    .merge(_conv_grp, on="therapist", how="left") \
    .merge(_sess_grp, on="therapist", how="left") \
    .merge(_active_grp, on="therapist", how="left") \
    .merge(_inactive_grp, on="therapist", how="left")
for c in ["new_opd","fu_opd","total_opd","rpp_suggested","convergation",
          "sessions_done","active_client","inactive_client"]:
    if c not in _mtd.columns:
        _mtd[c] = 0
    _mtd[c] = _mtd[c].fillna(0).astype(int)
_mtd = _mtd[["therapist","new_opd","fu_opd","total_opd","rpp_suggested",
             "convergation","sessions_done","active_client","inactive_client"]]
_mtd.columns = ["Therapist","NEW OPD","F/U OPD","Total OPD Done","RPP Suggested",
                "Convergation","Sessions Done","Active Client","Inactive Client"]

# G. Sheet write + format (alag tab)
_TAB = "MTD_Performance"
replace_worksheet(sheet, _TAB, _mtd)
_ws = sheet.worksheet(_TAB)
_hdr = list(_mtd.columns); _nc = len(_hdr); _nr = len(_mtd)
_sid = _ws._properties["sheetId"]

_TEAL={"red":0.18,"green":0.55,"blue":0.56}; _WHITE={"red":1,"green":1,"blue":1}
_GREEN={"red":0.72,"green":0.88,"blue":0.72}; _RED={"red":0.96,"green":0.78,"blue":0.78}
_GRID={"red":0.55,"green":0.55,"blue":0.55}

_req=[]
_req.append({"insertDimension":{"range":{"sheetId":_sid,"dimension":"ROWS","startIndex":0,"endIndex":1},"inheritFromBefore":False}})
_req.append({"mergeCells":{"range":{"sheetId":_sid,"startRowIndex":0,"endRowIndex":1,"startColumnIndex":0,"endColumnIndex":_nc},"mergeType":"MERGE_ALL"}})
_title=f"MTD({_m_start.strftime('%d-%m-%Y')} to {_y.strftime('%d-%m-%Y')}) Clinical Performance"
_req.append({"updateCells":{"rows":[{"values":[{"userEnteredValue":{"stringValue":_title},"userEnteredFormat":{"backgroundColor":_TEAL,"horizontalAlignment":"CENTER","verticalAlignment":"MIDDLE","textFormat":{"bold":True,"fontSize":12,"foregroundColor":_WHITE}}}]}],"fields":"userEnteredValue,userEnteredFormat","start":{"sheetId":_sid,"rowIndex":0,"columnIndex":0}}})
_HR=1; _DS=2
_req.append({"repeatCell":{"range":{"sheetId":_sid,"startRowIndex":_HR,"endRowIndex":_HR+1,"startColumnIndex":0,"endColumnIndex":_nc},"cell":{"userEnteredFormat":{"backgroundColor":_TEAL,"horizontalAlignment":"CENTER","verticalAlignment":"MIDDLE","textFormat":{"bold":True,"foregroundColor":_WHITE},"wrapStrategy":"WRAP"}},"fields":"userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat,wrapStrategy)"}})
_req.append({"repeatCell":{"range":{"sheetId":_sid,"startRowIndex":_DS,"endRowIndex":_DS+_nr,"startColumnIndex":1,"endColumnIndex":_nc},"cell":{"userEnteredFormat":{"horizontalAlignment":"CENTER"}},"fields":"userEnteredFormat.horizontalAlignment"}})
_req.append({"repeatCell":{"range":{"sheetId":_sid,"startRowIndex":_DS,"endRowIndex":_DS+_nr,"startColumnIndex":0,"endColumnIndex":1},"cell":{"userEnteredFormat":{"horizontalAlignment":"LEFT","textFormat":{"bold":True}}},"fields":"userEnteredFormat(horizontalAlignment,textFormat)"}})
_req.append({"updateBorders":{"range":{"sheetId":_sid,"startRowIndex":0,"endRowIndex":_DS+_nr,"startColumnIndex":0,"endColumnIndex":_nc},"top":{"style":"SOLID","color":_GRID},"bottom":{"style":"SOLID","color":_GRID},"left":{"style":"SOLID","color":_GRID},"right":{"style":"SOLID","color":_GRID},"innerHorizontal":{"style":"SOLID","color":_GRID},"innerVertical":{"style":"SOLID","color":_GRID}}})

_hl=["NEW OPD","F/U OPD","Total OPD Done","RPP Suggested","Convergation","Sessions Done","Active Client","Inactive Client"]
_rev={"Inactive Client"}
for _col in _hl:
    _ci=_hdr.index(_col); _vals=_mtd[_col].tolist()
    _nz=[v for v in _vals if v!=0]
    if not _nz: continue
    _hi,_lo=max(_nz),min(_nz)
    _gv,_rv=(_lo,_hi) if _col in _rev else (_hi,_lo)
    for _ri,_v in enumerate(_vals):
        _c=None
        if _v!=0 and _v==_gv: _c=_GREEN
        elif _v!=0 and _v==_rv and _hi!=_lo: _c=_RED
        if _c:
            _req.append({"repeatCell":{"range":{"sheetId":_sid,"startRowIndex":_DS+_ri,"endRowIndex":_DS+_ri+1,"startColumnIndex":_ci,"endColumnIndex":_ci+1},"cell":{"userEnteredFormat":{"backgroundColor":_c}},"fields":"userEnteredFormat.backgroundColor"}})

_req.append({"autoResizeDimensions":{"dimensions":{"sheetId":_sid,"dimension":"COLUMNS","startIndex":0,"endIndex":_nc}}})
_req.append({"updateDimensionProperties":{"range":{"sheetId":_sid,"dimension":"ROWS","startIndex":0,"endIndex":1},"properties":{"pixelSize":30},"fields":"pixelSize"}})
_req.append({"updateDimensionProperties":{"range":{"sheetId":_sid,"dimension":"ROWS","startIndex":_HR,"endIndex":_HR+1},"properties":{"pixelSize":42},"fields":"pixelSize"}})

if _req:
    sheet.batch_update({"requests":_req})
print(f"MTD_Performance tab banayi ({_m_start.strftime('%d-%m-%Y')} to {_y.strftime('%d-%m-%Y')})")