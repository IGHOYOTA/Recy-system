"""Bounded, parameterized filtering for portal lists."""
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import parse_qs
from .http_support import require


def filters(environ, date_column, status_column):
    query=parse_qs(environ.get('QUERY_STRING',''))
    clauses=[]
    args=[]
    dates={}
    for key,operator in (('from','>='),('to','<')):
        value=query.get(key,[''])[0]
        if value:
            try:parsed=date.fromisoformat(value)
            except ValueError:
                require(False,'Use YYYY-MM-DD date filters')
            dates[key]=parsed
            clauses.append(date_column+operator+'%s')
            require(key!='to' or parsed<date.max,'To date is outside the supported range')
            boundary=parsed+timedelta(days=1) if key=='to' else parsed
            args.append(datetime.combine(boundary,time.min,tzinfo=timezone.utc))
    require(not dates.get('from') or not dates.get('to') or dates['from']<=dates['to'],'From date must not exceed to date')
    state=query.get('status',[''])[0]
    if state:
        require(len(state)<=50,'Status filter too long')
        clauses.append(status_column+'=%s');args.append(state)
    return ''.join(' AND '+clause for clause in clauses),args
