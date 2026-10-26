import base64
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from uuid import uuid4

from recy.auth import SupabaseAuth
from recy.http_support import APIError, utcnow


class AuthTransportTests(unittest.TestCase):
    def setUp(self):
        self.patch=patch.dict(os.environ,{'SUPABASE_URL':'https://example.supabase.co','SUPABASE_PUBLISHABLE_KEY':'test-key'})
        self.patch.start();self.addCleanup(self.patch.stop)
        self.auth=SupabaseAuth()

    def token(self,**overrides):
        claims={'sub':str(uuid4()),'session_id':str(uuid4()),'exp':utcnow().timestamp()+3600}
        claims.update(overrides)
        return 'header.'+base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')+'.signature',claims

    def test_password_and_identity_forwarded_exactly(self):
        for identity,field in [('user@example.com','email'),('+16045550123','phone')]:
            with patch('recy.auth.urlopen',return_value=io.BytesIO(b'{"access_token":"token"}')) as transport:
                self.auth.login(identity,' password with spaces ')
                request=transport.call_args[0][0]
                self.assertEqual(request.full_url,'https://example.supabase.co/auth/v1/token?grant_type=password')
                self.assertEqual(json.loads(request.data),{field:identity,'password':' password with spaces '})
                self.assertEqual(request.get_header('Apikey'),'test-key')

    def test_provider_verifies_before_claims_are_used(self):
        token,claims=self.token()
        with patch.object(self.auth,'call',return_value={'id':claims['sub']}) as call:
            user,uid,sid=self.auth.verify_user(token)
            call.assert_called_once_with('user','GET',token=token)
            self.assertEqual(str(uid),claims['sub']);self.assertEqual(str(sid),claims['session_id'])
        with patch.object(self.auth,'call',side_effect=APIError(401,'auth_failed','Denied')):
            with self.assertRaises(APIError):self.auth.verify_user(token)

    def test_expired_mismatched_and_malformed_claims_rejected(self):
        for token,claims in [self.token(exp=0),self.token(),('malformed',{'sub':str(uuid4())})]:
            with patch.object(self.auth,'call',return_value={'id':str(uuid4())}):
                with self.assertRaises(APIError) as raised:self.auth.verify_user(token)
                self.assertEqual(raised.exception.status,401)

    def test_provider_errors_never_expose_credentials(self):
        for error,status in [(HTTPError('https://example',400,'secret password',{},io.BytesIO(b'sensitive')),401),
                             (HTTPError('https://example',429,'sensitive',{},io.BytesIO(b'sensitive')),429),
                             (URLError('sensitive'),503)]:
            with patch('recy.auth.urlopen',side_effect=error):
                with self.assertRaises(APIError) as raised:self.auth.login('user@example.com','secret password')
                self.assertEqual(raised.exception.status,status)
                self.assertNotIn('secret',raised.exception.message)

    def test_logout_only_revokes_current_session(self):
        with patch.object(self.auth,'call',return_value={}) as call:
            self.auth.logout('token')
            call.assert_called_once_with('logout?scope=local',token='token')
