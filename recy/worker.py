"""Durable recovery runner. Collection inactivity is owned by firmware, never this worker."""
import logging
import threading
import time

from .app_service import AppService
from .backend import get_database


def refresh_alerts(service):
    with service.db.connect() as c:
        for query,kind,resource in (
            ("SELECT session_id AS id FROM app.session_credit_sla WHERE overdue AND credit_posted_at IS NULL",'credit_overdue','collection_session'),
            ("SELECT payout_id AS id FROM app.payout_processing_sla WHERE overdue AND status IN ('pending','processing','needs_review')",'payout_overdue','payout_request')):
            for row in c.execute(query).fetchall():
                c.execute('''INSERT INTO app.operational_alerts(kind,resource_type,resource_id,deduplication_key,severity)
                    VALUES (%s,%s,%s,%s,'critical') ON CONFLICT (deduplication_key) WHERE status<>'resolved'
                    DO UPDATE SET last_seen_at=now()''',(kind,resource,row['id'],kind+':'+str(row['id'])))
        c.execute("""UPDATE app.operational_alerts a SET status='resolved',resolved_at=now()
            WHERE a.kind='credit_overdue' AND a.status<>'resolved' AND EXISTS
            (SELECT 1 FROM app.collection_sessions s WHERE s.id=a.resource_id AND s.reconciliation_status IN ('posted','no_credit'))""")
        c.execute("""UPDATE app.operational_alerts a SET status='resolved',resolved_at=now()
            WHERE a.kind='payout_overdue' AND a.status<>'resolved' AND EXISTS
            (SELECT 1 FROM app.payout_requests p WHERE p.id=a.resource_id AND p.status IN ('paid','cancelled','rejected'))""")


def run_jobs(service,limit=10):
    handled=[]
    for _ in range(limit):
        with service.db.connect() as c:
            job=c.execute("""SELECT * FROM app.work_jobs WHERE
                (status IN ('pending','retry') AND available_at<=now()) OR
                (status='running' AND locked_until<now())
                ORDER BY priority DESC,available_at FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
            if not job:break
            c.execute("UPDATE app.work_jobs SET status='running',attempts=attempts+1,locked_until=now()+interval '2 minutes' WHERE id=%s",(job['id'],))
        result='completed'
        try:
            if job['kind']=='session_reconcile':
                service.reconcile(job['resource_id'])
                with service.db.connect() as c:
                    s=c.execute('SELECT reconciliation_status FROM app.collection_sessions WHERE id=%s',(job['resource_id'],)).fetchone()
                    if not s or s['reconciliation_status']=='needs_review':result='needs_review'
            elif job['kind']=='machine_auth_revoke':
                with service.db.connect() as c:
                    row=c.execute('SELECT * FROM app.machine_logins WHERE id=%s FOR NO KEY UPDATE',(job['resource_id'],)).fetchone()
                    if row and row['auth_revoked_before_collection']:
                        c.execute("UPDATE app.machine_logins SET logout_status='revoked',revoked_at=coalesce(revoked_at,now()) WHERE id=%s",(row['id'],))
                    else:result='needs_review'
            else:
                # Manual review required for job types whose external service is not configured.
                result='needs_review'
            with service.db.connect() as c:
                c.execute('UPDATE app.work_jobs SET status=%s,completed_at=CASE WHEN %s=\'completed\' THEN now() ELSE NULL END,locked_until=NULL WHERE id=%s',
                          (result,result,job['id']))
        except Exception as exc:
            # Keep source money records unchanged and leave a durable retry.
            logging.warning('Recovery job failed: %s',type(exc).__name__)
            with service.db.connect() as c:
                c.execute("UPDATE app.work_jobs SET status='retry',available_at=now()+interval '10 seconds',locked_until=NULL,last_error_code=%s WHERE id=%s",
                          (type(exc).__name__,job['id']))
            result='retry'
        handled.append({'job_id':job['id'],'status':result})
    refresh_alerts(service)
    return {'jobs':handled}


def run_forever(poll_seconds=5):
    while True:
        try:run_jobs(AppService(get_database()),20)
        except Exception as exc:logging.warning('Recovery loop unavailable: %s',type(exc).__name__)
        time.sleep(poll_seconds)


def start_background_worker():
    thread=threading.Thread(target=run_forever,name='recy-recovery',daemon=True)
    thread.start()
    return thread
