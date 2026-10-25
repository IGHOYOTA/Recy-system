"""Production WSGI entrypoint for Gunicorn; no schema changes on API startup."""
import hmac
import json
import logging
import os
from http import HTTPStatus
from urllib.parse import unquote

from .backend import get_database


def application(environ, start_response):
    path = environ.get('PATH_INFO', '/')
    status, body = 404, {'error': 'Route not found'}
    try:
        if environ.get('REQUEST_METHOD') != 'GET':
            status, body = 405, {'error': 'Method not allowed'}
        elif path == '/health':
            status, body = 200, {'status': 'ok'}
        else:
            key = os.environ.get('RECY_API_KEY', '')
            authorized = not key or hmac.compare_digest(environ.get('HTTP_AUTHORIZATION', ''), 'Bearer ' + key)
            if path.startswith('/api/') and not authorized:
                status, body = 401, {'error': 'Unauthorized'}
            elif path == '/ready':
                latest = get_database().status()
                status, body = (200 if latest else 503), {'ready': latest is not None}
            elif path == '/api/v1/schema':
                latest = get_database().status()
                if latest:
                    status, body = 200, {'table': 'registered_products', 'columns': latest['headers'],
                                         'storage_type': 'TEXT', 'source_url': latest['source_url']}
                else:
                    status, body = 503, {'error': 'Registry has not been synced'}
            elif path.startswith('/api/v1/upcs/'):
                result = get_database().verify(unquote(path[len('/api/v1/upcs/'):]))
                status, body = (200, result) if result is not None else (503, {'error': 'Registry has not been synced'})
    except ValueError as exc:
        status, body = 400, {'error': str(exc)}
    except Exception:
        logging.exception('API database operation failed')
        status, body = 503, {'error': 'Registry temporarily unavailable'}
    encoded = json.dumps(body, ensure_ascii=False).encode('utf-8')
    start_response('%s %s' % (status, HTTPStatus(status).phrase),
                   [('Content-Type', 'application/json; charset=utf-8'),
                    ('Content-Length', str(len(encoded))), ('Cache-Control', 'no-store')])
    return [encoded]
