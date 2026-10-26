"""Shared HTTP validation. Exceptions never carry credentials or raw request bodies."""
import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import UUID


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def require(condition, message, status=400, code='invalid_request'):
    if not condition:
        raise APIError(status, code, message)


def text(value, name, maximum=250):
    require(isinstance(value, str) and 0 < len(value.strip()) <= maximum,
            '%s must be nonempty text (maximum %s characters)' % (name, maximum))
    return value.strip()


def identifier(value, name='id'):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise APIError(400, 'invalid_request', name + ' must be a UUID') from None


def integer(value, name, minimum=0, maximum=2**53-1):
    require(type(value) is int and minimum <= value <= maximum,
            '%s must be an integer between %s and %s' % (name, minimum, maximum))
    return value


def email(value):
    value = text(value, 'email', 254)
    require(re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value) is not None, 'Invalid email address')
    return value


def phone(value):
    value = text(value, 'phone', 16)
    require(re.fullmatch(r'\+[1-9][0-9]{6,14}', value) is not None,
            'Use an international phone number, for example +16045550123')
    return value


def timestamp(value, name):
    require(isinstance(value, str), name + ' must be an ISO timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(parsed.tzinfo is not None, name + ' must include a timezone')
        return parsed
    except ValueError:
        raise APIError(400, 'invalid_request', name + ' must be an ISO timestamp') from None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     default=str, allow_nan=False).encode()).hexdigest()


def utcnow():
    return datetime.now(timezone.utc)
