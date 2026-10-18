"""Shared per-account attachment reservations. All upload entrypoints must use this DB."""
import sqlite3,time,os
from contextlib import closing
from pathlib import Path

def reserve(db,account,event_id,count,threshold=80,now=None):
    now=time.time() if now is None else now
    if count<1:raise ValueError('Attachment count must be positive')
    db=Path(db);db.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(str(db),timeout=15)) as con:
        con.execute('CREATE TABLE IF NOT EXISTS uploads(account TEXT,event_id TEXT,count INTEGER,at REAL,status TEXT,PRIMARY KEY(account,event_id))')
        con.execute('BEGIN IMMEDIATE')
        old=con.execute('SELECT count,at,status FROM uploads WHERE account=? AND event_id=?',(account,event_id)).fetchone()
        if old:
            con.commit()
            return {'allowed':False,'reason':'existing_attempt_reconcile','count':old[0],'at':old[1],'status':old[2]}
        # Retain entries for 180 minutes plus a conservative two-minute buffer.
        rows=con.execute('SELECT count,at FROM uploads WHERE account=? AND at>? ORDER BY at',(account,now-10920)).fetchall()
        used=sum(x[0] for x in rows)
        if count>threshold:
            con.commit()
            return {'allowed':False,'reason':'whole_set_exceeds_threshold','used':used,'nextCheckAt':None}
        if used+count>threshold:
            remaining=used;release=None
            for n,at in rows:
                remaining-=n
                if remaining+count<=threshold:release=at+10920;break
            con.commit()
            return {'allowed':False,'reason':'rolling_budget_wait','used':used,'nextCheckAt':release}
        con.execute('INSERT INTO uploads VALUES(?,?,?,?,?)',(account,event_id,count,now,'reserved'))
        con.commit()
        return {'allowed':True,'usedBefore':used,'reserved':count,'remaining':threshold-used-count}

def mark(db,account,event_id,status):
    with closing(sqlite3.connect(str(db),timeout=15)) as con:
        # Failed/unknown attempts remain counted conservatively.
        con.execute('UPDATE uploads SET status=? WHERE account=? AND event_id=?',(status,account,event_id))
        con.commit()

def get_event(db,account,event_id):
    db=Path(db)
    if not db.exists():return None
    with closing(sqlite3.connect(str(db),timeout=15)) as con:
        row=con.execute('SELECT count,at,status FROM uploads WHERE account=? AND event_id=?',(account,event_id)).fetchone()
        return None if row is None else {'count':row[0],'at':row[1],'status':row[2]}

def default_db():
    base=Path(os.environ.get('SPOKEN_PRODUCTION_STATE_ROOT',str(Path.home()/'.local/share/spoken-production-line')))
    return base/'account-upload-ledger.sqlite3'
