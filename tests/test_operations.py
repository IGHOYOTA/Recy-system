import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from recy.http_support import utcnow
from tests import test_application_api as api_tests


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'),'Disposable PostgreSQL required')
class OperationsTests(unittest.TestCase):
    setUp=api_tests.ApplicationAPITests.setUp
    tearDown=api_tests.ApplicationAPITests.tearDown
    post=api_tests.ApplicationAPITests.post
    balance=api_tests.ApplicationAPITests.balance
    request=api_tests.ApplicationAPITests.request
    start=api_tests.ApplicationAPITests.start
    bottle=api_tests.ApplicationAPITests.bottle
    captured=api_tests.ApplicationAPITests.captured
    final=api_tests.ApplicationAPITests.final

    def flagged(self):
        sid=self.start()['session_id'];first=self.captured(sid)
        self.bottle(sid,2,'999999999999')
        self.final(sid,[first])
        with self.db.connect() as c:
            event=c.execute("SELECT id FROM app.bottle_events WHERE session_id=%s AND status='accepted'",(sid,)).fetchone()['id']
        return sid,str(event)

    def depot(self):
        code,row=self.request('/api/v1/admin/depots','POST',{'code':'DEPOT','name':'Receiving depot','address':'123 Test Street'},token='staff',key='depot')
        self.assertEqual(code,200,row);return row['id']

    def evacuation(self):
        depot=self.depot()
        code,row=self.request('/api/v1/admin/evacuations','POST',{'machine_id':str(self.machine),'depot_id':depot,'scheduled_at':utcnow().isoformat()},token='staff',key='evacuation')
        self.assertEqual(code,200,row);return row['id'],depot

    def test_review_credit_is_evidenced_audited_and_exactly_once(self):
        sid,event=self.flagged();path='/api/v1/admin/deposit-reviews/'+sid+'/resolve'
        body={'resolution':'approved','reason':'Confirmed captured bottle; remaining attempt returned','accepted_event_ids':[event]}
        self.assertEqual(self.request(path,'POST',body,token='user',key='unauthorized')[0],403)
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies=list(pool.map(lambda _:self.request(path,'POST',body,token='staff',key='resolve'),range(2)))
        self.assertEqual([r[0] for r in replies],[200,200],replies)
        self.assertEqual(self.balance(),(707,0))
        self.assertEqual(self.request(path,'POST',body,token='staff',key='different')[0],409)
        with self.db.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM app.deposit_reviews WHERE adjustment_id IS NOT NULL').fetchone()['n'],1)
            self.assertEqual(c.execute('SELECT reconciliation_status FROM app.collection_sessions WHERE id=%s',(sid,)).fetchone()['reconciliation_status'],'needs_review')
            self.assertEqual(c.execute("SELECT count(*) AS n FROM app.audit_events WHERE action='deposit_review_resolve'").fetchone()['n'],1)

    def test_review_cannot_invent_evidence_or_change_resolved_decision(self):
        sid,event=self.flagged();path='/api/v1/admin/deposit-reviews/'+sid+'/resolve'
        body={'resolution':'approved','reason':'Invalid evidence','accepted_event_ids':[str(uuid4())]}
        self.assertEqual(self.request(path,'POST',body,token='staff',key='invalid')[0],409)
        rejected={'resolution':'rejected','reason':'Insufficient proof','accepted_event_ids':[]}
        code,row=self.request(path,'POST',rejected,token='staff',key='reject')
        self.assertEqual(code,200,row);self.assertEqual(self.balance(),(700,0))
        body['accepted_event_ids']=[event]
        self.assertEqual(self.request(path,'POST',body,token='staff',key='later')[0],409)

    def test_fill_report_full_latches_and_old_cycle_cannot_reset(self):
        sid=self.start()['session_id']
        pending,_=self.bottle(sid)
        _,heartbeat=self.request('/api/v1/machines/heartbeat','POST',{},machine=True)
        body={'machine_event_id':str(uuid4()),'cycle_id':heartbeat['capacity_cycle_id'],'observed_at':utcnow().isoformat(),'level':'full','percent':100}
        code,first=self.request('/api/v1/machines/fill-reports','POST',body,machine=True)
        self.assertEqual(code,201,first)
        new_attempt=dict(pending,machine_event_id=str(uuid4()),sequence_no=2)
        self.assertEqual(self.request('/api/v1/collection-sessions/'+sid+'/bottles','POST',new_attempt,machine=True)[0],409)
        proof={'status':'accepted','capture_event_id':str(uuid4()),'captured_at':utcnow().isoformat()}
        self.assertEqual(self.request('/api/v1/collection-sessions/'+sid+'/bottles/'+pending['machine_event_id']+'/capture','POST',proof,machine=True)[0],200)
        _,(code,closed)=self.final(sid,[dict(pending,**proof)])
        self.assertEqual(code,200,closed);self.assertEqual(closed['reward_total_cents'],7)
        self.assertEqual(self.request('/api/v1/machines/fill-reports','POST',body,machine=True)[0],200)
        self.assertEqual(self.request('/api/v1/machines/fill-reports','POST',dict(body,level='normal'),machine=True)[0],409)
        newer=dict(body,machine_event_id=str(uuid4()),observed_at=utcnow().isoformat(),level='normal',percent=0)
        self.assertEqual(self.request('/api/v1/machines/fill-reports','POST',newer,machine=True)[0],201)
        _,heartbeat=self.request('/api/v1/machines/heartbeat','POST',{},machine=True)
        self.assertEqual(heartbeat['fill_status'],'full')
        eid,_=self.evacuation()
        code,row=self.request('/api/v1/admin/evacuations/'+eid+'/collect','POST',{'emptying_confirmed':True},token='staff',key='collect')
        self.assertEqual(code,200,row)
        newer['machine_event_id']=str(uuid4())
        self.assertEqual(self.request('/api/v1/machines/fill-reports','POST',newer,machine=True)[0],409)
        _,heartbeat=self.request('/api/v1/machines/heartbeat','POST',{},machine=True)
        self.assertEqual(heartbeat['fill_status'],'normal')

    def test_8000_bottles_and_emptying_preserve_lifetime_count(self):
        sid=self.start()['session_id']
        with self.db.connect() as c:
            run=c.execute('SELECT max(id) AS id FROM recy.sync_runs').fetchone()['id']
            c.execute('''INSERT INTO app.bottle_events(machine_event_id,session_id,user_id,machine_id,sequence_no,upc,
                eligibility_status,status,reason_code,registry_sync_run_id,observed_at,payload_hash,decided_at,backend_acknowledged_at,
                capture_event_id,captured_at,reward_cents)
                SELECT gen_random_uuid(),%s,%s,%s,n,'001234567890','eligible','accepted','captured',%s,now(),n::text,now(),now(),gen_random_uuid(),now(),7
                FROM generate_series(1,8000) n''',(sid,self.user,self.machine,run))
        _,fleet=self.request('/api/v1/admin/fleet',token='staff')
        self.assertEqual(fleet['items'][0]['fill_status'],'almost_full')
        self.assertEqual(fleet['items'][0]['accepted_since_evacuation'],8000)
        eid,_=self.evacuation()
        path='/api/v1/admin/evacuations/'+eid+'/collect'
        body={'emptying_confirmed':True}
        self.assertEqual(self.request(path,'POST',body,token='staff',key='collect')[0],200)
        self.assertEqual(self.request(path,'POST',body,token='staff',key='collect')[0],200)
        _,fleet=self.request('/api/v1/admin/fleet',token='staff')
        self.assertEqual(fleet['items'][0]['lifetime_accepted'],8000)
        self.assertEqual(fleet['items'][0]['accepted_since_evacuation'],0)
        self.assertEqual(fleet['items'][0]['fill_status'],'normal')
        self.assertEqual(self.balance(),(700,0))

    def test_operations_reviewer_cannot_issue_financial_corrections(self):
        sid,event=self.flagged()
        with self.db.connect() as c:
            c.execute("INSERT INTO app.staff_roles(user_id,role,granted_by) VALUES (%s,'operations_reviewer',%s)",(self.user,self.staff))
        path='/api/v1/admin/deposit-reviews/'+sid+'/resolve'
        body={'resolution':'approved','reason':'Test','accepted_event_ids':[event]}
        self.assertEqual(self.request(path,'POST',body,token='user',key='no-finance')[0],403)
        self.assertEqual(self.balance(),(700,0))

    def test_pickup_delivery_and_independent_verification(self):
        eid,depot=self.evacuation();base='/api/v1/admin/evacuations/'+eid
        self.assertEqual(self.request(base+'/deliver','POST',{},token='staff',key='early')[0],409)
        self.assertEqual(self.request(base+'/collect','POST',{},token='staff',key='unconfirmed')[0],400)
        self.assertEqual(self.request(base+'/collect','POST',{'emptying_confirmed':True},token='staff',key='pickup')[0],200)
        self.assertEqual(self.request(base+'/deliver','POST',{'depot_id':depot},token='staff',key='deliver')[0],200)
        proof={'received_count':0,'receipt_reference':'RECEIPT-1'}
        self.assertEqual(self.request(base+'/verify','POST',proof,token='staff',key='self-verify')[0],409)
        with self.db.connect() as c:c.execute("INSERT INTO app.staff_roles(user_id,role,granted_by) VALUES (%s,'depot_receiver',%s)",(self.user,self.staff))
        code,row=self.request(base+'/verify','POST',proof,token='user',key='verify')
        self.assertEqual(code,200,row);self.assertEqual(row['status'],'verified')
        self.assertEqual(self.request('/api/v1/admin/payouts',token='user')[0],403)
        _,detail=self.request(base,token='staff');self.assertEqual(len(detail['events']),4)

    def test_collector_has_no_financial_access_or_other_pickups(self):
        eid,_=self.evacuation()
        with self.db.connect() as c:c.execute("INSERT INTO app.staff_roles(user_id,role,granted_by) VALUES (%s,'collector',%s)",(self.user,self.staff))
        _,listing=self.request('/api/v1/admin/evacuations',token='user')
        self.assertEqual(listing['items'],[])
        self.assertEqual(self.request('/api/v1/admin/evacuations/'+eid+'/collect','POST',{'emptying_confirmed':True},token='user',key='steal')[0],403)
        self.assertEqual(self.request('/api/v1/admin/deposit-reviews',token='user')[0],403)
        self.assertEqual(self.request('/api/v1/admin/payouts',token='user')[0],403)
