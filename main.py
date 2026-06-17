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