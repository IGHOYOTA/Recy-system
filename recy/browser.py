"""Same-origin browser adapter. Supabase tokens stay in Secure HttpOnly cookies."""
import os
from http.cookies import SimpleCookie, CookieError

from .http_support import APIError, require

COOKIE_PATH = '/api/v1/browser/'


def dispatch(environ, body, database):
    from .web_api import ApplicationAPI
    original = environ['PATH_INFO']
    target = '/api/v1/' + original[len(COOKIE_PATH):]
    require(not environ.get('HTTP_X_MACHINE_KEY'), 'Machine credentials are not accepted in the browser adapter',403,'forbidden')
    if environ['REQUEST_METHOD'] != 'GET':
        # A custom header requires a successful browser CORS preflight cross-origin.
        require(environ.get('HTTP_X_RECY_BROWSER') == '1', 'Browser request header required',403,'csrf_failed')
    jar = SimpleCookie()
    try:jar.load(environ.get('HTTP_COOKIE',''))
    except CookieError:raise APIError(400,'invalid_cookie','Invalid session cookie') from None
    access=jar['recy_access'].value if 'recy_access' in jar else None
    refresh=jar['recy_refresh'].value if 'recy_refresh' in jar else None
    forwarded=dict(environ,PATH_INFO=target)
    forwarded.pop('HTTP_AUTHORIZATION',None)
    if access:forwarded['HTTP_AUTHORIZATION']='Bearer '+access
    api=ApplicationAPI(database)
    cookies=[]

    def retain(result):
        for name,field in (('recy_access','access_token'),('recy_refresh','refresh_token')):
            if result.get(field):
                cookie=SimpleCookie();cookie[name]=result[field];cookie[name]['path']=COOKIE_PATH
                cookie[name]['httponly']=True;cookie[name]['samesite']='Strict'
                if os.environ.get('RECY_COOKIE_SECURE','1')!='0':cookie[name]['secure']=True
                cookies.append(('Set-Cookie',cookie[name].OutputString()))
        return {k:v for k,v in result.items() if k not in ('access_token','refresh_token')}

    def renew():
        require(refresh,'Sign in to continue',401,'unauthorized')
        _,result=api.dispatch(dict(forwarded,PATH_INFO='/api/v1/auth/refresh',REQUEST_METHOD='POST'),{'refresh_token':refresh})
        forwarded['HTTP_AUTHORIZATION']='Bearer '+result['access_token']
        retain(result)

    if target=='/api/v1/auth/logout':
        require(environ['REQUEST_METHOD']=='POST','Use POST to sign out',405,'method_not_allowed')
        revoked=True
        try:
            if not access and refresh:renew()
            api.dispatch(forwarded,body)
        except APIError:
            # Local sign-out must still remove tokens when the provider is unavailable.
            revoked=False
        status,result=200,{'logged_out':True,'provider_revoked':revoked}
    else:
        if target=='/api/v1/auth/refresh':body={'refresh_token':refresh}
        elif not target.startswith('/api/v1/auth/') and not access and refresh:renew()
        try:
            status,result=api.dispatch(forwarded,body)
        except APIError as exc:
            if exc.status==401 and exc.code!='reauthentication_required' and refresh and not target.startswith('/api/v1/auth/'):
                renew();status,result=api.dispatch(forwarded,body)
            else:raise
    if target=='/api/v1/auth/logout':
        for name in ('recy_access','recy_refresh'):
            cookie=SimpleCookie();cookie[name]='';cookie[name]['path']=COOKIE_PATH;cookie[name]['max-age']=0
            cookie[name]['httponly']=True;cookie[name]['samesite']='Strict'
            if os.environ.get('RECY_COOKIE_SECURE','1')!='0':cookie[name]['secure']=True
            cookies.append(('Set-Cookie',cookie[name].OutputString()))
    return status,retain(result),cookies
