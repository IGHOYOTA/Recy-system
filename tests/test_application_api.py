import io
import json
import os
import unittest
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from tests import test_app_schema as schema
from recy.app_service import AppService
from recy.http_support import APIError, utcnow
from recy.worker import run_jobs
from recy.wsgi import application


class FakeAuth:
    """Trusted-provider test double; real HTTP adapter has separate transport tests."""
    def __init__(self,db):self.db=db;self.tokens={};self.accounts={};self.calls=[]

    def add(self,auth_id,sid,token,email='user@example.com',phone='+16045550123'):
        user={'id':str(auth_id),'email':email,'phone':phone,'email_confirmed_at':utcnow().isoformat(),
              'phone_confirmed_at':utcnow().isoformat(),'user_metadata':{'first_name':'Test','last_name':'User','role':'administrator'}}
        self.tokens[token]=(user,auth_id,sid);self.accounts[email]=(user,auth_id);self.accounts[phone]=(user,auth_id)

    def login(self,identity,password):
        if identity not in self.accounts or password!='correct-password':raise APIError(401,'auth_failed','Invalid login')
        user,auth_id=self.accounts[identity];sid=uuid4();token='token-'+str(sid)
        with self.db.connect() as c:c.execute('INSERT INTO auth.sessions(id,user_id) VALUES (%s,%s)',(sid,auth_id))
        self.tokens[token]=(user,auth_id,sid)
        return {'access_token':token,'refresh_token':'refresh-test','expires_in':3600,'token_type':'bearer'}

    def verify_user(self,token):
        if token not in self.tokens:raise APIError(401,'unauthorized','Invalid session')
        return self.tokens[token]

    def logout(self,token):
        user,auth_id,sid=self.verify_user(token)
        with self.db.connect() as c:c.execute('DELETE FROM auth.sessions WHERE id=%s',(sid,))
        self.calls.append(('logout',sid))
        return {}

    def call(self,path,method='POST',payload=None,token=None):
        self.calls.append((path,payload))
        return {}


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'),'Disposable PostgreSQL required')
class ApplicationAPITests(unittest.TestCase):
    # Reuse schema fixtures, not their test methods.
    post=schema.ApplicationSchemaTests.post
    balance=schema.ApplicationSchemaTests.balance
    tearDown=schema.ApplicationSchemaTests.tearDown

    def setUp(self):
        schema.ApplicationSchemaTests.setUp(self)
        self.auth=FakeAuth(self.db)
        with self.db.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS auth.sessions (id uuid PRIMARY KEY,user_id uuid NOT NULL REFERENCES auth.users(id),created_at timestamptz NOT NULL DEFAULT now())')
            for name,profile,mail in (('user',self.user,'user@example.com'),('staff',self.staff,'staff@example.com')):
                auth_id,sid=uuid4(),uuid4()
                c.execute('INSERT INTO auth.users(id) VALUES (%s)',(auth_id,))
                c.execute('INSERT INTO auth.sessions(id,user_id) VALUES (%s,%s)',(sid,auth_id))
                c.execute('UPDATE app.profiles SET auth_user_id=%s WHERE id=%s',(auth_id,profile))
                self.auth.add(auth_id,sid,name,mail,'+16045550123' if name=='user' else '+16045550124')
            import hashlib
            self.machine_key='mc_test.'+'a'*43
            c.execute("INSERT INTO app.machine_credentials(machine_id,key_prefix,secret_hash) VALUES (%s,'mc_test',%s)",
                      (self.machine,hashlib.sha256(('a'*43).encode()).hexdigest()))
        self.db.sync(__import__('recy.source',fromlist=['Snapshot']).Snapshot(['UPC','Brand'],[('001234567890','Example')],1),utcnow().isoformat())
        self.addCleanup(patch.stopall)
        patch('recy.wsgi.get_database',return_value=self.db).start()
        patch('recy.web_api.SupabaseAuth',return_value=self.auth).start()
        patch.dict(os.environ,{'RECY_API_KEY':'legacy-key','RECY_ALLOWED_ORIGINS':'https://app.example.com'}).start()

    def request(self,path,method='GET',body=None,token=None,machine=False,key=None,raw=None,origin=None):
        captured=[]
        encoded=raw if raw is not None else json.dumps(body).encode() if body is not None else b''
        route,_,query=path.partition('?')
        environ={'PATH_INFO':route,'QUERY_STRING':query,'REQUEST_METHOD':method,'wsgi.input':io.BytesIO(encoded),
                 'CONTENT_LENGTH':str(len(encoded)),'CONTENT_TYPE':'application/json','REMOTE_ADDR':'127.0.0.1'}
        if token:environ['HTTP_AUTHORIZATION']='Bearer '+token
        if machine:environ['HTTP_X_MACHINE_KEY']=self.machine_key
        if key:environ['HTTP_IDEMPOTENCY_KEY']=key
        if origin:environ['HTTP_ORIGIN']=origin
        result=b''.join(application(environ,lambda status,headers:captured.append((status,headers))))
        return int(captured[0][0].split()[0]),json.loads(result)

    def start(self):
        code,result=self.request('/api/v1/auth/login','POST',{'email':'user@example.com','password':'correct-password'},machine=True,key='start')
        self.assertEqual(code,200,result)
        return result

    def bottle(self,sid,seq=1,upc='001234567890'):
        event=str(uuid4())
        payload={'machine_event_id':event,'sequence_no':seq,'upc':upc,'observed_at':utcnow().isoformat()}
        code,row=self.request('/api/v1/collection-sessions/'+sid+'/bottles','POST',payload,machine=True)
        self.assertEqual(code,201,row)
        return payload,row

    def captured(self,sid,seq=1):
        payload,row=self.bottle(sid,seq)
        capture={'status':'accepted','capture_event_id':str(uuid4()),'captured_at':utcnow().isoformat()}
        code,result=self.request('/api/v1/collection-sessions/'+sid+'/bottles/'+payload['machine_event_id']+'/capture','POST',capture,machine=True)
        self.assertEqual(code,200,result)
        return dict(payload,**capture)

    def final(self,sid,bottles,key='end'):
        body={'machine_end_event_id':str(uuid4()),'end_reason':'inactivity','final_sequence_no':len(bottles),'bottles':bottles}
        return body,self.request('/api/v1/collection-sessions/'+sid+'/submit','POST',body,machine=True,key=key)

    def test_authentication_and_staff_boundaries(self):
        self.assertEqual(self.request('/api/v1/wallet')[0],401)
        self.assertEqual(self.request('/api/v1/wallet',token='legacy-key')[0],401)
        self.assertEqual(self.request('/api/v1/admin/payouts',token='user')[0],403)
        self.assertEqual(self.request('/api/v1/admin/machines',token='staff')[0],200)
        code,result=self.request('/api/v1/me',token='user')
        self.assertEqual(code,200);self.assertEqual(result['user']['full_name'],'Test User')

    def test_web_login_phone_login_and_logout(self):
        for identity in ('user@example.com','+16045550123'):
            code,result=self.request('/api/v1/auth/login','POST',{'identifier':identity,'password':'correct-password'})
            self.assertEqual(code,200,result)
            self.assertEqual(result['user']['full_name'],'Test User')
            token=result['access_token']
            self.assertEqual(self.request('/api/v1/auth/logout','POST',{},token=token)[0],200)
            self.assertEqual(self.request('/api/v1/wallet',token=token)[0],401)

    def test_signup_starts_verification_without_storing_password(self):
        body={'first_name':'New','last_name':'User','email':'new@example.com','phone':'+16045550125','password':'new-password'}
        code,result=self.request('/api/v1/auth/signup','POST',body)
        self.assertEqual(code,202,result)
        self.assertIn('verify_phone',result['next_steps'])
        self.assertEqual(self.auth.calls[-1][1]['data']['requested_phone'],'+16045550125')

    def test_machine_login_exchanges_and_revokes_auth_tokens(self):
        result=self.start()
        self.assertNotIn('access_token',result)
        self.assertNotIn('refresh_token',result)
        self.assertEqual(result['auth_scope'],'machine_collection_only')
        self.assertTrue(self.auth.calls)
        self.assertEqual(self.request('/api/v1/wallet',token='user')[0],200) # independent web login survives

    def test_complete_session_retries_credit_once_and_logout(self):
        sid=self.start()['session_id']
        bottles=[self.captured(sid,1),self.captured(sid,2)]
        body,(code,result)=self.final(sid,bottles)
        self.assertEqual(code,200,result)
        self.assertEqual(result['reconciliation_status'],'posted')
        self.assertEqual(result['reward_total_cents'],14)
        self.assertTrue(result['user_logged_out'])
        self.assertEqual(self.balance(),(714,0))
        code,again=self.request('/api/v1/collection-sessions/'+sid+'/submit','POST',body,machine=True,key='end')
        self.assertEqual(code,200,again);self.assertEqual(self.balance(),(714,0))
        with self.db.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) AS n FROM app.ledger_transactions WHERE kind='session_credit'").fetchone()['n'],1)
            self.assertEqual(c.execute('SELECT logout_status FROM app.machine_logins').fetchone()['logout_status'],'revoked')

    def test_empty_session_and_rejected_container_no_credit(self):
        sid=self.start()['session_id']
        payload,row=self.bottle(sid,1,'999999999999')
        self.assertEqual(row['status'],'rejected')
        body,(code,result)=self.final(sid,[dict(payload,status='rejected')])
        self.assertEqual(code,200,result);self.assertEqual(result['reconciliation_status'],'no_credit')
        self.assertEqual(self.balance(),(700,0))

    def test_forged_accepted_count_is_reviewed_without_credit(self):
        sid=self.start()['session_id']
        forged={'machine_event_id':str(uuid4()),'sequence_no':1,'upc':'001234567890','status':'accepted',
                'capture_event_id':str(uuid4()),'captured_at':utcnow().isoformat()}
        body,(code,result)=self.final(sid,[forged])
        self.assertEqual(code,200,result)
        self.assertEqual(result['reconciliation_status'],'needs_review')
        self.assertTrue(result['user_logged_out']);self.assertEqual(self.balance(),(700,0))

    def test_session_owner_and_machine_isolation(self):
        sid=self.start()['session_id']
        self.assertEqual(self.request('/api/v1/collection-sessions/'+sid,token='staff')[0],404)
        self.assertEqual(self.request('/api/v1/collection-sessions/'+sid+'/bottles','POST',{},token='user')[0],401)
        with self.db.connect() as c:
            import hashlib
            other=c.execute("INSERT INTO app.machines(machine_code,name,status,operating_mode) VALUES ('OTHER','Other','active','live') RETURNING id").fetchone()['id']
            c.execute("INSERT INTO app.machine_credentials(machine_id,key_prefix,secret_hash) VALUES (%s,'mc_other',%s)",(other,hashlib.sha256(('b'*43).encode()).hexdigest()))
        self.machine_key='mc_other.'+'b'*43
        self.assertEqual(self.request('/api/v1/collection-sessions/'+sid,machine=True)[0],404)

    def test_bottle_retry_conflicts_and_capture_cannot_be_forged(self):
        sid=self.start()['session_id'];payload,row=self.bottle(sid)
        code,again=self.request('/api/v1/collection-sessions/'+sid+'/bottles','POST',payload,machine=True)
        self.assertEqual(code,201);self.assertEqual(again['id'],row['id'])
        payload['upc']='999999999999'
        self.assertEqual(self.request('/api/v1/collection-sessions/'+sid+'/bottles','POST',payload,machine=True)[0],409)
        body,(code,result)=self.final(sid,[dict(payload,status='accepted')])
        self.assertEqual(code,200,result);self.assertEqual(result['reconciliation_status'],'needs_review')

    def test_credit_recovers_after_server_failure_following_submission(self):
        sid=self.start()['session_id'];bottle=self.captured(sid)
        with patch.object(AppService,'reconcile',side_effect=RuntimeError('simulated outage')):
            body,(code,result)=self.final(sid,[bottle])
        self.assertEqual(code,503)
        with self.db.connect() as c:
            self.assertEqual(c.execute('SELECT collection_status FROM app.collection_sessions').fetchone()['collection_status'],'submitted')
        run_jobs(AppService(self.db))
        self.assertEqual(self.balance(),(707,0))
        self.assertEqual(self.request('/api/v1/collection-sessions/'+sid+'/submit','POST',body,machine=True,key='end')[0],200)
        self.assertEqual(self.balance(),(707,0))

    def test_payout_minimum_no_fee_idempotency_and_cancel(self):
        self.assertEqual(self.request('/api/v1/payouts','POST',{'email':'user@example.com','amount_cents':499},token='user',key='small')[0],400)
        self.assertEqual(self.request('/api/v1/payouts','POST',{'email':'user@example.com','amount_cents':500,'fee_cents':0},token='user',key='extra')[0],400)
        body={'email':'user@example.com','amount_cents':500}
        code,result=self.request('/api/v1/payouts','POST',body,token='user',key='pay')
        self.assertEqual(code,201,result);self.assertEqual(self.balance(),(200,500))
        self.assertEqual(result['fee_cents'],0)
        self.assertEqual(self.request('/api/v1/payouts','POST',body,token='user',key='pay')[1]['id'],result['id'])
        self.assertEqual(self.request('/api/v1/payouts','POST',dict(body,amount_cents=700),token='user',key='pay')[0],409)
        self.assertEqual(self.request('/api/v1/payouts/'+result['id']+'/cancel','POST',{},token='user')[0],200)
        self.assertEqual(self.balance(),(700,0))

    def test_staff_claim_completion_and_no_second_debit(self):
        code,p=self.request('/api/v1/payouts','POST',{'email':'user@example.com','amount_cents':700},token='user',key='full')
        self.assertEqual(code,201,p)
        base='/api/v1/admin/payouts/'+p['id']
        self.assertEqual(self.request(base+'/claim','POST',{},token='user')[0],403)
        self.assertEqual(self.request(base+'/claim','POST',{},token='staff')[0],200)
        self.assertEqual(self.request('/api/v1/payouts/'+p['id']+'/cancel','POST',{},token='user')[0],409)
        paid={'payment_reference':'BANK-123','paid_at':utcnow().isoformat()}
        code,result=self.request(base+'/complete','POST',paid,token='staff')
        self.assertEqual(code,200,result);self.assertEqual(self.balance(),(0,0))
        self.assertEqual(self.request(base+'/complete','POST',paid,token='staff')[0],200)
        self.assertEqual(self.balance(),(0,0))

    def test_staff_reject_requires_confirmed_unpaid(self):
        _,p=self.request('/api/v1/payouts','POST',{'email':'user@example.com','amount_cents':500},token='user',key='reject')
        path='/api/v1/admin/payouts/'+p['id']+'/reject'
        self.assertEqual(self.request(path,'POST',{'reason':'incorrect email'},token='staff')[0],400)
        self.assertEqual(self.request(path,'POST',{'reason':'incorrect email','confirmed_unpaid':True},token='staff')[0],200)
        self.assertEqual(self.balance(),(700,0))

    def test_recent_auth_required_for_withdrawal(self):
        with self.db.connect() as c:c.execute("UPDATE auth.sessions SET created_at=now()-interval '1 day'")
        self.assertEqual(self.request('/api/v1/payouts','POST',{'email':'user@example.com','amount_cents':500},token='user',key='stale')[0],401)
        self.assertEqual(self.balance(),(700,0))

    def test_support_and_adjustment_authorization(self):
        code,case=self.request('/api/v1/support-cases','POST',{'category':'missing_credit','description':'Missing bottle'},token='user',key='case')
        self.assertEqual(code,201,case)
        self.assertEqual(self.request('/api/v1/support-cases/'+case['id'],token='staff')[0],404)
        payload={'wallet_id':str(self.wallet),'amount_cents':7,'reason_code':'support_credit','support_case_id':case['id']}
        self.assertEqual(self.request('/api/v1/admin/wallet-adjustments','POST',payload,token='user',key='bad')[0],403)
        code,adjust=self.request('/api/v1/admin/wallet-adjustments','POST',payload,token='staff',key='adjust')
        self.assertEqual(code,201,adjust)
        path='/api/v1/admin/wallet-adjustments/'+adjust['id']+'/approve'
        code,result=self.request(path,'POST',{},token='staff')
        self.assertEqual(code,200,result);self.assertEqual(self.balance(),(707,0))
        self.assertEqual(self.request(path,'POST',{},token='staff')[0],200);self.assertEqual(self.balance(),(707,0))

    def test_machine_provisioning_key_rotation_and_revocation(self):
        body={'machine_code':'NEW','name':'New machine','status':'active','operating_mode':'live'}
        code,m=self.request('/api/v1/admin/machines','POST',body,token='staff',key='machine')
        self.assertEqual(code,201,m)
        path='/api/v1/admin/machines/'+m['id']+'/credentials'
        code,result=self.request(path,'POST',{},token='staff',key='credential')
        self.assertEqual(code,201,result)
        self.assertEqual(self.request(path,'POST',{},token='staff',key='credential')[0],409)
        self.machine_key=result['machine_key']
        self.assertEqual(self.request('/api/v1/machines/heartbeat','POST',{},machine=True)[0],200)
        self.assertEqual(self.request(path+'/'+result['credential_id']+'/revoke','POST',{},token='staff')[0],200)
        self.assertEqual(self.request('/api/v1/machines/heartbeat','POST',{},machine=True)[0],401)

    def test_http_parsing_cors_and_openapi(self):
        self.assertEqual(self.request('/api/v1/auth/login','POST',raw=b'{"email":"one","email":"two"}')[0],400)
        self.assertEqual(self.request('/api/v1/auth/login','POST',raw=b'{"amount":NaN}')[0],400)
        self.assertEqual(self.request('/api/v1/wallet',token='user',origin='https://evil.example.com')[0],403)
        self.assertEqual(self.request('/api/v1/wallet',method='OPTIONS',origin='https://app.example.com')[0],200)
        code,spec=self.request('/api/v1/openapi.json')
        self.assertEqual(code,200);self.assertIn('/payouts',spec['paths'])

    def test_rate_limit_is_shared_and_contains_no_raw_identity(self):
        for _ in range(10):self.request('/api/v1/auth/login','POST',{'email':'user@example.com','password':'wrong-pass'})
        self.assertEqual(self.request('/api/v1/auth/login','POST',{'email':'user@example.com','password':'correct-password'})[0],429)
        with self.db.connect() as c:
            self.assertTrue(all('@' not in r['bucket'] for r in c.execute('SELECT bucket FROM app.auth_rate_limits')))

    def test_final_dictionary_can_confirm_capture_or_reference_existing_evidence(self):
        sid=self.start()['session_id']
        first=self.captured(sid)
        minimal={k:first[k] for k in ('machine_event_id','sequence_no','upc','status')}
        second,_=self.bottle(sid,2)
        second.update(status='accepted',capture_event_id=str(uuid4()),captured_at=utcnow().isoformat())
        _,(code,result)=self.final(sid,[minimal,second])
        self.assertEqual(code,200,result);self.assertEqual(result['reward_total_cents'],14)
        self.assertEqual(self.balance(),(714,0))
        code,notifications=self.request('/api/v1/notifications',token='user')
        self.assertEqual(code,200);self.assertEqual(len(notifications['items']),1)
        notice=notifications['items'][0]['id']
        self.assertEqual(self.request('/api/v1/notifications/'+notice+'/read','POST',{},token='staff')[0],404)
        self.assertEqual(self.request('/api/v1/notifications/'+notice+'/read','POST',{},token='user')[0],200)

    def test_unavailable_registry_is_recorded_and_never_credited(self):
        sid=self.start()['session_id']
        with self.db.connect() as c:c.execute("UPDATE recy.sync_runs SET completed_at='2020-01-01T00:00:00+00:00'")
        payload,row=self.bottle(sid)
        self.assertEqual(row['eligibility_status'],'unavailable');self.assertEqual(row['status'],'rejected')
        _,(code,result)=self.final(sid,[dict(payload,status='rejected')])
        self.assertEqual(code,200,result);self.assertEqual(result['reconciliation_status'],'no_credit')
        self.assertEqual(self.balance(),(700,0))

    def test_sync_batch_is_durable_and_detects_conflicting_retry(self):
        sid=self.start()['session_id'];payload,_=self.bottle(sid)
        event=dict(payload,status='accepted',capture_event_id=str(uuid4()),captured_at=utcnow().isoformat())
        body={'session_id':sid,'device_batch_id':str(uuid4()),'machine_boot_id':str(uuid4()),'bottles':[event]}
        path='/api/v1/machines/sync-batches'
        code,first=self.request(path,'POST',body,machine=True)
        self.assertEqual(code,200,first)
        self.assertEqual(self.request(path,'POST',body,machine=True)[1]['id'],first['id'])
        changed=dict(body,bottles=[dict(event,status='rejected')])
        self.assertEqual(self.request(path,'POST',changed,machine=True)[0],409)
        _,(code,result)=self.final(sid,[event]);self.assertEqual(code,200,result)
        self.assertEqual(self.balance(),(707,0))

    def test_parallel_payout_requests_cannot_spend_same_balance(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda key:self.request('/api/v1/payouts','POST',
                {'email':'user@example.com','amount_cents':500},token='user',key=key),['parallel-a','parallel-b']))
        self.assertEqual(sorted(r[0] for r in results),[201,409],results)
        self.assertEqual(self.balance(),(200,500))

    def test_staff_session_review_and_incident_resolution(self):
        sid=self.start()['session_id'];payload,_=self.bottle(sid)
        self.final(sid,[dict(payload,status='accepted')])
        base='/api/v1/admin/collection-sessions/'+sid
        self.assertEqual(self.request(base,token='staff')[0],200)
        self.assertEqual(self.request(base+'/bottles',token='staff')[0],200)
        self.assertEqual(self.request(base+'/reconcile','POST',{},token='staff')[0],200)
        code,incidents=self.request('/api/v1/admin/machine-incidents',token='staff')
        self.assertEqual(code,200);self.assertEqual(len(incidents['items']),1)
        path='/api/v1/admin/machine-incidents/'+incidents['items'][0]['id']
        code,result=self.request(path,'PATCH',{'status':'resolved','reason':'support_follow_up'},token='staff')
        self.assertEqual(code,200,result)
        self.assertEqual(self.balance(),(700,0))

    def test_phone_verification_cannot_switch_auth_identity(self):
        with patch.object(self.auth,'call',return_value={'access_token':'staff'}):
            code,result=self.request('/api/v1/auth/verify','POST',
                {'type':'phone_change','phone':'+16045550123','token':'123456'},token='user')
        self.assertEqual(code,403,result);self.assertEqual(result['code'],'identity_mismatch')

    def test_another_operator_cannot_release_claimed_funds(self):
        _,p=self.request('/api/v1/payouts','POST',{'email':'user@example.com','amount_cents':500},token='user',key='claim')
        base='/api/v1/admin/payouts/'+p['id']
        self.assertEqual(self.request(base+'/claim','POST',{},token='staff')[0],200)
        with self.db.connect() as c:
            c.execute("INSERT INTO app.staff_roles(user_id,role,granted_by) VALUES (%s,'payout_processor',%s)",(self.user,self.staff))
        self.assertEqual(self.request(base+'/claim','POST',{},token='user')[0],409)
        code,result=self.request(base+'/reject','POST',{'reason':'not sent','confirmed_unpaid':True},token='user')
        self.assertEqual(code,409,result);self.assertEqual(self.balance(),(200,500))

    def test_reused_capture_evidence_seals_session_for_review(self):
        sid=self.start()['session_id'];first,_=self.bottle(sid);second,_=self.bottle(sid,2)
        evidence={'status':'accepted','capture_event_id':str(uuid4()),'captured_at':utcnow().isoformat()}
        _,(code,result)=self.final(sid,[dict(first,**evidence),dict(second,**evidence)])
        self.assertEqual(code,200,result);self.assertTrue(result['user_logged_out'])
        self.assertEqual(result['collection_status'],'submitted')
        self.assertEqual(result['reconciliation_status'],'needs_review')
        self.assertEqual(self.balance(),(700,0))

    def test_unchanged_source_check_keeps_old_publication_usable_but_not_indefinitely(self):
        from datetime import date
        from recy.refresh import next_midnight
        with self.db.connect() as c:
            c.execute("UPDATE recy.sync_runs SET completed_at='2020-01-01T00:00:00+00:00',source_updated_on='2020-01-01'")
        now=utcnow()
        self.db.record_source_observation(date(2020,1,1),now,next_midnight(now),'America/Vancouver')
        sid=self.start()['session_id']
        _,row=self.bottle(sid)
        self.assertEqual(row['eligibility_status'],'eligible')
        with self.db.connect() as c:c.execute("UPDATE recy.source_monitor SET last_checked_at=now()-interval '3 days'")
        _,row=self.bottle(sid,2)
        self.assertEqual(row['eligibility_status'],'unavailable')
        self.db.record_source_observation(date(2020,1,2),now,next_midnight(now),'America/Vancouver')
        _,row=self.bottle(sid,3)
        self.assertEqual(row['eligibility_status'],'unavailable')
