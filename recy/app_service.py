"""Transactional application operations. Auth is verified before entering this layer."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from uuid import uuid4

from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation
from psycopg.types.json import Jsonb

from .database import normalize_upc, quote
from .http_support import APIError, digest, email, identifier, integer, require, text, timestamp, utcnow
from .source import upc_column


class AppService:
    def __init__(self, database):
        require(hasattr(database, 'migrate'), 'Application APIs require PostgreSQL', 503, 'database_not_configured')
        self.db = database

    @staticmethod
    def audit(c, actor_type, actor_id, action, resource, resource_id, details=None):
        c.execute('INSERT INTO app.audit_events(actor_type,actor_id,action,resource_type,resource_id,redacted_details) VALUES (%s,%s,%s,%s,%s,%s)',
                  (actor_type,actor_id,action,resource,resource_id,Jsonb(details or {})))

    @staticmethod
    def wallet(c, user_id, lock=False):
        row = c.execute('SELECT * FROM app.wallets WHERE user_id=%s AND currency=\'CAD\'' +
                        (' FOR NO KEY UPDATE' if lock else ''), (user_id,)).fetchone()
        require(row is not None, 'Wallet is not provisioned', 409, 'wallet_unavailable')
        return row

    @staticmethod
    def balances(c, wallet_id):
        return c.execute('SELECT * FROM app.wallet_balances WHERE wallet_id=%s', (wallet_id,)).fetchone()

    @staticmethod
    def posting(c, kind, key, entries, session_id=None, payout_id=None):
        return c.execute('SELECT app.post_journal(%s,%s,%s,%s,%s) AS id',
                         (kind,key,Jsonb([{'account_id':str(a),'amount_cents':v} for a,v in entries]),session_id,payout_id)).fetchone()['id']

    def profile(self, user):
        auth_id = identifier(user.get('id'), 'user')
        require(user.get('email_confirmed_at') or user.get('phone_confirmed_at'),
                'Confirm your email or phone before continuing', 403, 'contact_unverified')
        with self.db.connect() as c:
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('profile:'+str(auth_id),))
            row = c.execute('SELECT * FROM app.profiles WHERE auth_user_id=%s', (auth_id,)).fetchone()
            if row is None:
                metadata = user.get('user_metadata') or {}
                first,last = text(metadata.get('first_name'), 'first_name', 100),text(metadata.get('last_name'), 'last_name', 100)
                row = c.execute('INSERT INTO app.profiles(auth_user_id,first_name,last_name) VALUES (%s,%s,%s) RETURNING *',
                                (auth_id,first,last)).fetchone()
            require(row['status']=='active', 'Account is unavailable', 403, 'account_disabled')
            if not c.execute('SELECT id FROM app.wallets WHERE user_id=%s', (row['id'],)).fetchone():
                w,a,r = uuid4(),uuid4(),uuid4()
                c.execute('INSERT INTO app.wallets(id,user_id,available_account_id,reserved_account_id) VALUES (%s,%s,%s,%s)', (w,row['id'],a,r))
                c.execute("INSERT INTO app.ledger_accounts(id,wallet_id,purpose) VALUES (%s,%s,'user_available'),(%s,%s,'user_reserved')", (a,w,r,w))
            row['full_name'] = row['first_name']+' '+row['last_name']
            return row

    def user_principal(self, auth, token, allow_machine=False, recent=False):
        user,auth_id,sid = auth.verify_user(token)
        with self.db.connect() as c:
            session = c.execute('SELECT created_at FROM auth.sessions WHERE id=%s AND user_id=%s', (sid,auth_id)).fetchone()
            require(session is not None, 'Session has ended; sign in again', 401, 'session_ended')
            bound = c.execute('SELECT id FROM app.machine_logins WHERE supabase_auth_session_id=%s', (sid,)).fetchone()
            require(allow_machine or not bound, 'Machine login cannot access user account operations', 403, 'machine_scope')
            if recent:
                require(utcnow()-session['created_at'] <= timedelta(minutes=15),
                        'Sign in again before this action', 401, 'reauthentication_required')
        return self.profile(user), user, sid

    def staff(self, profile, administrator=False):
        with self.db.connect() as c:
            row=c.execute('SELECT role FROM app.staff_roles WHERE user_id=%s AND revoked_at IS NULL' +
                          (" AND role='administrator'" if administrator else " AND role IN ('administrator','payout_processor')"), (profile['id'],)).fetchone()
        require(row is not None, 'Staff authorization required', 403, 'forbidden')

    def rate_limit(self, identity, remote, operation):
        # Persistent per-identity and per-peer budgets; raw email/IP is not stored.
        limited=False
        with self.db.connect() as c:
            c.execute('DELETE FROM app.auth_rate_limits WHERE expires_at<now()')
            for bucket,limit in ((operation+':identity:'+identity.casefold(),10), (operation+':peer:'+remote,60)):
                row=c.execute("""INSERT INTO app.auth_rate_limits(bucket,window_start,requests,expires_at)
                    VALUES (%s,date_trunc('minute',now()),1,date_trunc('minute',now())+interval '2 minutes')
                    ON CONFLICT (bucket,window_start) DO UPDATE SET requests=app.auth_rate_limits.requests+1
                    RETURNING requests""", (hashlib.sha256(bucket.encode()).hexdigest(),)).fetchone()
                limited = limited or row['requests']>limit
        require(not limited, 'Too many requests; try again shortly', 429, 'rate_limited')

    def machine(self, credential):
        require(isinstance(credential,str) and '.' in credential, 'Machine credential required', 401, 'machine_unauthorized')
        prefix,secret=credential.split('.',1)
        require(len(prefix)<=64 and 20<=len(secret)<=150, 'Invalid machine credential', 401, 'machine_unauthorized')
        with self.db.connect() as c:
            row=c.execute('''SELECT k.*,m.status AS machine_status,m.operating_mode FROM app.machine_credentials k
                JOIN app.machines m ON m.id=k.machine_id WHERE k.key_prefix=%s''', (prefix,)).fetchone()
            valid=row and row['revoked_at'] is None and (row['expires_at'] is None or row['expires_at']>utcnow())
            valid=valid and hmac.compare_digest(row['secret_hash'],hashlib.sha256(secret.encode()).hexdigest())
            require(valid and row['machine_status']!='disabled', 'Invalid machine credential', 401, 'machine_unauthorized')
            c.execute('UPDATE app.machine_credentials SET last_used_at=now() WHERE id=%s',(row['id'],))
            return row

    @staticmethod
    def session(c, session_id, machine_id=None, user_id=None):
        row=c.execute('SELECT * FROM app.collection_sessions WHERE id=%s FOR NO KEY UPDATE', (identifier(session_id,'session_id'),)).fetchone()
        require(row and (machine_id is None or row['machine_id']==machine_id) and
                (user_id is None or row['user_id']==user_id), 'Session not found', 404, 'not_found')
        return row

    @staticmethod
    def replay(c, principal_type, principal_id, operation, key, body):
        key=text(key,'Idempotency-Key',150)
        scope=':'.join((principal_type,str(principal_id),operation,key))
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(scope,))
        old=c.execute('''SELECT * FROM app.idempotency_requests WHERE principal_type=%s AND principal_id=%s
            AND operation=%s AND key=%s''', (principal_type,principal_id,operation,key)).fetchone()
        require(not old or old['request_hash']==digest(body), 'Idempotency key was used with different data',409,'idempotency_conflict')
        return old['resource_id'] if old else None

    @staticmethod
    def remember(c, kind, principal, operation, key, body, resource, resource_id):
        c.execute('''INSERT INTO app.idempotency_requests(principal_type,principal_id,operation,key,request_hash,
            resource_type,resource_id,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,now()+interval '30 days')''',
                  (kind,principal,operation,key,digest(body),resource,resource_id))

    def start_session(self, profile, machine, auth_session_id, key):
        body={'user_id':str(profile['id'])}
        with self.db.connect() as c:
            old=self.replay(c,'machine',machine['machine_id'],'start_session',key,body)
            if old:return self.session(c,old,machine_id=machine['machine_id'])
            require(machine['machine_status']=='active' and machine['operating_mode']=='live', 'Machine is not accepting bottles',409,'machine_unavailable')
            require(c.execute('SELECT fill_status FROM app.machine_capacity WHERE id=%s',(machine['machine_id'],)).fetchone()['fill_status']!='full',
                    'Machine is full',409,'machine_full')
            login=c.execute('''INSERT INTO app.machine_logins(user_id,machine_id,supabase_auth_session_id,auth_revoked_before_collection)
                VALUES (%s,%s,%s,true) RETURNING id''',(profile['id'],machine['machine_id'],auth_session_id)).fetchone()['id']
            rate=c.execute("SELECT * FROM app.reward_rates WHERE effective_from<=now() AND (effective_until IS NULL OR effective_until>now())").fetchone()
            require(rate is not None,'Reward rate unavailable',503,'rate_unavailable')
            row=c.execute('''INSERT INTO app.collection_sessions(user_id,machine_id,machine_login_id,reward_rate_id)
                VALUES (%s,%s,%s,%s) RETURNING *''',(profile['id'],machine['machine_id'],login,rate['id'])).fetchone()
            self.remember(c,'machine',machine['machine_id'],'start_session',key,body,'collection_session',row['id'])
            self.audit(c,'machine',machine['machine_id'],'session_started','collection_session',row['id'])
            return row

    @staticmethod
    def registry_lookup(c, code):
        latest=c.execute('SELECT * FROM recy.sync_runs ORDER BY id DESC LIMIT 1').fetchone()
        require(latest is not None,'Registry has not been published',503,'registry_unavailable')
        fresh=utcnow()-datetime.fromisoformat(latest['completed_at'])<=timedelta(days=8)
        if not fresh and c.execute("SELECT to_regclass('recy.source_monitor') AS name").fetchone()['name']:
            monitor=c.execute('SELECT * FROM recy.source_monitor WHERE id=1').fetchone()
            if monitor and utcnow()-monitor['last_checked_at']<=timedelta(days=2):
                from .refresh import imported_date
                baseline=imported_date(latest)
                known=latest.get('source_updated_on')
                fresh=(monitor['observed_source_date']==baseline if known else monitor['observed_source_date']<=baseline)
        require(fresh,'Registry requires a fresh publication or an up-to-date source check',503,'registry_unavailable')
        column=upc_column(json.loads(latest['headers']))
        match=c.execute('SELECT 1 FROM recy.registered_products WHERE recy.normalize_upc(%s)=%%s LIMIT 1' % quote(column),(code,)).fetchone()
        return bool(match),latest['id']

    def add_bottle(self, machine, session_id, body):
        event=identifier(body.get('machine_event_id'),'machine_event_id')
        seq=integer(body.get('sequence_no'),'sequence_no',1,100000)
        try:code=normalize_upc(body.get('upc'))
        except ValueError:raise APIError(400,'invalid_upc','UPC must be a text barcode with 4–18 digits') from None
        observed=timestamp(body.get('observed_at'),'observed_at')
        payload={'machine_event_id':str(event),'sequence_no':seq,'upc':code,'observed_at':observed.isoformat()}
        with self.db.connect() as c:
            s=self.session(c,session_id,machine_id=machine['machine_id'])
            old=c.execute('SELECT * FROM app.bottle_events WHERE machine_id=%s AND machine_event_id=%s',(machine['machine_id'],event)).fetchone()
            if old:
                require(old['session_id']==s['id'] and old['payload_hash']==digest(payload),'Bottle event retry conflicts with original',409,'event_conflict')
                return old
            require(machine['machine_status']=='active' and machine['operating_mode']=='live',
                    'Machine is not accepting new bottles',409,'machine_unavailable')
            require(s['collection_status']=='active','Session is closed',409,'session_closed')
            require(c.execute('SELECT fill_status FROM app.machine_capacity WHERE id=%s',(machine['machine_id'],)).fetchone()['fill_status']!='full',
                    'Machine is full; return the bottle',409,'machine_full')
            try:eligible,run=self.registry_lookup(c,code)
            except APIError as exc:
                if exc.code!='registry_unavailable':raise
                eligible,run=None,None
            row=c.execute('''INSERT INTO app.bottle_events(machine_event_id,session_id,user_id,machine_id,sequence_no,upc,
                eligibility_status,status,reason_code,registry_sync_run_id,observed_at,payload_hash,decided_at,backend_acknowledged_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s THEN NULL ELSE now() END,now()) RETURNING *''',
                (event,s['id'],s['user_id'],s['machine_id'],seq,code,'eligible' if eligible else 'unavailable' if eligible is None else 'ineligible',
                 'pending' if eligible else 'rejected','registered' if eligible else 'registry_unavailable' if eligible is None else 'not_registered',
                 run,observed,digest(payload),bool(eligible))).fetchone()
            c.execute('UPDATE app.collection_sessions SET last_scan_at=greatest(last_scan_at,%s) WHERE id=%s',(observed,s['id']))
            return row

    @staticmethod
    def apply_capture(c, row, body):
        outcome=body.get('status')
        require(outcome in ('accepted','rejected'),'status must be accepted or rejected')
        if row['status']=='accepted' and outcome=='accepted' and 'capture_event_id' not in body and 'captured_at' not in body:
            return row  # The physical evidence was already durably recorded by the capture endpoint.
        capture=identifier(body.get('capture_event_id'),'capture_event_id') if outcome=='accepted' else None
        captured=timestamp(body.get('captured_at'),'captured_at') if outcome=='accepted' else None
        require(outcome!='accepted' or row['eligibility_status']=='eligible','Unregistered bottle cannot be accepted',409,'ineligible_bottle')
        evidence=digest({'status':outcome,'capture_event_id':str(capture) if capture else None,
                         'captured_at':captured.isoformat() if captured else None})
        if row['status']!='pending':
            require(row['status']==outcome and (outcome=='rejected' or row['capture_evidence_hash']==evidence),
                    'Capture outcome conflicts with original',409,'capture_conflict')
            return row
        reason='captured' if outcome=='accepted' else text(body.get('reason_code','capture_failed'),'reason_code',100)
        return c.execute('''UPDATE app.bottle_events SET status=%s,capture_event_id=%s,captured_at=%s,
            decided_at=now(),reward_cents=%s,reason_code=%s,capture_evidence_hash=%s WHERE id=%s RETURNING *''',
                         (outcome,capture,captured,7 if outcome=='accepted' else 0,reason,evidence,row['id'])).fetchone()

    def capture(self,machine,session_id,event_id,body):
        with self.db.connect() as c:
            s=self.session(c,session_id,machine_id=machine['machine_id'])
            row=c.execute('SELECT * FROM app.bottle_events WHERE session_id=%s AND machine_event_id=%s FOR UPDATE',
                          (s['id'],identifier(event_id,'machine_event_id'))).fetchone()
            require(row is not None,'Bottle attempt not found',404,'not_found')
            return self.apply_capture(c,row,body)

    def submit(self,machine,session_id,body,key):
        bottles=body.get('bottles')
        require(isinstance(bottles,list) and len(bottles)<=5000,'bottles must be a list of at most 5000 attempts')
        final=integer(body.get('final_sequence_no'),'final_sequence_no',0,5000)
        require(len(bottles)==final,'Final sequence number must match bottle count')
        ids=[identifier(x.get('machine_event_id'),'machine_event_id') for x in bottles if isinstance(x,dict)]
        require(len(ids)==len(bottles) and len(set(ids))==len(ids),'Bottle event IDs must be unique')
        require(sorted(integer(x.get('sequence_no'),'sequence_no',1,5000) for x in bottles)==list(range(1,final+1)),
                'Bottle sequence must cover every attempt from 1 to final_sequence_no')
        require(all(x.get('status') in ('accepted','rejected') for x in bottles),'Every bottle must claim accepted or rejected')
        end_event=identifier(body.get('machine_end_event_id'),'machine_end_event_id')
        reason=body.get('end_reason','inactivity')
        require(reason in ('inactivity','machine_fault','restart','admin_recovery'),'Invalid end_reason')
        machine_end=timestamp(body['machine_ended_at'],'machine_ended_at') if body.get('machine_ended_at') else None
        request_hash=digest(body)
        with self.db.connect() as c:
            s=self.session(c,session_id,machine_id=machine['machine_id'])
            prior=c.execute('SELECT * FROM app.session_submissions WHERE session_id=%s',(s['id'],)).fetchone()
            if prior:
                require(prior['request_hash']==request_hash and prior['idempotency_key']==key,
                        'Final session dictionary conflicts with original',409,'submission_conflict')
            else:
                require(s['collection_status']=='active','Session is closed',409,'session_closed')
                recorded=c.execute('SELECT * FROM app.bottle_events WHERE session_id=%s ORDER BY sequence_no FOR UPDATE',(s['id'],)).fetchall()
                by_event={r['machine_event_id']:r for r in recorded}
                issue=None
                if set(by_event)!=set(ids):issue='manifest_missing_or_extra_attempts'
                for claimed in bottles:
                    row=by_event.get(identifier(claimed['machine_event_id']))
                    if row is None:continue
                    if claimed['sequence_no']!=row['sequence_no'] or claimed.get('upc')!=row['upc']:
                        issue='manifest_identity_mismatch';continue
                    # A final dictionary can carry an outstanding physical-capture acknowledgement.
                    try:
                        with c.transaction():  # Savepoint: rejected evidence must not erase the end signal.
                            self.apply_capture(c,row,claimed)
                    except (APIError,CheckViolation,ForeignKeyViolation,UniqueViolation):
                        issue='capture_evidence_mismatch'
                c.execute('''INSERT INTO app.session_submissions(session_id,machine_id,idempotency_key,request_hash,
                    claimed_accepted_count,claimed_rejected_count,final_sequence_no,machine_end_event_id,end_reason,
                    machine_ended_at,expected_attempt_count,manifest_hash,sealed_machine_event_ids,status,validated_at,review_reason,last_scan_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s THEN NULL ELSE now() END,%s,%s)''',
                          (s['id'],s['machine_id'],key,request_hash,sum(x['status']=='accepted' for x in bottles),
                           sum(x['status']=='rejected' for x in bottles),final,end_event,reason,machine_end,final,
                           digest([str(x) for x in ids]),ids,'needs_review' if issue else 'validated',bool(issue),issue,
                           max((r['observed_at'] for r in recorded),default=None)))
                if issue:
                    c.execute("UPDATE app.collection_sessions SET reconciliation_status='needs_review',last_error_code=%s WHERE id=%s",(issue,s['id']))
                    c.execute("INSERT INTO app.machine_incidents(machine_id,session_id,kind,redacted_details) VALUES (%s,%s,'capture_uncertain',%s)",
                              (s['machine_id'],s['id'],Jsonb({'reason':issue})))
                c.execute("UPDATE app.machine_logins SET logout_status='revoked',revoked_at=now() WHERE id=%s AND auth_revoked_before_collection",(s['machine_login_id'],))
                c.execute("""UPDATE app.work_jobs SET status='completed',completed_at=now()
                    WHERE kind='machine_auth_revoke' AND resource_id=%s AND EXISTS
                    (SELECT 1 FROM app.machine_logins WHERE id=%s AND auth_revoked_before_collection)""",
                          (s['machine_login_id'],s['machine_login_id']))
                self.audit(c,'machine',s['machine_id'],'session_submitted','collection_session',s['id'])
        self.reconcile(s['id'])
        with self.db.connect() as c:
            result=self.session(c,s['id'],machine_id=machine['machine_id'])
            result['user_logged_out']=True
            return result

    def reconcile(self,session_id):
        with self.db.connect() as c:
            s=self.session(c,session_id)
            if s['reconciliation_status'] in ('posted','no_credit','needs_review'):return s
            submission=c.execute('SELECT * FROM app.session_submissions WHERE session_id=%s',(s['id'],)).fetchone()
            require(submission and submission['status']=='validated','Session is not ready for reconciliation',409,'not_ready')
            events=c.execute('SELECT * FROM app.bottle_events WHERE session_id=%s ORDER BY sequence_no',(s['id'],)).fetchall()
            valid=(len(events)==submission['expected_attempt_count'] and all(e['status']!='pending' for e in events) and
                   [e['sequence_no'] for e in events]==list(range(1,len(events)+1)))
            if not valid:
                c.execute("UPDATE app.collection_sessions SET reconciliation_status='needs_review',last_error_code='incomplete_events' WHERE id=%s",(s['id'],))
                return
            accepted=sum(e['status']=='accepted' for e in events)
            amount=accepted*s['reward_per_bottle_cents']
            tx=None
            if amount:
                w=self.wallet(c,s['user_id'],True)
                platform=c.execute("SELECT id FROM app.ledger_accounts WHERE purpose='platform_rewards' AND currency='CAD'").fetchone()['id']
                tx=self.posting(c,'session_credit','session:'+str(s['id']),[(w['available_account_id'],amount),(platform,-amount)],session_id=s['id'])
            c.execute('''UPDATE app.collection_sessions SET reconciliation_status=%s,accepted_count=%s,rejected_count=%s,
                reward_total_cents=%s,credit_transaction_id=%s,credit_posted_at=CASE WHEN %s THEN now() ELSE NULL END,
                reconciled_at=now(),reconciliation_attempts=reconciliation_attempts+1 WHERE id=%s''',
                      ('posted' if amount else 'no_credit',accepted,len(events)-accepted,amount,tx,bool(amount),s['id']))
            c.execute("UPDATE app.work_jobs SET status='completed',completed_at=now() WHERE kind='session_reconcile' AND resource_id=%s",(s['id'],))
            c.execute("""INSERT INTO app.notifications(user_id,kind,resource_type,resource_id,channel,deduplication_key)
                VALUES (%s,'session_reconciled','collection_session',%s,'in_app',%s) ON CONFLICT (deduplication_key) DO NOTHING""",
                      (s['user_id'],s['id'],'session_credit:'+str(s['id'])))
            self.audit(c,'system',None,'session_reconciled','collection_session',s['id'],{'credit_cents':amount})

    def payout(self,user_id,body,key):
        amount=integer(body.get('amount_cents'),'amount_cents',500)
        destination=email(body.get('email'))
        normalized={'amount_cents':amount,'email':destination}
        with self.db.connect() as c:
            old=self.replay(c,'user',user_id,'payout',key,normalized)
            if old:return c.execute('SELECT * FROM app.payout_requests WHERE id=%s',(old,)).fetchone()
            w=self.wallet(c,user_id,True)
            require(w['status']=='active','Wallet is unavailable',409,'wallet_frozen')
            require(self.balances(c,w['id'])['available_cents']>=amount,'Insufficient available funds',409,'insufficient_funds')
            p=c.execute('''INSERT INTO app.payout_requests(user_id,wallet_id,destination_email,amount_cents,
                request_idempotency_key,request_hash) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *''',
                        (user_id,w['id'],destination,amount,key,digest(normalized))).fetchone()
            tx=self.posting(c,'payout_reserve','reserve:'+str(p['id']),[(w['reserved_account_id'],amount),(w['available_account_id'],-amount)],payout_id=p['id'])
            p=c.execute('UPDATE app.payout_requests SET reserve_transaction_id=%s WHERE id=%s RETURNING *',(tx,p['id'])).fetchone()
            self.remember(c,'user',user_id,'payout',key,normalized,'payout_request',p['id'])
            self.audit(c,'user',user_id,'payout_requested','payout_request',p['id'])
            c.execute("""INSERT INTO app.notifications(user_id,kind,resource_type,resource_id,channel,deduplication_key)
                VALUES (%s,'payout_requested','payout_request',%s,'in_app',%s)""",(user_id,p['id'],'payout_requested:'+str(p['id'])))
            return p

    def payout_action(self,profile,payout_id,action,body,staff=False):
        with self.db.connect() as c:
            p=c.execute('SELECT * FROM app.payout_requests WHERE id=%s FOR UPDATE',(identifier(payout_id),)).fetchone()
            require(p and (staff or p['user_id']==profile['id']),'Payout not found',404,'not_found')
            if action=='claim':
                if p['status']=='processing' and p['assigned_to']==profile['id']:return p
                require(p['status']=='pending','Payout is already claimed or resolved',409,'payout_conflict')
                result=c.execute("UPDATE app.payout_requests SET status='processing',assigned_to=%s,processing_started_at=now() WHERE id=%s RETURNING *",(profile['id'],p['id'])).fetchone()
                self.audit(c,'staff',profile['id'],'payout_claimed','payout_request',p['id'])
                return result
            if action=='review':
                require(p['status'] in ('pending','processing','needs_review'),'Payout is resolved',409,'payout_conflict')
                return c.execute("UPDATE app.payout_requests SET status='needs_review',staff_note=%s WHERE id=%s RETURNING *",(text(body.get('reason'),'reason',1000),p['id'])).fetchone()
            target={'cancel':'cancelled','reject':'rejected','complete':'paid'}.get(action)
            require(target is not None,'Unknown payout action',404,'not_found')
            if p['status']==target:
                if target=='paid':require(p['payment_reference']==body.get('payment_reference'),'Payment reference differs',409,'payout_conflict')
                return p
            if action=='cancel':require(p['status']=='pending','Only pending payouts can be cancelled',409,'payout_conflict')
            elif action=='complete':require(p['status'] in ('processing','needs_review') and p['assigned_to']==profile['id'],
                                           'Claim this payout before completing it',409,'payout_conflict')
            else:
                require(p['status'] in ('pending','processing','needs_review'),'Payout is resolved',409,'payout_conflict')
                require(p['assigned_to'] in (None,profile['id']),
                        'Only the assigned operator can reject a claimed payout',409,'payout_conflict')
                require(body.get('confirmed_unpaid') is True,'Confirm that no transfer was sent before rejecting')
            w=self.wallet(c,p['user_id'],True)
            reference=text(body.get('payment_reference'),'payment_reference',200) if action=='complete' else None
            paid=timestamp(body.get('paid_at'),'paid_at') if action=='complete' else None
            if paid:require(p['requested_at']<=paid<=utcnow()+timedelta(minutes=1),'paid_at must be between request time and now')
            reason=text(body.get('reason'),'reason',1000) if action=='reject' else None
            account=self.wallet(c,p['user_id'])['available_account_id']
            if action=='complete':account=c.execute("SELECT id FROM app.ledger_accounts WHERE purpose='platform_payout_clearing'").fetchone()['id']
            tx=self.posting(c,'payout_settle' if action=='complete' else 'payout_release',
                            action+':'+str(p['id']),[(account,p['amount_cents']),(w['reserved_account_id'],-p['amount_cents'])],payout_id=p['id'])
            result=c.execute('''UPDATE app.payout_requests SET status=%s,processed_by=%s,payment_reference=%s,
                paid_at=%s,completed_at=now(),rejection_reason=%s,settle_transaction_id=%s,release_transaction_id=%s
                WHERE id=%s RETURNING *''',(target,profile['id'],reference,paid,reason,
                    tx if action=='complete' else None,tx if action!='complete' else None,p['id'])).fetchone()
            self.audit(c,'staff' if staff else 'user',profile['id'],'payout_'+target,'payout_request',p['id'])
            c.execute("""INSERT INTO app.notifications(user_id,kind,resource_type,resource_id,channel,deduplication_key)
                VALUES (%s,%s,'payout_request',%s,'in_app',%s) ON CONFLICT (deduplication_key) DO NOTHING""",
                      (p['user_id'],'payout_'+target,p['id'],'payout_'+target+':'+str(p['id'])))
            return result
