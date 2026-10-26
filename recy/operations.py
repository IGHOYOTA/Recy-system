"""Audited deposit decisions, physical capacity and evacuation custody workflows."""
from datetime import timedelta
from uuid import uuid4

from .http_support import identifier, integer, require, text, timestamp, utcnow

OPERATIONS_ROLES = ('administrator', 'operations_reviewer')


class OperationsAPI:
    def __init__(self, api):
        self.api = api
        self.service = api.service

    def roles(self, user_id):
        with self.service.db.connect() as c:
            return [r['role'] for r in c.execute(
                'SELECT role FROM app.staff_roles WHERE user_id=%s AND revoked_at IS NULL', (user_id,))]

    def dispatch(self, method, parts, environ, body):
        from .web_api import idempotency
        profile, _, _ = self.api.principal(environ, recent=method != 'GET')
        roles = self.roles(profile['id'])
        group = parts[0]
        operational = any(r in roles for r in OPERATIONS_ROLES)
        require(roles, 'Staff authorization required', 403, 'forbidden')
        if group == 'deposit-reviews':
            require(operational, 'Deposit review permission required', 403, 'forbidden')
            return self.reviews(profile, roles, method, parts[1:], environ, body)
        if group in ('fleet', 'overview', 'team', 'audit-events'):
            require(operational, 'Operations permission required', 403, 'forbidden')
            require(method == 'GET', 'Use GET', 405, 'method_not_allowed')
            return self.read_operations(group, parts[1:], environ)
        if group == 'depots':
            require(operational or any(r in roles for r in ('collector','depot_receiver')), 'Depot permission required', 403, 'forbidden')
            return self.depots(profile, operational, method, parts[1:], environ, body)
        if group == 'evacuations':
            require(operational or any(r in roles for r in ('collector','depot_receiver')), 'Evacuation permission required', 403, 'forbidden')
            return self.evacuations(profile, roles, operational, method, parts[1:], environ, body)
        require(False, 'Route not found', 404, 'not_found')

    def read_operations(self, group, parts, environ):
        limit, offset = self.api.pagination(environ)
        with self.service.db.connect() as c:
            if group == 'overview':
                return 200, c.execute('''SELECT
                    (SELECT count(*) FROM app.collection_sessions s LEFT JOIN app.deposit_reviews r ON r.session_id=s.id
                     WHERE s.reconciliation_status='needs_review' AND coalesce(r.status,'open')<>'resolved') AS deposit_reviews,
                    (SELECT count(*) FROM app.payout_requests WHERE status='pending') AS pending_withdrawals,
                    (SELECT count(*) FROM app.payout_requests WHERE status IN ('pending','processing','needs_review') AND processing_due_at<now()) AS overdue_withdrawals,
                    (SELECT count(*) FROM app.machine_capacity WHERE fill_status='almost_full') AS almost_full_machines,
                    (SELECT count(*) FROM app.machine_capacity WHERE fill_status='full') AS full_machines,
                    (SELECT count(*) FROM app.machines WHERE last_heartbeat_at IS NULL OR last_heartbeat_at<now()-interval '5 minutes') AS offline_machines,
                    (SELECT count(*) FROM app.evacuations WHERE status NOT IN ('verified','cancelled')) AS open_evacuations,
                    (SELECT coalesce(sum(available_cents+reserved_cents),0) FROM app.wallet_balances) AS wallet_liability_cents,
                    (SELECT count(*) FROM app.bottle_events WHERE status='accepted') AS lifetime_accepted,
                    now() AS updated_at''').fetchone()
            if group == 'fleet':
                if parts:
                    machine = c.execute('SELECT * FROM app.machine_capacity WHERE id=%s', (identifier(parts[0]),)).fetchone()
                    require(machine, 'Machine not found', 404, 'not_found')
                    trends = c.execute("""SELECT date_trunc('day',decided_at) AS day,
                        count(*) FILTER (WHERE status='accepted') AS accepted,
                        count(*) FILTER (WHERE status='rejected') AS rejected FROM app.bottle_events
                        WHERE machine_id=%s AND decided_at>=now()-interval '30 days' GROUP BY 1 ORDER BY 1""", (machine['id'],)).fetchall()
                    return 200, {'machine':machine, 'daily_counts':trends}
                from .listing import filters
                where,args=filters(environ,'created_at','fill_status')
                return 200, {'items':c.execute('SELECT * FROM app.machine_capacity WHERE true'+where+' ORDER BY machine_code LIMIT %s OFFSET %s', args+[limit,offset]).fetchall(), 'limit':limit, 'offset':offset}
            if group == 'team':
                return 200, {'items':c.execute('''SELECT p.id,p.first_name,p.last_name,array_agg(r.role ORDER BY r.role) AS roles
                    FROM app.profiles p JOIN app.staff_roles r ON r.user_id=p.id AND r.revoked_at IS NULL
                    WHERE p.status='active' GROUP BY p.id ORDER BY p.first_name,p.id LIMIT %s OFFSET %s''', (limit,offset)).fetchall()}
            return 200, {'items':c.execute('SELECT id,actor_type,actor_id,action,resource_type,resource_id,created_at,redacted_details FROM app.audit_events ORDER BY created_at DESC,id LIMIT %s OFFSET %s', (limit,offset)).fetchall()}

    def reviews(self, profile, roles, method, parts, environ, body):
        from .web_api import idempotency
        with self.service.db.connect() as c:
            if method == 'GET' and not parts:
                limit,offset = self.api.pagination(environ)
                return 200, {'items':c.execute('''SELECT s.id AS session_id,s.machine_id,s.user_id,s.started_at,s.ended_at,s.last_error_code,
                    p.first_name||' '||p.last_name AS full_name,m.machine_code,
                    coalesce(r.status,'open') AS review_status,r.resolution,r.amount_cents,r.assigned_to,r.reason,
                    (SELECT count(*) FROM app.bottle_events b WHERE b.session_id=s.id AND b.status='accepted') AS evidenced_accepted
                    FROM app.collection_sessions s JOIN app.profiles p ON p.id=s.user_id JOIN app.machines m ON m.id=s.machine_id
                    LEFT JOIN app.deposit_reviews r ON r.session_id=s.id
                    WHERE s.reconciliation_status='needs_review' ORDER BY (coalesce(r.status,'open')='resolved'),s.ended_at LIMIT %s OFFSET %s''', (limit,offset)).fetchall()}
            require(parts, 'Review not found', 404, 'not_found')
            session = self.service.session(c, parts[0])
            require(session['reconciliation_status']=='needs_review', 'Session is not awaiting review', 409, 'review_conflict')
            review = c.execute('SELECT * FROM app.deposit_reviews WHERE session_id=%s', (session['id'],)).fetchone()
            if method == 'GET':
                return 200, {'session':session, 'review':review, 'bottles':c.execute('SELECT * FROM app.bottle_events WHERE session_id=%s ORDER BY sequence_no', (session['id'],)).fetchall(),
                             'submission':c.execute('SELECT * FROM app.session_submissions WHERE session_id=%s', (session['id'],)).fetchone()}
            require(method=='POST' and len(parts)==2 and parts[1] in ('claim','resolve'), 'Route not found',404,'not_found')
            key = idempotency(environ)
            operation = 'deposit_review:'+str(session['id'])+':'+parts[1]
            old = self.service.replay(c,'staff',profile['id'],operation,key,body)
            if old:
                return 200,c.execute('SELECT * FROM app.deposit_reviews WHERE id=%s',(old,)).fetchone()
            if review is None:
                review = c.execute('INSERT INTO app.deposit_reviews(session_id) VALUES (%s) RETURNING *',(session['id'],)).fetchone()
            require(review['status']!='resolved', 'Review is already resolved',409,'review_conflict')
            require(review['assigned_to'] in (None,profile['id']), 'Review belongs to another reviewer',409,'review_conflict')
            if parts[1]=='claim':
                result=c.execute("UPDATE app.deposit_reviews SET status='investigating',assigned_to=%s WHERE id=%s RETURNING *",(profile['id'],review['id'])).fetchone()
            else:
                resolution=body.get('resolution')
                require(resolution in ('approved','rejected'),'resolution must be approved or rejected')
                require(resolution!='approved' or 'administrator' in roles,'Administrator required to credit a correction',403,'forbidden')
                reason=text(body.get('reason'),'reason',2000)
                evidence=body.get('accepted_event_ids',[])
                require(isinstance(evidence,list) and len(evidence)<=5000,'accepted_event_ids must be an array')
                ids=[identifier(v,'accepted_event_id') for v in evidence]
                require(len(ids)==len(set(ids)),'Evidence IDs must be unique')
                adjustment=None
                amount=0
                if resolution=='approved':
                    require(ids,'Select physically confirmed accepted bottles')
                    rows=c.execute("""SELECT id FROM app.bottle_events WHERE session_id=%s AND id=ANY(%s)
                        AND status='accepted' AND eligibility_status='eligible' AND capture_event_id IS NOT NULL AND captured_at IS NOT NULL""",(session['id'],ids)).fetchall()
                    require(len(rows)==len(ids),'Every credited bottle must have stored eligibility and physical capture evidence',409,'evidence_required')
                    require(session['credit_transaction_id'] is None,'Session already credited',409,'review_conflict')
                    wallet=self.service.wallet(c,session['user_id'],True)
                    require(wallet['status']=='active','Wallet unavailable',409,'wallet_frozen')
                    amount=len(ids)*7
                    adjustment=c.execute('''INSERT INTO app.wallet_adjustments(wallet_id,amount_cents,reason_code,requested_by,approved_by,approved_at,status)
                        VALUES (%s,%s,'deposit_review',%s,%s,now(),'approved') RETURNING id''',(wallet['id'],amount,profile['id'],profile['id'])).fetchone()['id']
                    platform=c.execute("SELECT id FROM app.ledger_accounts WHERE purpose='platform_rewards' AND currency='CAD'").fetchone()['id']
                    tx=self.service.posting(c,'adjustment','deposit_review:'+str(session['id']),[(wallet['available_account_id'],amount),(platform,-amount)])
                    c.execute("UPDATE app.wallet_adjustments SET status='posted',posted_at=now(),ledger_transaction_id=%s WHERE id=%s",(tx,adjustment))
                else:
                    require(not ids,'Rejected review cannot credit bottles')
                result=c.execute('''UPDATE app.deposit_reviews SET status='resolved',assigned_to=%s,resolution=%s,reason=%s,
                    accepted_event_ids=%s,amount_cents=%s,adjustment_id=%s,resolved_by=%s,resolved_at=now() WHERE id=%s RETURNING *''',
                    (profile['id'],resolution,reason,ids,amount,adjustment,profile['id'],review['id'])).fetchone()
                c.execute("UPDATE app.work_jobs SET status='completed',completed_at=now(),locked_until=NULL WHERE kind='session_reconcile' AND resource_id=%s",(session['id'],))
                c.execute("UPDATE app.operational_alerts SET status='resolved',resolved_at=now() WHERE resource_id=%s AND kind='credit_overdue' AND status<>'resolved'",(session['id'],))
                c.execute("INSERT INTO app.notifications(user_id,kind,resource_type,resource_id,channel,deduplication_key) VALUES (%s,'deposit_review_resolved','collection_session',%s,'in_app',%s)",(session['user_id'],session['id'],'review:'+str(session['id'])))
            self.service.remember(c,'staff',profile['id'],operation,key,body,'deposit_review',result['id'])
            self.service.audit(c,'staff',profile['id'],'deposit_review_'+parts[1],'collection_session',session['id'],{'resolution':result['resolution'],'amount_cents':result['amount_cents']})
            return 200,result

    def depots(self, profile, operational, method, parts, environ, body):
        from .web_api import idempotency
        with self.service.db.connect() as c:
            if method=='GET':
                limit,offset=self.api.pagination(environ)
                return 200,{'items':c.execute('SELECT * FROM app.receiving_depots ORDER BY name,id LIMIT %s OFFSET %s',(limit,offset)).fetchall()}
            require(operational,'Operations permission required',403,'forbidden')
            if method=='POST' and not parts:
                key=idempotency(environ)
                old=self.service.replay(c,'staff',profile['id'],'depot',key,body)
                if old:return 200,c.execute('SELECT * FROM app.receiving_depots WHERE id=%s',(old,)).fetchone()
                row=c.execute('INSERT INTO app.receiving_depots(code,name,address,contact) VALUES (%s,%s,%s,%s) RETURNING *',
                    (text(body.get('code'),'code',100),text(body.get('name'),'name',200),text(body.get('address'),'address',500),text(body['contact'],'contact',200) if body.get('contact') else None)).fetchone()
                self.service.remember(c,'staff',profile['id'],'depot',key,body,'receiving_depot',row['id'])
            elif method=='PATCH' and len(parts)==1:
                require(type(body.get('active')) is bool,'active must be a boolean')
                row=c.execute('UPDATE app.receiving_depots SET active=%s WHERE id=%s RETURNING *',(body['active'],identifier(parts[0]))).fetchone()
                require(row,'Depot not found',404,'not_found')
            else:require(False,'Route not found',404,'not_found')
            self.service.audit(c,'staff',profile['id'],'depot_updated','receiving_depot',row['id'])
            return 200,row

    def evacuations(self, profile, roles, operational, method, parts, environ, body):
        from .web_api import idempotency
        with self.service.db.connect() as c:
            if method=='GET':
                limit,offset=self.api.pagination(environ)
                where='true' if operational or 'depot_receiver' in roles else 'e.assigned_collector_id=%s'
                args=[] if where=='true' else [profile['id']]
                query='''SELECT e.*,m.machine_code,m.location AS machine_location,
                    d.name AS planned_depot_name,a.name AS actual_depot_name,d.address AS planned_depot_address,a.address AS actual_depot_address,
                    p.first_name||' '||p.last_name AS collector_name,v.first_name||' '||v.last_name AS verified_by_name,
                    pc.first_name||' '||pc.last_name AS collected_by_name,pd.first_name||' '||pd.last_name AS delivered_by_name
                    FROM app.evacuations e JOIN app.machines m ON m.id=e.machine_id JOIN app.receiving_depots d ON d.id=e.planned_depot_id
                    LEFT JOIN app.receiving_depots a ON a.id=e.actual_depot_id JOIN app.profiles p ON p.id=e.assigned_collector_id
                    LEFT JOIN app.profiles v ON v.id=e.verified_by
                    LEFT JOIN app.profiles pc ON pc.id=e.collected_by LEFT JOIN app.profiles pd ON pd.id=e.delivered_by WHERE '''+where
                from .listing import filters
                extra,filter_args=filters(environ,'coalesce(e.collected_at,e.scheduled_at)','e.status')
                query+=extra;args.extend(filter_args)
                if parts:
                    query+=' AND e.id=%s';args.append(identifier(parts[0]))
                    row=c.execute(query,args).fetchone();require(row,'Evacuation not found',404,'not_found')
                    return 200,{'evacuation':row,'events':c.execute("""SELECT e.*,p.first_name||' '||p.last_name AS actor_name
                        FROM app.evacuation_events e JOIN app.profiles p ON p.id=e.actor_id
                        WHERE evacuation_id=%s ORDER BY e.created_at,e.id""",(row['id'],)).fetchall()}
                query+=' ORDER BY e.created_at DESC,e.id LIMIT %s OFFSET %s';args.extend([limit,offset])
                return 200,{'items':c.execute(query,args).fetchall(),'limit':limit,'offset':offset}
            require(method=='POST','Use POST',405,'method_not_allowed')
            key=idempotency(environ)
            operation='evacuation:create' if not parts else 'evacuation:'+str(identifier(parts[0]))+':'+(parts[1] if len(parts)==2 else '')
            old=self.service.replay(c,'staff',profile['id'],operation,key,body)
            if old:return 200,c.execute('SELECT * FROM app.evacuations WHERE id=%s',(old,)).fetchone()
            before=None
            if not parts:
                require(operational,'Operations permission required',403,'forbidden')
                machine=identifier(body.get('machine_id'),'machine_id');depot=identifier(body.get('depot_id'),'depot_id')
                collector=identifier(body.get('collector_id',profile['id']),'collector_id')
                require(c.execute('SELECT id FROM app.receiving_depots WHERE id=%s AND active',(depot,)).fetchone(),'Active depot required',409,'depot_unavailable')
                require(c.execute("""SELECT p.id FROM app.profiles p JOIN app.staff_roles r ON r.user_id=p.id AND r.revoked_at IS NULL
                    WHERE p.id=%s AND p.status='active' AND r.role IN ('administrator','operations_reviewer','collector')""",(collector,)).fetchone(),'Collector must be active and authorized',409,'collector_unavailable')
                row=c.execute('''INSERT INTO app.evacuations(machine_id,planned_depot_id,assigned_collector_id,created_by,scheduled_at,notes)
                    VALUES (%s,%s,%s,%s,%s,%s) RETURNING *''',(machine,depot,collector,profile['id'],timestamp(body.get('scheduled_at'),'scheduled_at'),
                    text(body['notes'],'notes',2000) if body.get('notes') else None)).fetchone()
                action='schedule'
            else:
                require(len(parts)==2,'Action required',404,'not_found')
                row=c.execute('SELECT * FROM app.evacuations WHERE id=%s FOR UPDATE',(identifier(parts[0]),)).fetchone()
                require(row,'Evacuation not found',404,'not_found')
                action=parts[1];before=row['status']
                collector=row['assigned_collector_id']==profile['id']
                reason=text(body.get('reason'),'reason',2000) if body.get('reason') else None
                if action=='collect':
                    require(operational or (collector and 'collector' in roles),'Assigned collector required',403,'forbidden')
                    require(before=='scheduled','Pickup already recorded or cancelled',409,'evacuation_conflict')
                    require(body.get('emptying_confirmed') is True,'Confirm that the machine was completely emptied')
                    machine=c.execute('SELECT * FROM app.machines WHERE id=%s FOR UPDATE',(row['machine_id'],)).fetchone()
                    total=c.execute("SELECT count(*) AS n FROM app.bottle_events WHERE machine_id=%s AND status='accepted'",(machine['id'],)).fetchone()['n']
                    cycle=uuid4()
                    row=c.execute('''UPDATE app.evacuations SET status='collected',collected_at=clock_timestamp(),collected_by=%s,
                        accepted_total_at_pickup=%s,estimated_count_at_pickup=%s,previous_cycle_id=%s,next_cycle_id=%s,seal_reference=%s WHERE id=%s RETURNING *''',
                        (profile['id'],total,max(0,total-machine['capacity_baseline_count']),machine['capacity_cycle_id'],cycle,
                         text(body['seal_reference'],'seal_reference',200) if body.get('seal_reference') else None,row['id'])).fetchone()
                    c.execute('UPDATE app.machines SET capacity_baseline_count=%s,capacity_cycle_id=%s,capacity_cycle_started_at=clock_timestamp() WHERE id=%s',(total,cycle,machine['id']))
                elif action=='transit':
                    require(operational or (collector and 'collector' in roles),'Assigned collector required',403,'forbidden')
                    require(before=='collected','Pickup must be recorded first',409,'evacuation_conflict')
                    row=c.execute("UPDATE app.evacuations SET status='in_transit' WHERE id=%s RETURNING *",(row['id'],)).fetchone()
                elif action=='deliver':
                    require(operational or (collector and 'collector' in roles),'Assigned collector required',403,'forbidden')
                    require(before in ('collected','in_transit','needs_review') and row['delivered_at'] is None,'Evacuation is not in transit',409,'evacuation_conflict')
                    depot=identifier(body.get('depot_id',row['planned_depot_id']),'depot_id')
                    require(c.execute('SELECT id FROM app.receiving_depots WHERE id=%s AND active',(depot,)).fetchone(),'Active depot required',409,'depot_unavailable')
                    row=c.execute("UPDATE app.evacuations SET status='delivered',delivered_at=clock_timestamp(),delivered_by=%s,actual_depot_id=%s WHERE id=%s RETURNING *",(profile['id'],depot,row['id'])).fetchone()
                elif action=='verify':
                    require(operational or 'depot_receiver' in roles,'Receiver permission required',403,'forbidden')
                    require(before in ('delivered','needs_review') and row['delivered_at'] is not None,'Delivery must be recorded first',409,'evacuation_conflict')
                    require(row['delivered_by']!=profile['id'],'A different person must verify the delivery',409,'independent_verification_required')
                    count=integer(body.get('received_count'),'received_count',0,1000000)
                    receipt=text(body.get('receipt_reference'),'receipt_reference',200)
                    if count!=row['estimated_count_at_pickup']:require(reason,'Explain count discrepancies before verification')
                    row=c.execute("""UPDATE app.evacuations SET status='verified',verified_at=clock_timestamp(),verified_by=%s,
                        received_count=%s,receipt_reference=%s WHERE id=%s RETURNING *""",(profile['id'],count,receipt,row['id'])).fetchone()
                elif action=='review':
                    require(operational or 'depot_receiver' in roles,'Receiver permission required',403,'forbidden')
                    require(before in ('collected','in_transit','delivered','needs_review'),'Cannot review this evacuation',409,'evacuation_conflict')
                    require(reason,'Review reason required')
                    row=c.execute("UPDATE app.evacuations SET status='needs_review' WHERE id=%s RETURNING *",(row['id'],)).fetchone()
                elif action=='cancel':
                    require(operational,'Operations permission required',403,'forbidden')
                    require(before=='scheduled','Only uncollected evacuations may be cancelled',409,'evacuation_conflict')
                    require(reason,'Cancellation reason required')
                    row=c.execute("UPDATE app.evacuations SET status='cancelled' WHERE id=%s RETURNING *",(row['id'],)).fetchone()
                else:require(False,'Unknown action',404,'not_found')
            c.execute('INSERT INTO app.evacuation_events(evacuation_id,actor_id,action,from_status,to_status,reason) VALUES (%s,%s,%s,%s,%s,%s)',
                (row['id'],profile['id'],action,before,row['status'],body.get('reason')))
            self.service.remember(c,'staff',profile['id'],operation,key,body,'evacuation',row['id'])
            self.service.audit(c,'staff',profile['id'],'evacuation_'+action,'evacuation',row['id'])
            return 200,row

    def fill_report(self, machine, body):
        event=identifier(body.get('machine_event_id'),'machine_event_id')
        cycle=identifier(body.get('cycle_id'),'cycle_id')
        observed=timestamp(body.get('observed_at'),'observed_at')
        level=body.get('level');require(level in ('normal','almost_full','full'),'Invalid fill level')
        percent=integer(body['percent'],'percent',0,100) if body.get('percent') is not None else None
        from .http_support import digest
        with self.service.db.connect() as c:
            m=c.execute('SELECT * FROM app.machines WHERE id=%s FOR UPDATE',(machine['machine_id'],)).fetchone()
            prior=c.execute('SELECT * FROM app.machine_fill_reports WHERE machine_id=%s AND machine_event_id=%s',(m['id'],event)).fetchone()
            if prior:
                require(prior['payload_hash']==digest(body),'Fill report conflicts with original',409,'event_conflict')
                return 200,prior
            require(cycle==m['capacity_cycle_id'],'Refresh the current capacity cycle from heartbeat',409,'stale_capacity_cycle')
            require(m['capacity_cycle_started_at']<=observed<=utcnow()+timedelta(minutes=1),'Report timestamp is outside the current cycle')
            row=c.execute('''INSERT INTO app.machine_fill_reports(machine_id,machine_event_id,cycle_id,payload_hash,level,percent,observed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *''',(m['id'],event,cycle,digest(body),level,percent,observed)).fetchone()
            self.service.audit(c,'machine',m['id'],'fill_reported','machine_fill_report',row['id'],{'level':level})
            return 201,row
