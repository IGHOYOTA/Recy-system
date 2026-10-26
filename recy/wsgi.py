"""Production JSON WSGI entrypoint. No schema changes occur on request/startup."""
import hmac
import json
import logging
import os
from datetime import date, datetime
from decimal import Decimal
from http import HTTPStatus
from uuid import UUID, uuid4

import psycopg

from .backend import get_database
from .http_support import APIError, require

MAX_BODY=2_000_000


def json_value(value):
    if isinstance(value,UUID):return str(value)
    if isinstance(value,(datetime,date)):return value.isoformat()
    if isinstance(value,Decimal):return int(value) if value==value.to_integral_value() else str(value)
    raise TypeError('Unsupported response value')


def read_body(environ):
    try:length=int(environ.get('CONTENT_LENGTH') or '0')
    except ValueError:raise APIError(400,'invalid_request','Invalid Content-Length') from None
    require(0<=length<=MAX_BODY,'Request body exceeds 2 MB',413,'body_too_large')
    if not length:return {}
    require(environ.get('CONTENT_TYPE','').split(';')[0].strip().lower()=='application/json',
            'Use Content-Type: application/json',415,'unsupported_media_type')
    raw=environ['wsgi.input'].read(length)
    require(len(raw)==length,'Incomplete request body')
    def pairs(items):
        result={}
        for k,v in items:
            if k in result:raise ValueError('duplicate key')
            result[k]=v
        return result
    try:
        body=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    except (ValueError,UnicodeError):raise APIError(400,'invalid_json','Body must be valid JSON without duplicate keys') from None
    require(isinstance(body,dict),'JSON body must be an object')
    return body


def application(environ,start_response):
    path=environ.get('PATH_INFO','/')
    method=environ.get('REQUEST_METHOD','GET')
    request_id=str(uuid4())
    status,body=404,{'error':'Route not found'}
    extra=[]
    origin=environ.get('HTTP_ORIGIN')
    try:
        if origin:
            allowed=[s.strip() for s in os.environ.get('RECY_ALLOWED_ORIGINS','').split(',') if s.strip()]
            require(origin in allowed,'Origin is not allowed',403,'origin_forbidden')
            extra.extend([('Access-Control-Allow-Origin',origin),('Vary','Origin')])
        if method=='OPTIONS':
            require(origin is not None,'CORS Origin required')
            status,body=200,{}
            extra.extend([('Access-Control-Allow-Methods','GET, POST, PATCH, OPTIONS'),
                          ('Access-Control-Allow-Headers','Authorization, Content-Type, Idempotency-Key, X-Machine-Key'),
                          ('Access-Control-Max-Age','600')])
        elif path in ('/health','/ready','/api/v1/schema') or path.startswith('/api/v1/upcs/'):
            if method!='GET':status,body=405,{'error':'Method not allowed'}
            elif path=='/health':status,body=200,{'status':'ok'}
            else:
                key=os.environ.get('RECY_API_KEY','')
                auth=environ.get('HTTP_AUTHORIZATION','')
                authorized=not key or hmac.compare_digest(auth.encode(),('Bearer '+key).encode())
                if path.startswith('/api/') and not authorized:status,body=401,{'error':'Unauthorized'}
                elif path=='/ready':
                    latest=get_database().status();status,body=(200 if latest else 503),{'ready':latest is not None}
                elif path=='/api/v1/schema':
                    latest=get_database().status()
                    status,body=(200,{'table':'registered_products','columns':latest['headers'],
                                     'storage_type':'TEXT','source_url':latest['source_url']}) if latest else (503,{'error':'Registry has not been synced'})
                else:
                    from urllib.parse import unquote
                    result=get_database().verify(unquote(path[len('/api/v1/upcs/'):]))
                    status,body=(200,result) if result is not None else (503,{'error':'Registry has not been synced'})
        elif path=='/api/v1/openapi.json' and method=='GET':
            from .openapi import specification
            status,body=200,specification()
        elif path.startswith(('/api/v1/auth/','/api/v1/me','/api/v1/wallet','/api/v1/payouts',
                              '/api/v1/collection-sessions','/api/v1/machines/','/api/v1/support-cases','/api/v1/admin/',
                              '/api/v1/notifications','/auth/','/collection-sessions','/me','/wallet','/payouts','/machines/','/support-cases','/admin/','/notifications')):
            from .web_api import ApplicationAPI
            status,body=ApplicationAPI(get_database()).dispatch(environ,read_body(environ))
    except APIError as exc:
        status,body=exc.status,{'error':exc.message,'code':exc.code,'request_id':request_id}
    except (psycopg.errors.UniqueViolation,psycopg.errors.CheckViolation,psycopg.errors.ForeignKeyViolation):
        status,body=409,{'error':'The operation conflicts with the current record state','code':'state_conflict','request_id':request_id}
    except ValueError:
        status,body=400,{'error':'Invalid request value','code':'invalid_request','request_id':request_id}
    except Exception as exc:
        # SQL errors can contain row values; never log request bodies, tokens, or raw exceptions.
        logging.error('API request %s failed (%s)',request_id,type(exc).__name__)
        status,body=503,{'error':'Service temporarily unavailable','code':'service_unavailable','request_id':request_id}
    encoded=json.dumps(body,ensure_ascii=False,default=json_value,allow_nan=False).encode('utf-8')
    start_response('%s %s'%(status,HTTPStatus(status).phrase),
                   [('Content-Type','application/json; charset=utf-8'),('Content-Length',str(len(encoded))),
                    ('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),('X-Request-ID',request_id)]+extra)
    return [encoded]
