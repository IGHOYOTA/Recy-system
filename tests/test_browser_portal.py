import io
import json
import os
import unittest
from unittest.mock import patch

from tests import test_application_api as fixtures
from recy.wsgi import application
from recy.http_support import APIError


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'),'Disposable PostgreSQL required')
class BrowserPortalTests(unittest.TestCase):
    setUp=fixtures.ApplicationAPITests.setUp
    tearDown=fixtures.ApplicationAPITests.tearDown
    post=fixtures.ApplicationAPITests.post
    balance=fixtures.ApplicationAPITests.balance
    request=fixtures.ApplicationAPITests.request

    def browser(self,path,method='GET',body=None,cookie='',header=True,origin='https://portal.example.com'):
        raw=json.dumps(body).encode() if body is not None else b''
        env={'PATH_INFO':'/api/v1/browser'+path,'REQUEST_METHOD':method,'HTTP_HOST':'portal.example.com',
             'HTTP_ORIGIN':origin,'wsgi.url_scheme':'https','wsgi.input':io.BytesIO(raw),'CONTENT_LENGTH':str(len(raw)),
             'CONTENT_TYPE':'application/json','HTTP_COOKIE':cookie,'REMOTE_ADDR':'127.0.0.1'}
        if header:env['HTTP_X_RECY_BROWSER']='1'
        captured=[]
        result=b''.join(application(env,lambda status,headers:captured.append((status,headers))))
        return int(captured[0][0].split()[0]),json.loads(result),captured[0][1]

    def test_browser_login_uses_secure_httponly_cookies_not_json_tokens(self):
        code,result,headers=self.browser('/auth/login','POST',{'email':'user@example.com','password':'correct-password'})
        self.assertEqual(code,200,result)
        self.assertNotIn('access_token',result);self.assertNotIn('refresh_token',result)
        cookies=[v for k,v in headers if k=='Set-Cookie'];self.assertEqual(len(cookies),2)
        self.assertTrue(all('HttpOnly' in c and 'Secure' in c and 'SameSite=Strict' in c for c in cookies))
        cookie='; '.join(c.split(';')[0] for c in cookies)
        code,result,_=self.browser('/me',cookie=cookie)
        self.assertEqual(code,200,result);self.assertEqual(result['roles'],[])
        self.assertEqual(result['user']['full_name'],'Test User')
        code,result,headers=self.browser('/auth/logout','POST',{},cookie=cookie)
        self.assertEqual(code,200,result)
        self.assertTrue(all('Max-Age=0' in v for k,v in headers if k=='Set-Cookie'))
        self.assertEqual(self.browser('/me',cookie=cookie)[0],401)

    def test_csrf_and_untrusted_origins_cannot_mutate_with_cookies(self):
        self.assertEqual(self.browser('/payouts','POST',{'email':'user@example.com','amount_cents':500},cookie='recy_access=user',header=False)[0],403)
        self.assertEqual(self.browser('/auth/login','POST',{},origin='https://evil.example.com')[0],403)
        self.assertEqual(self.balance(),(700,0))

    def test_browser_can_sign_out_locally_during_auth_provider_outage(self):
        with patch.object(self.auth,'logout',side_effect=APIError(503,'auth_unavailable','Unavailable')):
            code,result,headers=self.browser('/auth/logout','POST',{},cookie='recy_access=user')
        self.assertEqual(code,200,result);self.assertTrue(result['logged_out'])
        self.assertFalse(result['provider_revoked'])
        self.assertEqual(len([v for k,v in headers if k=='Set-Cookie' and 'Max-Age=0' in v]),2)

    def test_browser_refresh_rotates_cookies_without_exposing_tokens(self):
        with patch.object(self.auth,'call',return_value={'access_token':'user','refresh_token':'replacement','expires_in':3600}):
            code,result,headers=self.browser('/me',cookie='recy_refresh=old-refresh')
        self.assertEqual(code,200,result);self.assertNotIn('access_token',result)
        self.assertEqual(len([v for k,v in headers if k=='Set-Cookie']),2)

    def test_wallet_activity_groups_journal_entries_and_filters_before_pagination(self):
        self.request('/api/v1/payouts','POST',{'email':'user@example.com','amount_cents':500},token='user',key='hold')
        code,result=self.request('/api/v1/wallet/activity?limit=1&offset=0&status=payout_reserve',token='user')
        self.assertEqual(code,200,result);self.assertEqual(len(result['items']),1)
        row=result['items'][0]
        self.assertEqual(row['available_delta_cents'],-500);self.assertEqual(row['reserved_delta_cents'],500)
        self.assertEqual(row['available_after_cents'],200)
        self.assertEqual(self.request('/api/v1/payouts?from=invalid',token='user')[0],400)
        self.assertEqual(self.request('/api/v1/payouts?status=paid',token='user')[1]['items'],[])
        self.assertEqual(self.request('/api/v1/payouts?from=2030-01-01&to=2020-01-01',token='user')[0],400)

    def test_public_frontend_and_assets_have_csp_and_no_secrets(self):
        for path,mime in [('/','text/html'),('/assets/app.js','text/javascript'),('/assets/styles.css','text/css')]:
            response=[]
            result=b''.join(application({'PATH_INFO':path,'REQUEST_METHOD':'GET'},lambda status,headers:response.append((status,dict(headers)))))
            self.assertTrue(response[0][0].startswith('200'))
            self.assertIn(mime,response[0][1]['Content-Type'])
            self.assertIn("frame-ancestors 'none'",response[0][1]['Content-Security-Policy'])
            self.assertNotIn(b'postgresql://',result);self.assertNotIn(b'sb_publishable_',result)
