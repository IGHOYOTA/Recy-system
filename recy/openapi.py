"""Discoverable API surface. Detailed workflows and examples live in docs/api.md."""

ROUTES={
    '/auth/signup':['post'], '/auth/login':['post'], '/auth/refresh':['post'],
    '/auth/logout':['post'], '/auth/recover':['post'], '/auth/verify':['post'],
    '/auth/phone':['post'], '/auth/email':['post'], '/auth/password':['post'],
    '/me':['get','patch'], '/wallet':['get'], '/wallet/transactions':['get'],
    '/me/summary':['get'], '/wallet/activity':['get'],
    '/machines/fill-reports':['post'],
    '/admin/overview':['get'], '/admin/fleet':['get'], '/admin/fleet/{machine_id}':['get'],
    '/admin/team':['get'], '/admin/audit-events':['get'],
    '/admin/deposit-reviews':['get'], '/admin/deposit-reviews/{session_id}':['get'],
    '/admin/deposit-reviews/{session_id}/claim':['post'], '/admin/deposit-reviews/{session_id}/resolve':['post'],
    '/admin/depots':['get','post'], '/admin/depots/{depot_id}':['patch'],
    '/admin/evacuations':['get','post'], '/admin/evacuations/{evacuation_id}':['get'],
    '/admin/evacuations/{evacuation_id}/collect':['post'], '/admin/evacuations/{evacuation_id}/transit':['post'],
    '/admin/evacuations/{evacuation_id}/deliver':['post'], '/admin/evacuations/{evacuation_id}/verify':['post'],
    '/admin/evacuations/{evacuation_id}/review':['post'], '/admin/evacuations/{evacuation_id}/cancel':['post'],
    '/collection-sessions':['get','post'], '/collection-sessions/{session_id}':['get'],
    '/collection-sessions/{session_id}/bottles':['get','post'],
    '/collection-sessions/{session_id}/bottles/{machine_event_id}/capture':['post'],
    '/collection-sessions/{session_id}/submit':['post'],
    '/machines/heartbeat':['post'], '/machines/sync-batches':['post'],
    '/payouts':['get','post'], '/payouts/{payout_id}':['get'], '/payouts/{payout_id}/cancel':['post'],
    '/support-cases':['get','post'], '/support-cases/{case_id}':['get'],
    '/notifications':['get'], '/notifications/{notification_id}/read':['post'],
    '/admin/collection-sessions':['get'], '/admin/collection-sessions/{session_id}':['get'],
    '/admin/collection-sessions/{session_id}/bottles':['get'],
    '/admin/collection-sessions/{session_id}/reconcile':['post'],
    '/admin/operational-alerts':['get'], '/admin/operational-alerts/{alert_id}':['patch'],
    '/admin/machine-incidents':['get'], '/admin/machine-incidents/{incident_id}':['patch'],
    '/admin/machines':['get','post'], '/admin/machines/{machine_id}':['patch'],
    '/admin/machines/{machine_id}/credentials':['post'],
    '/admin/machines/{machine_id}/credentials/{credential_id}/revoke':['post'],
    '/admin/staff-roles':['post'], '/admin/payouts':['get'], '/admin/payouts/{payout_id}':['get'],
    '/admin/payouts/{payout_id}/claim':['post'], '/admin/payouts/{payout_id}/review':['post'],
    '/admin/payouts/{payout_id}/complete':['post'], '/admin/payouts/{payout_id}/reject':['post'],
    '/admin/support-cases':['get'], '/admin/support-cases/{case_id}':['patch'],
    '/admin/wallet-adjustments':['get','post'], '/admin/wallet-adjustments/{adjustment_id}/approve':['post'],
    '/admin/operations':['get'], '/admin/jobs/run':['post']
}


def object_schema(properties, required=()):
    return {'type':'object','properties':properties,'required':list(required)}


def request_schemas():
    string={'type':'string'}
    uuid={'type':'string','format':'uuid'}
    date={'type':'string','format':'date-time'}
    contact={'type':'string','format':'email'}
    phone={'type':'string','pattern':r'^\+[1-9][0-9]{6,14}$'}
    password={'type':'string','minLength':8,'maxLength':1024,'writeOnly':True}
    login=object_schema({'identifier':string,'email':contact,'phone':phone,'password':password},['password'])
    login['anyOf']=[{'required':[field]} for field in ('identifier','email','phone')]
    scan=object_schema({'machine_event_id':uuid,'sequence_no':{'type':'integer','minimum':1,'maximum':100000},
                        'upc':{'type':'string','pattern':'^[0-9]{4,18}$'},'observed_at':date},
                       ['machine_event_id','sequence_no','upc','observed_at'])
    capture=object_schema({'status':{'type':'string','enum':['accepted','rejected']},
                           'capture_event_id':uuid,'captured_at':date,'reason_code':string},['status'])
    capture['description']='Accepted status requires capture_event_id and captured_at, unless evidence is already stored.'
    final_row=object_schema(dict(scan['properties'],**capture['properties']),['machine_event_id','sequence_no','upc','status'])
    final=object_schema({'machine_end_event_id':uuid,'final_sequence_no':{'type':'integer','minimum':0,'maximum':5000},
                         'end_reason':{'type':'string','enum':['inactivity','machine_fault','restart','admin_recovery']},
                         'machine_ended_at':date,'bottles':{'type':'array','maxItems':5000,'items':final_row}},
                        ['machine_end_event_id','final_sequence_no','bottles'])
    batch_row=object_schema(dict(scan['properties'],**capture['properties']),scan['required'])
    return {
        '/auth/signup':object_schema({'first_name':string,'last_name':string,'email':contact,'phone':phone,'password':password},
                                     ['first_name','last_name','email','phone','password']),
        '/auth/login':login,'/collection-sessions':login,
        '/auth/refresh':object_schema({'refresh_token':dict(string,writeOnly=True)},['refresh_token']),
        '/auth/recover':object_schema({'email':contact},['email']),
        '/auth/phone':object_schema({'phone':phone},['phone']),
        '/auth/email':object_schema({'email':contact},['email']),
        '/auth/password':object_schema({'password':password},['password']),
        '/auth/verify':object_schema({'type':{'type':'string','enum':['email','recovery','phone_change']},
                                     'token':dict(string,writeOnly=True),'email':contact,'phone':phone},['type','token']),
        '/me':object_schema({'first_name':string,'last_name':string},['first_name','last_name']),
        '/collection-sessions/{session_id}/bottles':scan,
        '/collection-sessions/{session_id}/bottles/{machine_event_id}/capture':capture,
        '/collection-sessions/{session_id}/submit':final,
        '/machines/heartbeat':object_schema({'firmware_version':string}),
        '/machines/sync-batches':object_schema({'session_id':uuid,'device_batch_id':uuid,'machine_boot_id':uuid,
                                               'bottles':{'type':'array','minItems':1,'maxItems':500,'items':batch_row}},
                                              ['session_id','device_batch_id','machine_boot_id','bottles']),
        '/support-cases':object_schema({'category':{'type':'string','enum':['missing_credit','bottle_dispute','payout_problem','account_recovery']},
                                       'description':string,'session_id':uuid,'bottle_event_id':uuid,'payout_id':uuid},['category','description']),
        '/admin/payouts/{payout_id}/complete':object_schema({'payment_reference':string,'paid_at':date},['payment_reference','paid_at']),
        '/admin/payouts/{payout_id}/reject':object_schema({'reason':string,'confirmed_unpaid':{'type':'boolean','enum':[True]}},['reason','confirmed_unpaid']),
        '/admin/payouts/{payout_id}/review':object_schema({'reason':string},['reason']),
        '/admin/machines':object_schema({'machine_code':string,'name':string,'location':string,
                                        'status':{'type':'string','enum':['active','maintenance','disabled']},
                                        'operating_mode':{'type':'string','enum':['live','test']}},['machine_code','name']),
        '/admin/machines/{machine_id}':object_schema({'status':{'type':'string','enum':['active','maintenance','disabled']},
                                                     'operating_mode':{'type':'string','enum':['live','test']}}),
        '/admin/staff-roles':object_schema({'user_id':uuid,'role':{'type':'string','enum':['administrator','payout_processor','operations_reviewer','collector','depot_receiver']},
                                           'revoke':{'type':'boolean'}},['user_id','role']),
        '/admin/wallet-adjustments':object_schema({'wallet_id':uuid,'amount_cents':{'type':'integer'},
                                                  'reason_code':string,'support_case_id':uuid},['wallet_id','amount_cents','reason_code']),
        '/admin/jobs/run':object_schema({'limit':{'type':'integer','minimum':1,'maximum':20}}),
        '/admin/support-cases/{case_id}':object_schema({'status':{'type':'string','enum':['open','investigating','resolved','closed']},
                                                       'resolution_code':string},['status']),
        '/admin/operational-alerts/{alert_id}':object_schema({'status':{'type':'string','enum':['open','acknowledged','resolved']}},['status']),
        '/admin/machine-incidents/{incident_id}':object_schema({'status':{'type':'string','enum':['open','investigating','resolved']},
                                                               'reason':string},['status','reason'])
    }


def specification():
    paths={}
    schemas=request_schemas()
    uuid={'type':'string','format':'uuid'}
    text={'type':'string'}
    schemas.update({
        '/machines/fill-reports':object_schema({'machine_event_id':uuid,'cycle_id':uuid,'observed_at':{'type':'string','format':'date-time'},
            'level':{'type':'string','enum':['normal','almost_full','full']},'percent':{'type':'integer','minimum':0,'maximum':100}},['machine_event_id','cycle_id','observed_at','level']),
        '/admin/deposit-reviews/{session_id}/resolve':object_schema({'resolution':{'type':'string','enum':['approved','rejected']},
            'reason':text,'accepted_event_ids':{'type':'array','items':uuid,'uniqueItems':True}},['resolution','reason']),
        '/admin/depots':object_schema({'code':text,'name':text,'address':text,'contact':text},['code','name','address']),
        '/admin/depots/{depot_id}':object_schema({'active':{'type':'boolean'}},['active']),
        '/admin/evacuations':object_schema({'machine_id':uuid,'depot_id':uuid,'collector_id':uuid,'scheduled_at':{'type':'string','format':'date-time'},'notes':text},['machine_id','depot_id','scheduled_at']),
        '/admin/evacuations/{evacuation_id}/collect':object_schema({'emptying_confirmed':{'type':'boolean','enum':[True]},'seal_reference':text},['emptying_confirmed']),
        '/admin/evacuations/{evacuation_id}/deliver':object_schema({'depot_id':uuid}),
        '/admin/evacuations/{evacuation_id}/verify':object_schema({'received_count':{'type':'integer','minimum':0},'receipt_reference':text,'reason':text},['received_count','receipt_reference']),
        '/admin/evacuations/{evacuation_id}/review':object_schema({'reason':text},['reason']),
        '/admin/evacuations/{evacuation_id}/cancel':object_schema({'reason':text},['reason'])
    })
    for path,methods in ROUTES.items():
        item={}
        for method in methods:
            machine=path.startswith('/machines/') or (path.startswith('/collection-sessions') and method=='post')
            public=path in ('/auth/signup','/auth/login','/auth/refresh','/auth/recover','/auth/verify')
            op={'summary':method.upper()+' '+path,
                'security':[] if public else [{'machineKey':[]}] if machine else ([{'userJWT':[]},{'machineKey':[]}] if path.startswith('/collection-sessions/') else [{'userJWT':[]}]),
                'responses':{str(code):{'description':description} for code,description in
                             ((200,'Successful response'),(201,'Created'),(202,'Verification pending'),
                              (400,'Invalid request'),(401,'Authentication required'),(403,'Not authorized'),
                              (404,'Not found'),(409,'State or idempotency conflict'),(429,'Rate limited'),(503,'Dependency unavailable'))},
                'parameters':[{'in':'path','name':segment[1:-1],'required':True,'schema':{'type':'string','format':'uuid'}}
                              for segment in path.split('/') if segment.startswith('{')]}
            if method in ('post','patch'):
                op['requestBody']={'content':{'application/json':{'schema':schemas.get(path,{'type':'object'})}}}
            if method=='get' and '{' not in path:
                op['parameters'] += [{'in':'query','name':'limit','schema':{'type':'integer','minimum':1,'maximum':100}},
                                     {'in':'query','name':'offset','schema':{'type':'integer','minimum':0}}]
            if path=='/auth/login':
                op['parameters'] += [{'in':'header','name':'X-Machine-Key','schema':{'type':'string'},
                                      'description':'Optional: exchange this fresh login for a collection-only session.'},
                                     {'in':'header','name':'Idempotency-Key','schema':{'type':'string','maxLength':150},
                                      'description':'Required when X-Machine-Key is supplied.'}]
            if (method=='post' and path in ('/payouts','/collection-sessions','/collection-sessions/{session_id}/submit',
                                            '/support-cases','/admin/machines','/admin/machines/{machine_id}/credentials','/admin/wallet-adjustments',
                                            '/admin/depots','/admin/evacuations','/admin/deposit-reviews/{session_id}/claim','/admin/deposit-reviews/{session_id}/resolve')) or (method=='post' and path.startswith('/admin/evacuations/')):
                op['parameters'].append({'in':'header','name':'Idempotency-Key','required':True,'schema':{'type':'string','maxLength':150}})
            if path=='/payouts' and method=='post':
                op['requestBody']={'required':True,'content':{'application/json':{'schema':{'type':'object',
                    'required':['email','amount_cents'],'additionalProperties':False,
                    'properties':{'email':{'type':'string','format':'email'},'amount_cents':{'type':'integer','minimum':500}}}}}}
            item[method]=op
        paths[path]=item
    return {'openapi':'3.0.3','info':{'title':'Recy application API','version':'0.2.0',
                                    'description':'See docs/api.md for exact request contracts. CAD amounts use integer cents.'},
            'servers':[{'url':'/api/v1'}],'paths':paths,
            'components':{'securitySchemes':{'userJWT':{'type':'http','scheme':'bearer','bearerFormat':'JWT'},
                                            'machineKey':{'type':'apiKey','in':'header','name':'X-Machine-Key'}}}}
