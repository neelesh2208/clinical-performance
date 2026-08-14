import requests, traceback
from datetime import date, datetime
def _parse_dt(s):
    if not s: return None
    try: return datetime.fromisoformat(str(s).replace('Z','+00:00')).date()
    except: return None
def _flatten(val):
    if val is None or val=='' or val==[]: return ''
    if isinstance(val,(str,int,float)): return str(val)
    if isinstance(val,dict):
        for k in ('name','title','packageName','label'):
            if val.get(k): return str(val[k])
        return ', '.join(str(v) for v in val.values() if v not in (None,'',[]))
    if isinstance(val,list):
        return ', '.join(p for p in (_flatten(i) for i in val) if p)
    return str(val)
url='https://api-v2-report.emoneeds.com/api/v1/reports/patient-reports/master-data'
try:
    payload = requests.get(url, params={'page':1,'limit':500}, timeout=90).json()
    print('payload type:', type(payload).__name__)
    rows = payload.get('data', [])
    print('rows:', len(rows))
    for i, r in enumerate(rows):
        _parse_dt(r.get('createdAt'))
        _flatten(r.get('package'))
        _flatten(r.get('assignments'))
        r.get('leadSource',''); r.get('monthsWithUs','')
    print('ALL OK - koi crash nahi page 1 par')
except Exception:
    traceback.print_exc()
