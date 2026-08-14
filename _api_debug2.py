import requests, traceback
from datetime import date, datetime
url = "https://api-v2-report.emoneeds.com/api/v1/reports/patient-reports/master-data"
def _parse_dt(s):
    if not s: return None
    try: return datetime.fromisoformat(str(s).replace('Z','+00:00')).date()
    except: return None
try:
    resp = requests.get(url, params={'page':1,'limit':5}, timeout=60)
    payload = resp.json()
    rows = payload.get('data', [])
    print('rows type:', type(rows).__name__, 'len:', len(rows))
    print('first row type:', type(rows[0]).__name__)
    for r in rows:
        print('  item type:', type(r).__name__, '| createdAt:', r.get('createdAt') if isinstance(r,dict) else 'NOT DICT')
    meta = payload.get('meta', {})
    print('meta hasNextPage:', meta.get('hasNextPage'))
except Exception as e:
    print('CRASH:'); traceback.print_exc()
