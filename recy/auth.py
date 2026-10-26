"""Stateless Supabase Auth HTTP adapter; no global client session or token storage."""
import base64
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .http_support import APIError, identifier, require, utcnow


class SupabaseAuth:
    def __init__(self):
        self.url = os.environ.get('SUPABASE_URL', '').rstrip('/')
        self.key = os.environ.get('SUPABASE_PUBLISHABLE_KEY') or os.environ.get('SUPABASE_ANON_KEY')
        if not self.key or urlsplit(self.url).scheme != 'https' or not urlsplit(self.url).hostname:
            raise APIError(503, 'auth_not_configured', 'Supabase Auth URL and publishable key must be configured')

    def call(self, path, method='POST', payload=None, token=None):
        headers = {'apikey': self.key, 'Content-Type': 'application/json', 'Accept': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        request = Request(self.url + '/auth/v1/' + path, method=method, headers=headers,
                          data=json.dumps(payload).encode() if payload is not None else None)
        try:
            with urlopen(request, timeout=15) as response:
                raw = response.read(1_000_001)
                require(len(raw) <= 1_000_000, 'Auth response too large', 502, 'auth_unavailable')
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            # Provider text can expose account existence or submitted values. Return fixed messages.
            status = 429 if exc.code == 429 else 503 if exc.code >= 500 else 401 if path.startswith('token') or path == 'user' else 400
            exc.close()
            raise APIError(status, 'auth_rate_limited' if status == 429 else 'auth_failed',
                           'Authentication request could not be completed') from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise APIError(503, 'auth_unavailable', 'Authentication service temporarily unavailable') from None

    def login(self, identity, password):
        field = 'email' if '@' in identity else 'phone'
        return self.call('token?grant_type=password', payload={field: identity, 'password': password})

    def verify_user(self, token):
        # Never trust an unverified local JWT decode; the provider verifies it first.
        user = self.call('user', 'GET', token=token)
        try:
            segment = token.split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(segment + '=' * (-len(segment) % 4)))
            require(claims.get('sub') == user.get('id') and claims.get('exp', 0) > utcnow().timestamp(),
                    'Invalid or expired session', 401, 'unauthorized')
            session_id = identifier(claims.get('session_id'), 'auth session')
            user_id = identifier(user.get('id'), 'auth user')
        except (ValueError, IndexError, TypeError, KeyError, APIError):
            raise APIError(401, 'unauthorized', 'Invalid or expired session') from None
        return user, user_id, session_id

    def logout(self, token):
        return self.call('logout?scope=local', token=token)
