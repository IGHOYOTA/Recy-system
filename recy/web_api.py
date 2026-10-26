"""Versioned JSON API router for users, machines, and back-office staff."""
import json
from uuid import uuid4

from psycopg.types.json import Jsonb

from .app_service import AppService
from .auth import SupabaseAuth
from .http_support import APIError, digest, email, identifier, integer, phone, require, text, timestamp


def bearer(environ):
    value=environ.get('HTTP_AUTHORIZATION','')
    require(value.startswith('Bearer ') and len(value)<=10000,'Bearer authentication required',401,'unauthorized')
    return value[7:]


def idempotency(environ):
    return text(environ.get('HTTP_IDEMPOTENCY_KEY'),'Idempotency-Key',150)


def password(value):
    require(isinstance(value,str) and 8<=len(value)<=1024,'Password must contain 8–1024 characters')
    return value


class ApplicationAPI:
    def __init__(self,database,auth=None):
        self.service=AppService(database)
        self._auth=auth

    @property
    def auth(self):
        if self._auth is None:self._auth=SupabaseAuth()
        return self._auth

    def principal(self,environ,recent=False,administrator=False,staff=False):
        profile,user,sid=self.service.user_principal(self.auth,bearer(environ),recent=recent)
        if staff or administrator:self.service.staff(profile,administrator)
        return profile,user,sid

    def dispatch(self,environ,body):
        method=environ['REQUEST_METHOD']
        path=environ['PATH_INFO']
        path=path[len('/api/v1'):] if path.startswith('/api/v1/') else path
        parts=[p for p in path.split('/') if p]
        require(parts,'Route not found',404,'not_found')
        if parts[0]=='auth':return self.auth_route(method,parts[1:],environ,body)
        if parts[0]=='admin':return self.admin_route(method,parts[1:],environ,body)
        if parts[0]=='machines' and method=='POST':
            machine=self.service.machine(environ.get('HTTP_X_MACHINE_KEY'))
            if parts==['machines','heartbeat']:
                with self.service.db.connect() as c:
                    c.execute('UPDATE app.machines SET last_seen_at=now(),last_heartbeat_at=now(),firmware_version=coalesce(%s,firmware_version) WHERE id=%s',
                              (text(body['firmware_version'],'firmware_version',100) if 'firmware_version' in body else None,machine['machine_id']))
                    capacity=c.execute('SELECT capacity_cycle_id,accepted_since_evacuation,fill_status FROM app.machine_capacity WHERE id=%s',(machine['machine_id'],)).fetchone()
                return 200,dict(capacity,status='ok')
            if parts==['machines','fill-reports']:
                from .operations import OperationsAPI
                return OperationsAPI(self).fill_report(machine,body)
            if parts==['machines','sync-batches']:return self.sync_batch(machine,body)
        if parts[0]=='collection-sessions':return self.collection_route(method,parts[1:],environ,body)
        profile,user,sid=self.principal(environ,recent=(method!='GET' and parts[0]=='payouts'))
        if parts==['me','summary'] and method=='GET':
            with self.service.db.connect() as c:
                return 200,c.execute('''SELECT
                    (SELECT count(*) FROM app.bottle_events WHERE user_id=%s AND status='accepted') AS accepted_bottles,
                    (SELECT count(*) FROM app.collection_sessions WHERE user_id=%s AND ended_at IS NOT NULL) AS completed_sessions,
                    (SELECT coalesce(sum(amount_cents),0) FROM app.payout_requests WHERE user_id=%s AND status='paid') AS paid_withdrawals_cents''',
                    (profile['id'],profile['id'],profile['id'])).fetchone()
        if parts==['me']:
            if method=='GET':
                from .operations import OperationsAPI
                return 200,{'user':profile,'email':user.get('email'),'phone':user.get('phone'),'roles':OperationsAPI(self).roles(profile['id'])}
            if method=='PATCH':
                first=text(body.get('first_name'),'first_name',100);last=text(body.get('last_name'),'last_name',100)
                with self.service.db.connect() as c:
                    row=c.execute('UPDATE app.profiles SET first_name=%s,last_name=%s WHERE id=%s RETURNING *',(first,last,profile['id'])).fetchone()
                row['full_name']=first+' '+last
                return 200,{'user':row}
        if parts==['wallet'] and method=='GET':
            with self.service.db.connect() as c:
                w=self.service.wallet(c,profile['id'])
                return 200,self.service.balances(c,w['id'])
        if parts==['wallet','transactions'] and method=='GET':
            limit,offset=self.pagination(environ)
            with self.service.db.connect() as c:
                return 200,{'items':c.execute('''SELECT t.id,t.kind,t.created_at,t.session_id,t.payout_id,
                    e.amount_cents,a.purpose FROM app.ledger_entries e JOIN app.ledger_accounts a ON a.id=e.account_id
                    JOIN app.wallets w ON w.id=a.wallet_id JOIN app.ledger_transactions t ON t.id=e.transaction_id
                    WHERE w.user_id=%s ORDER BY t.created_at DESC,t.id,a.purpose LIMIT %s OFFSET %s''',(profile['id'],limit,offset)).fetchall(),
                    'limit':limit,'offset':offset}
        if parts==['wallet','activity'] and method=='GET':
            from .listing import filters
            limit,offset=self.pagination(environ)
            where,args=filters(environ,'created_at','kind')
            with self.service.db.connect() as c:
                return 200,{'items':c.execute('''WITH activity AS (
                    SELECT t.id,t.kind,t.posted_at AS created_at,t.session_id,t.payout_id,
                      coalesce(sum(e.amount_cents) FILTER (WHERE a.purpose='user_available'),0) AS available_delta_cents,
                      coalesce(sum(e.amount_cents) FILTER (WHERE a.purpose='user_reserved'),0) AS reserved_delta_cents,
                      (SELECT r.session_id FROM app.deposit_reviews r JOIN app.wallet_adjustments j ON j.id=r.adjustment_id
                       WHERE j.ledger_transaction_id=t.id) AS review_session_id
                    FROM app.ledger_entries e JOIN app.ledger_accounts a ON a.id=e.account_id
                    JOIN app.wallets w ON w.id=a.wallet_id JOIN app.ledger_transactions t ON t.id=e.transaction_id
                    WHERE w.user_id=%s GROUP BY t.id), balances AS (
                      SELECT *,sum(available_delta_cents) OVER (ORDER BY created_at,id) AS available_after_cents FROM activity)
                    SELECT id,kind,created_at,coalesce(session_id,review_session_id) AS session_id,payout_id,
                      available_delta_cents,reserved_delta_cents,available_after_cents FROM balances WHERE true'''+where+
                    ' ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s',[profile['id']]+args+[limit,offset]).fetchall(),'limit':limit,'offset':offset}
        if parts[0]=='payouts':
            if len(parts)==1 and method=='POST':
                require(set(body)<= {'email','amount_cents'},'Only email and amount_cents are accepted; payouts have no fees')
                return 201,self.service.payout(profile['id'],body,idempotency(environ))
            if method=='GET':
                with self.service.db.connect() as c:
                    if len(parts)==1:
                        limit,offset=self.pagination(environ)
                        from .listing import filters
                        where,args=filters(environ,'requested_at','status')
                        return 200,{'items':c.execute('SELECT * FROM app.payout_requests WHERE user_id=%s'+where+' ORDER BY requested_at DESC,id DESC LIMIT %s OFFSET %s',
                                                     [profile['id']]+args+[limit,offset]).fetchall(),'limit':limit,'offset':offset}
                    if len(parts)==2:
                        row=c.execute('SELECT * FROM app.payout_requests WHERE id=%s AND user_id=%s',(identifier(parts[1]),profile['id'])).fetchone()
                        require(row,'Payout not found',404,'not_found');return 200,row
            if len(parts)==3 and parts[2]=='cancel' and method=='POST':
                return 200,self.service.payout_action(profile,parts[1],'cancel',body)
        if parts[0]=='support-cases':return self.support_route(method,parts[1:],environ,body,profile)
        if parts[0]=='notifications':
            with self.service.db.connect() as c:
                if len(parts)==1 and method=='GET':
                    limit,offset=self.pagination(environ)
                    return 200,{'items':c.execute('SELECT * FROM app.notifications WHERE user_id=%s ORDER BY created_at DESC,id LIMIT %s OFFSET %s',
                                                 (profile['id'],limit,offset)).fetchall(),'limit':limit,'offset':offset}
                if len(parts)==3 and parts[2]=='read' and method=='POST':
                    row=c.execute("UPDATE app.notifications SET status='sent',sent_at=coalesce(sent_at,now()) WHERE id=%s AND user_id=%s AND channel='in_app' RETURNING *",
                                  (identifier(parts[1]),profile['id'])).fetchone()
                    require(row,'Notification not found',404,'not_found');return 200,row
        raise APIError(404,'not_found','Route not found')

    @staticmethod
    def pagination(environ):
        from urllib.parse import parse_qs
        query=parse_qs(environ.get('QUERY_STRING',''))
        try:limit=int(query.get('limit',['50'])[0]);offset=int(query.get('offset',['0'])[0])
        except ValueError:raise APIError(400,'invalid_request','Pagination must use integers') from None
        return integer(limit,'limit',1,100),integer(offset,'offset',0,1000000)

    def auth_route(self,method,parts,environ,body):
        require(method=='POST' and len(parts)==1,'Auth endpoint requires POST',405,'method_not_allowed')
        action=parts[0]
        require(action in ('signup','login','refresh','logout','recover','verify','phone','email','password'),
                'Auth route not found',404,'not_found')
        self.service.rate_limit(str(body.get('identifier') or body.get('email') or body.get('phone') or
                                    environ.get('HTTP_AUTHORIZATION') or body.get('refresh_token') or 'anonymous'),
                                environ.get('REMOTE_ADDR','unknown'),'auth:'+action)
        if action=='signup':
            payload={'email':email(body.get('email')),'password':password(body.get('password')),
                     'data':{'first_name':text(body.get('first_name'),'first_name',100),
                             'last_name':text(body.get('last_name'),'last_name',100),
                             'requested_phone':phone(body.get('phone'))}}
            result=self.auth.call('signup',payload=payload)
            if result.get('access_token'):self.auth.logout(result['access_token'])
            return 202,{'message':'Check your email to complete signup, then sign in and verify your phone.',
                        'next_steps':['confirm_email','login','link_phone','verify_phone']}
        if action=='login':
            machine=self.service.machine(environ['HTTP_X_MACHINE_KEY']) if environ.get('HTTP_X_MACHINE_KEY') else None
            identity=text(body.get('identifier') or body.get('email') or body.get('phone'),'identifier',254)
            identity=email(identity) if '@' in identity else phone(identity)
            result=self.auth.login(identity,password(body.get('password')))
            token=result.get('access_token')
            require(token,'Authentication failed',401,'auth_failed')
            if machine:
                user,auth_id,sid=self.auth.verify_user(token)
                profile=self.service.profile(user)
                # Exchange the login for a collection-only binding. No Supabase token goes to the kiosk.
                self.auth.logout(token)
                session=self.service.start_session(profile,machine,sid,idempotency(environ))
                return 200,{'user_id':profile['id'],'full_name':profile['full_name'],
                            'session_id':session['id'],'session':session,'auth_scope':'machine_collection_only'}
            profile,user,sid=self.service.user_principal(self.auth,token)
            return 200,{k:result.get(k) for k in ('access_token','refresh_token','expires_in','token_type')} | {'user':profile}
        if action=='refresh':
            result=self.auth.call('token?grant_type=refresh_token',payload={'refresh_token':text(body.get('refresh_token'),'refresh_token',10000)})
            profile,user,sid=self.service.user_principal(self.auth,result.get('access_token',''))
            return 200,{k:result.get(k) for k in ('access_token','refresh_token','expires_in','token_type')} | {'user':profile}
        if action=='logout':
            self.auth.verify_user(bearer(environ));self.auth.logout(bearer(environ))
            return 200,{'logged_out':True}
        if action=='recover':
            self.auth.call('recover',payload={'email':email(body.get('email'))})
            return 202,{'message':'If the account can be recovered, check its email.'}
        if action=='verify':
            kind=body.get('type')
            require(kind in ('email','recovery','phone_change'),'Unsupported verification type')
            payload={'type':kind,'token':text(body.get('token'),'token',300)}
            expected_auth_id=None
            if kind=='phone_change':
                expected_auth_id=self.principal(environ,recent=True)[0]['auth_user_id']
                payload['phone']=phone(body.get('phone'))
            else:payload['email']=email(body.get('email'))
            result=self.auth.call('verify',payload=payload,token=bearer(environ) if kind=='phone_change' else None)
            if result.get('access_token'):
                if expected_auth_id:
                    require(self.auth.verify_user(result['access_token'])[1]==expected_auth_id,
                            'Phone verification does not belong to this account',403,'identity_mismatch')
                profile,user,sid=self.service.user_principal(self.auth,result['access_token'])
                return 200,{k:result.get(k) for k in ('access_token','refresh_token','expires_in','token_type')} | {'user':profile}
            return 200,{'verified':True}
        if action in ('phone','email','password'):
            profile,user,sid=self.principal(environ,recent=True)
            value=phone(body.get('phone')) if action=='phone' else email(body.get('email')) if action=='email' else password(body.get('password'))
            self.auth.call('user','PUT',{action:value},bearer(environ))
            return 200,{'updated':True,'verification_required':action!='password'}
        raise APIError(404,'not_found','Auth route not found')

    def collection_route(self,method,parts,environ,body):
        if not parts and method=='POST':
            require(environ.get('HTTP_X_MACHINE_KEY'),'Machine credential required',401,'machine_unauthorized')
            return self.auth_route('POST',['login'],environ,body)
        if not parts and method=='GET':
            profile,user,sid=self.principal(environ)
            limit,offset=self.pagination(environ)
            from .listing import filters
            where,args=filters(environ,'coalesce(s.ended_at,s.started_at)',"coalesce(r.resolution,s.reconciliation_status)")
            with self.service.db.connect() as c:
                return 200,{'items':c.execute('''SELECT s.*,m.machine_code,m.location AS machine_location,
                    r.resolution AS review_resolution,r.amount_cents AS review_amount_cents,r.resolved_at AS review_resolved_at
                    FROM app.collection_sessions s JOIN app.machines m ON m.id=s.machine_id
                    LEFT JOIN app.deposit_reviews r ON r.session_id=s.id WHERE s.user_id=%s'''+where+
                    ' ORDER BY s.started_at DESC,s.id DESC LIMIT %s OFFSET %s',
                    [profile['id']]+args+[limit,offset]).fetchall(),'limit':limit,'offset':offset}
        require(parts,'Route not found',404,'not_found')
        if method=='GET':
            machine=self.service.machine(environ['HTTP_X_MACHINE_KEY']) if environ.get('HTTP_X_MACHINE_KEY') else None
            profile=None if machine else self.principal(environ)[0]
            with self.service.db.connect() as c:
                s=self.service.session(c,parts[0],machine_id=machine['machine_id'] if machine else None,user_id=profile['id'] if profile else None)
                if len(parts)==1:
                    review=c.execute('SELECT resolution AS review_resolution,amount_cents AS review_amount_cents,resolved_at AS review_resolved_at FROM app.deposit_reviews WHERE session_id=%s',(s['id'],)).fetchone()
                    if review:s.update(review)
                    return 200,s
                if parts[1:]==['bottles']:
                    limit,offset=self.pagination(environ)
                    return 200,{'items':c.execute('SELECT * FROM app.bottle_events WHERE session_id=%s ORDER BY sequence_no LIMIT %s OFFSET %s',
                                                 (s['id'],limit,offset)).fetchall(),'limit':limit,'offset':offset}
        machine=self.service.machine(environ.get('HTTP_X_MACHINE_KEY'))
        if method=='POST' and len(parts)==2:
            if parts[1]=='bottles':return 201,self.service.add_bottle(machine,parts[0],body)
            if parts[1]=='submit':return 200,self.service.submit(machine,parts[0],body,idempotency(environ))
        if method=='POST' and len(parts)==4 and parts[1]=='bottles' and parts[3]=='capture':
            return 200,self.service.capture(machine,parts[0],parts[2],body)
        raise APIError(404,'not_found','Collection route not found')

    def sync_batch(self,machine,body):
        sid=identifier(body.get('session_id'),'session_id')
        batch=identifier(body.get('device_batch_id'),'device_batch_id')
        events=body.get('bottles')
        require(isinstance(events,list) and 1<=len(events)<=500,'bottles must contain 1–500 existing attempts')
        boot=identifier(body.get('machine_boot_id'),'machine_boot_id')
        with self.service.db.connect() as c:
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('batch:'+str(machine['machine_id'])+':'+str(batch),))
            require(c.execute('SELECT id FROM app.collection_sessions WHERE id=%s AND machine_id=%s',(sid,machine['machine_id'])).fetchone(),
                    'Session not found',404,'not_found')
            prior=c.execute('SELECT * FROM app.machine_sync_batches WHERE machine_id=%s AND device_batch_id=%s',(machine['machine_id'],batch)).fetchone()
            if prior:
                require(prior['payload_hash']==digest(body),'Batch conflicts with original',409,'batch_conflict')
                return 200,prior
            # Each acknowledgement is independently durable. The batch lock serializes conflicting retries.
            for item in events:
                require(isinstance(item,dict),'Every batch item must be an object')
                self.service.add_bottle(machine,sid,item)
                if item.get('status') in ('accepted','rejected'):
                    self.service.capture(machine,sid,item['machine_event_id'],item)
            return 200,c.execute('''INSERT INTO app.machine_sync_batches(machine_id,machine_boot_id,session_id,device_batch_id,
                payload_hash,first_sequence_no,last_sequence_no,status,applied_at,acknowledged_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'applied',now(),now()) ON CONFLICT (machine_id,device_batch_id)
                DO UPDATE SET acknowledged_at=app.machine_sync_batches.acknowledged_at RETURNING *''',
                (machine['machine_id'],boot,sid,batch,digest(body),
                 min(x['sequence_no'] for x in events),max(x['sequence_no'] for x in events))).fetchone()

    def support_route(self,method,parts,environ,body,profile):
        with self.service.db.connect() as c:
            if method=='GET':
                if parts:
                    row=c.execute('SELECT * FROM app.support_cases WHERE id=%s AND user_id=%s',(identifier(parts[0]),profile['id'])).fetchone()
                    require(row,'Support case not found',404,'not_found');return 200,row
                limit,offset=self.pagination(environ)
                return 200,{'items':c.execute('SELECT * FROM app.support_cases WHERE user_id=%s ORDER BY created_at DESC,id LIMIT %s OFFSET %s',
                                             (profile['id'],limit,offset)).fetchall(),'limit':limit,'offset':offset}
            if method=='POST' and not parts:
                key=idempotency(environ)
                old=self.service.replay(c,'user',profile['id'],'support',key,body)
                if old:return 200,c.execute('SELECT * FROM app.support_cases WHERE id=%s',(old,)).fetchone()
                category=body.get('category')
                require(category in ('missing_credit','bottle_dispute','payout_problem','account_recovery'),'Invalid support category')
                references={}
                for field,table in (('session_id','collection_sessions'),('bottle_event_id','bottle_events'),('payout_id','payout_requests')):
                    references[field]=identifier(body[field],field) if body.get(field) else None
                    if references[field]:
                        require(c.execute('SELECT id FROM app.'+table+' WHERE id=%s AND user_id=%s',(references[field],profile['id'])).fetchone(),
                                'Linked record not found',404,'not_found')
                row=c.execute('''INSERT INTO app.support_cases(user_id,category,description,session_id,bottle_event_id,payout_id)
                    VALUES (%s,%s,%s,%s,%s,%s) RETURNING *''',(profile['id'],category,text(body.get('description'),'description',4000),
                    references['session_id'],references['bottle_event_id'],references['payout_id'])).fetchone()
                self.service.remember(c,'user',profile['id'],'support',key,body,'support_case',row['id'])
                return 201,row
        raise APIError(404,'not_found','Support route not found')

    def admin_route(self,method,parts,environ,body):
        require(parts,'Route not found',404,'not_found')
        if parts[0] in ('deposit-reviews','fleet','overview','depots','evacuations','team','audit-events'):
            from .operations import OperationsAPI
            return OperationsAPI(self).dispatch(method,parts,environ,body)
        administrator=parts[0] in ('machines','staff-roles','wallet-adjustments')
        profile,user,sid=self.principal(environ,recent=method!='GET',administrator=administrator,staff=True)
        with self.service.db.connect() as c:
            if parts==['operations'] and method=='GET':
                return 200,{name:c.execute('SELECT * FROM app.'+name+' LIMIT 100').fetchall()
                            for name in ('wallet_liabilities','session_credit_sla','payout_processing_sla','machine_health','registry_health')}
            if parts[0]=='collection-sessions':
                if len(parts)==1 and method=='GET':
                    limit,offset=self.pagination(environ)
                    return 200,{'items':c.execute('SELECT * FROM app.collection_sessions ORDER BY started_at DESC,id LIMIT %s OFFSET %s',(limit,offset)).fetchall()}
                if len(parts)>=2:
                    s=c.execute('SELECT * FROM app.collection_sessions WHERE id=%s',(identifier(parts[1]),)).fetchone()
                    require(s,'Session not found',404,'not_found')
                    if len(parts)==2 and method=='GET':return 200,s
                    if parts[2:]==['bottles'] and method=='GET':
                        limit,offset=self.pagination(environ)
                        return 200,{'items':c.execute('SELECT * FROM app.bottle_events WHERE session_id=%s ORDER BY sequence_no LIMIT %s OFFSET %s',(s['id'],limit,offset)).fetchall()}
                    if parts[2:]==['reconcile'] and method=='POST':
                        self.service.reconcile(s['id'])
                        return 200,{'reconciliation_attempted':True}
            if parts[0] in ('operational-alerts','machine-incidents'):
                table='operational_alerts' if parts[0]=='operational-alerts' else 'machine_incidents'
                if len(parts)==1 and method=='GET':
                    limit,offset=self.pagination(environ)
                    return 200,{'items':c.execute('SELECT * FROM app.'+table+' LIMIT %s OFFSET %s',(limit,offset)).fetchall()}
                if len(parts)==2 and method=='PATCH':
                    status=body.get('status')
                    require(status in (('open','acknowledged','resolved') if table=='operational_alerts' else ('open','investigating','resolved')),'Invalid status')
                    if table=='operational_alerts':
                        row=c.execute("UPDATE app.operational_alerts SET status=%s,acknowledged_by=%s,resolved_at=CASE WHEN %s='resolved' THEN now() ELSE NULL END WHERE id=%s RETURNING *",
                                      (status,profile['id'],status,identifier(parts[1]))).fetchone()
                    else:
                        row=c.execute("UPDATE app.machine_incidents SET status=%s,assigned_to=%s,resolution_code=%s,resolved_at=CASE WHEN %s='resolved' THEN now() ELSE NULL END WHERE id=%s RETURNING *",
                                      (status,profile['id'],text(body.get('reason'),'reason',200),status,identifier(parts[1]))).fetchone()
                    require(row,'Record not found',404,'not_found')
                    self.service.audit(c,'staff',profile['id'],'operational_record_updated',table,row['id'],{'status':status})
                    return 200,row
            if parts[0]=='payouts':
                if len(parts)==1 and method=='GET':
                    limit,offset=self.pagination(environ)
                    from .listing import filters
                    where,args=filters(environ,'requested_at','status')
                    return 200,{'items':c.execute('SELECT * FROM app.payout_requests WHERE true'+where+' ORDER BY requested_at,id LIMIT %s OFFSET %s',args+[limit,offset]).fetchall(),
                                'limit':limit,'offset':offset}
                if len(parts)==2 and method=='GET':
                    row=c.execute('SELECT * FROM app.payout_requests WHERE id=%s',(identifier(parts[1]),)).fetchone()
                    require(row,'Payout not found',404,'not_found');return 200,row
                if len(parts)==3 and method=='POST':
                    # This service method owns its own short transaction; no financial writes occur above.
                    return 200,self.service.payout_action(profile,parts[1],parts[2],body,staff=True)
            if parts[0]=='machines':return self.admin_machines(c,profile,method,parts[1:],environ,body)
            if parts[0]=='staff-roles' and method=='POST' and len(parts)==1:
                target=identifier(body.get('user_id'),'user_id');role=body.get('role')
                require(role in ('administrator','payout_processor','operations_reviewer','collector','depot_receiver'),'Invalid staff role')
                if body.get('revoke'):
                    c.execute('UPDATE app.staff_roles SET revoked_at=now() WHERE user_id=%s AND role=%s AND revoked_at IS NULL',(target,role))
                else:c.execute('''INSERT INTO app.staff_roles(user_id,role,granted_by) VALUES (%s,%s,%s)
                    ON CONFLICT (user_id,role) WHERE revoked_at IS NULL DO NOTHING''',(target,role,profile['id']))
                self.service.audit(c,'staff',profile['id'],'staff_role_changed','profile',target,{'role':role,'revoked':bool(body.get('revoke'))})
                return 200,{'updated':True}
            if parts[0]=='support-cases':
                if method=='GET':
                    limit,offset=self.pagination(environ)
                    return 200,{'items':c.execute('SELECT * FROM app.support_cases ORDER BY created_at,id LIMIT %s OFFSET %s',(limit,offset)).fetchall()}
                if method=='PATCH' and len(parts)==2:
                    status=body.get('status');require(status in ('open','investigating','resolved','closed'),'Invalid support status')
                    row=c.execute('''UPDATE app.support_cases SET status=%s,assigned_to=%s,resolution_code=%s,
                        resolved_at=CASE WHEN %s='resolved' THEN now() ELSE resolved_at END,
                        closed_at=CASE WHEN %s='closed' THEN now() ELSE closed_at END WHERE id=%s RETURNING *''',
                        (status,profile['id'],text(body['resolution_code'],'resolution_code',200) if body.get('resolution_code') else None,status,status,identifier(parts[1]))).fetchone()
                    require(row,'Support case not found',404,'not_found')
                    self.service.audit(c,'staff',profile['id'],'support_updated','support_case',row['id'])
                    return 200,row
            if parts[0]=='wallet-adjustments':return self.adjustment(c,profile,method,parts[1:],environ,body)
        if parts==['jobs','run'] and method=='POST':
            from .worker import run_jobs
            return 200,run_jobs(self.service,limit=integer(body.get('limit',10),'limit',1,20))
        raise APIError(404,'not_found','Admin route not found')

    def admin_machines(self,c,profile,method,parts,environ,body):
        if method=='GET' and not parts:
            return 200,{'items':c.execute('SELECT * FROM app.machines ORDER BY machine_code LIMIT 100').fetchall()}
        if method=='POST' and not parts:
            key=idempotency(environ)
            old=self.service.replay(c,'staff',profile['id'],'machine',key,body)
            if old:return 200,c.execute('SELECT * FROM app.machines WHERE id=%s',(old,)).fetchone()
            status=body.get('status','disabled');mode=body.get('operating_mode','test')
            require(status in ('active','maintenance','disabled') and mode in ('live','test'),'Invalid machine state')
            row=c.execute('''INSERT INTO app.machines(machine_code,name,location,status,operating_mode)
                VALUES (%s,%s,%s,%s,%s) RETURNING *''',(text(body.get('machine_code'),'machine_code',100),
                text(body.get('name'),'name',200),text(body['location'],'location',300) if body.get('location') else None,status,mode)).fetchone()
            self.service.remember(c,'staff',profile['id'],'machine',key,body,'machine',row['id'])
            self.service.audit(c,'staff',profile['id'],'machine_created','machine',row['id'])
            return 201,row
        require(parts,'Machine not found',404,'not_found')
        machine=identifier(parts[0],'machine_id')
        require(c.execute('SELECT id FROM app.machines WHERE id=%s',(machine,)).fetchone(),'Machine not found',404,'not_found')
        if len(parts)==1 and method=='PATCH':
            status=body.get('status');mode=body.get('operating_mode')
            require(status in (None,'active','maintenance','disabled') and mode in (None,'live','test'),'Invalid machine state')
            self.service.audit(c,'staff',profile['id'],'machine_updated','machine',machine)
            return 200,c.execute('UPDATE app.machines SET status=coalesce(%s,status),operating_mode=coalesce(%s,operating_mode) WHERE id=%s RETURNING *',(status,mode,machine)).fetchone()
        if parts[1:]==['credentials'] and method=='POST':
            import hashlib,secrets
            key=idempotency(environ)
            old=self.service.replay(c,'staff',profile['id'],'credential',key,{'machine_id':str(machine)})
            require(old is None,'Credential already issued; revoke it and request a new key if its response was lost',409,'credential_already_issued')
            prefix='mc_'+uuid4().hex;secret=secrets.token_urlsafe(32)
            row=c.execute('INSERT INTO app.machine_credentials(machine_id,key_prefix,secret_hash) VALUES (%s,%s,%s) RETURNING id,key_prefix',
                          (machine,prefix,hashlib.sha256(secret.encode()).hexdigest())).fetchone()
            self.service.remember(c,'staff',profile['id'],'credential',key,{'machine_id':str(machine)},'machine_credential',row['id'])
            self.service.audit(c,'staff',profile['id'],'machine_credential_created','machine',machine)
            return 201,{'credential_id':row['id'],'machine_key':prefix+'.'+secret,'shown_once':True}
        if len(parts)==4 and parts[1]=='credentials' and parts[3]=='revoke' and method=='POST':
            row=c.execute('UPDATE app.machine_credentials SET revoked_at=coalesce(revoked_at,now()) WHERE id=%s AND machine_id=%s RETURNING id',
                          (identifier(parts[2]),machine)).fetchone()
            require(row,'Credential not found',404,'not_found')
            self.service.audit(c,'staff',profile['id'],'machine_credential_revoked','machine',machine)
            return 200,{'revoked':True}
        raise APIError(404,'not_found','Machine admin route not found')

    def adjustment(self,c,profile,method,parts,environ,body):
        if not parts and method=='GET':
            return 200,{'items':c.execute('SELECT * FROM app.wallet_adjustments ORDER BY created_at DESC LIMIT 100').fetchall()}
        if not parts and method=='POST':
            key=idempotency(environ);amount=body.get('amount_cents')
            require(type(amount) is int and amount!=0 and abs(amount)<=2**53-1,'amount_cents must be a nonzero integer')
            old=self.service.replay(c,'staff',profile['id'],'adjustment',key,body)
            if old:return 200,c.execute('SELECT * FROM app.wallet_adjustments WHERE id=%s',(old,)).fetchone()
            wallet=identifier(body.get('wallet_id'),'wallet_id')
            support=identifier(body['support_case_id']) if body.get('support_case_id') else None
            if support:
                require(c.execute('''SELECT 1 FROM app.support_cases s JOIN app.wallets w ON w.user_id=s.user_id
                    WHERE s.id=%s AND w.id=%s''',(support,wallet)).fetchone(),'Support case and wallet owner differ',409,'ownership_conflict')
            row=c.execute('''INSERT INTO app.wallet_adjustments(wallet_id,support_case_id,amount_cents,reason_code,requested_by)
                VALUES (%s,%s,%s,%s,%s) RETURNING *''',(wallet,support,amount,text(body.get('reason_code'),'reason_code',200),profile['id'])).fetchone()
            self.service.remember(c,'staff',profile['id'],'adjustment',key,body,'wallet_adjustment',row['id'])
            self.service.audit(c,'staff',profile['id'],'adjustment_proposed','wallet_adjustment',row['id'])
            return 201,row
        if len(parts)==2 and parts[1]=='approve' and method=='POST':
            row=c.execute('SELECT * FROM app.wallet_adjustments WHERE id=%s FOR UPDATE',(identifier(parts[0]),)).fetchone()
            require(row,'Adjustment not found',404,'not_found')
            if row['status']=='posted':return 200,row
            require(row['status']=='proposed','Adjustment is not pending approval',409,'adjustment_conflict')
            w=c.execute('SELECT * FROM app.wallets WHERE id=%s FOR NO KEY UPDATE',(row['wallet_id'],)).fetchone()
            require(self.service.balances(c,w['id'])['available_cents']+row['amount_cents']>=0,'Adjustment would overdraw wallet',409,'insufficient_funds')
            platform=c.execute("SELECT id FROM app.ledger_accounts WHERE purpose='platform_rewards'").fetchone()['id']
            tx=self.service.posting(c,'adjustment','adjustment:'+str(row['id']),[(w['available_account_id'],row['amount_cents']),(platform,-row['amount_cents'])])
            result=c.execute("UPDATE app.wallet_adjustments SET status='posted',approved_by=%s,approved_at=now(),posted_at=now(),ledger_transaction_id=%s WHERE id=%s RETURNING *",
                             (profile['id'],tx,row['id'])).fetchone()
            self.service.audit(c,'staff',profile['id'],'adjustment_posted','wallet_adjustment',row['id'])
            return 200,result
        raise APIError(404,'not_found','Adjustment route not found')
