"""Generate real n8n 2.40.5 workflows. Exports contain references, never credentials."""
import json
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'workflows' / 'n8n'
OUT.mkdir(parents=True, exist_ok=True)
BASE = 'n8n-nodes-base.'
AI = '@n8n/n8n-nodes-langchain.'
IDS = {f'WF{i:02d}': f'haWF{i:02d}LocalFlow' for i in range(10)}
IDS.update({f'SW{i:02d}': f'haSW{i:02d}LocalTool' for i in range(8)})
CRED_BACK = {'httpHeaderAuth': {'id': 'haBackendCred001', 'name': 'HackAlem backend'}}
CRED_EVENT = {'httpHeaderAuth': {'id': 'haEventCred00001', 'name': 'HackAlem event ingress'}}
CRED_OLLAMA = {'ollamaApi': {'id': 'haOllamaCred0001', 'name': 'HackAlem Ollama local'}}
TOOL_NAMES = ['smoke_echo', 'search_reference', 'resolve_participant', 'normalize_deadline', 'get_evidence', 'get_action_candidates', 'search_meetings', 'get_confirmed_actions']
TOOL_IDS = {name: IDS[f'SW{i:02d}'] for i, name in enumerate(TOOL_NAMES)}
TOOLS = {
 'extract': ['get_evidence', 'resolve_participant', 'normalize_deadline', 'search_reference'],
 'reconcile': ['get_evidence', 'normalize_deadline'],
 'summary': ['get_evidence', 'search_reference'],
 'verify': ['get_evidence', 'get_action_candidates'],
 'qa': ['search_meetings', 'get_evidence', 'get_confirmed_actions', 'search_reference'],
}
DESCRIPTIONS = {
 'search_reference': 'Find approved reference definitions. arguments_json: {"query":"term", "limit":3}. References do not prove meeting decisions.',
 'resolve_participant': 'Resolve a mentioned participant. arguments_json: {"mentioned_name":"name", "context_segment_ids":["source-id"]}. Never assign the current speaker by default.',
 'normalize_deadline': 'Propose a date from an exact source phrase. arguments_json: {"due_raw":"к пятнице", "source_segment_ids":["source-id"]}. Unknown dates remain null.',
 'get_evidence': 'Read exact source segments in this meeting. arguments_json: {"segment_ids":["source-id"]}. Use IDs from input, at most 20.',
 'get_action_candidates': 'Read current candidates. arguments_json: {"candidate_ids":[]}. Empty list returns up to 20 candidates.',
 'search_meetings': 'Search the confirmed meeting with local RAG. arguments_json: {"query":"question", "limit":6}. Return source IDs as citations.',
 'get_confirmed_actions': 'Read confirmed actions. arguments_json: {"status_filter":"all"}. Optional assignee_name.',
}


def node(name, kind, params=None, pos=(0,0), version=1, credentials=None, **extra):
    return {'id': str(uuid.uuid5(uuid.NAMESPACE_URL, name+kind+str(pos))), 'name': name, 'type': kind,
            'typeVersion': version, 'position': list(pos), 'parameters': params or {}, **({'credentials': credentials} if credentials else {}), **extra}


def workflow(code, title, nodes, connections, active=False):
    settings = {'executionOrder': 'v1', 'executionTimeout': 480, 'timezone': 'Asia/Qyzylorda',
                'saveDataSuccessExecution': 'none', 'saveDataErrorExecution': 'none', 'saveManualExecutions': False,
                'callerPolicy': 'workflowsFromSameOwner'}
    if code != 'WF08': settings['errorWorkflow'] = IDS['WF08']
    value = {'id': IDS[code], 'name': f'[HA] {code} | {title}', 'nodes': nodes, 'connections': connections,
             'settings': settings, 'active': active, 'pinData': {}, 'tags': []}
    (OUT / f'{code}.json').write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    return value


def link(edges, source, target, output=0, kind='main'):
    ports = edges.setdefault(source, {}).setdefault(kind, [])
    while len(ports) <= output: ports.append([])
    ports[output].append({'node': target, 'type': kind, 'index': 0})


def http(name, path, body='={{ JSON.stringify($json) }}', pos=(0,0), method='POST', **extra):
    params = {'method': method, 'url': 'http://api:8000'+path, 'authentication': 'genericCredentialType',
              'genericAuthType': 'httpHeaderAuth', 'options': {'timeout': 240000}}
    if method != 'GET': params.update(sendBody=True, specifyBody='json', jsonBody=body)
    return node(name, BASE+'httpRequest', params, pos, 4.3, CRED_BACK, **extra)


def trigger(fields=('scope_token',), name='Input'):
    return node(name, BASE+'executeWorkflowTrigger', {'workflowInputs': {'values': [{'name': f, 'type': 'string'} for f in fields]}}, (0,0), 1.1)


def mapper(values):
    return {'mappingMode': 'defineBelow', 'value': values, 'matchingColumns': [], 'schema': [
        {'id': k, 'displayName': k, 'required': False, 'defaultMatch': False, 'display': True, 'canBeUsedToMatch': True, 'type': 'string'} for k in values],
        'attemptToConvertTypes': False, 'convertFieldsToString': False}


def sub(name, code, values=None, pos=(0,0)):
    return node(name, BASE+'executeWorkflow', {'workflowId': {'__rl': True, 'value': IDS[code], 'mode': 'list'},
                 'workflowInputs': mapper(values or {'scope_token': '={{ $json.scope_token }}'}), 'options': {'waitForSubWorkflow': True}}, pos, 1.3)


def switch(name, field, values, pos):
    rules = []
    for value in values:
        rules.append({'conditions': {'options': {'caseSensitive': True, 'leftValue': '', 'typeValidation': 'strict', 'version': 2},
            'conditions': [{'leftValue': '={{ $json.'+field+' }}', 'rightValue': value, 'operator': {'type': 'string', 'operation': 'equals'}}],
            'combinator': 'and'}, 'renameOutput': True, 'outputKey': value})
    return node(name, BASE+'switch', {'rules': {'values': rules}, 'options': {'fallbackOutput': 'extra'}}, pos, 3.3)


def condition(name, field, pos):
    return node(name, BASE+'if', {'conditions': {'options': {'caseSensitive': True, 'leftValue': '', 'typeValidation': 'strict', 'version': 2},
        'conditions': [{'leftValue': '={{ $json.'+field+' }}', 'rightValue': '', 'operator': {'type': 'boolean', 'operation': 'true', 'singleValue': True}}],
        'combinator': 'and'}, 'options': {}}, pos, 2.2)


def note(content, pos, width=440, height=180):
    return node('Guide '+str(pos), BASE+'stickyNote', {'content': content, 'width': width, 'height': height, 'color': 5}, pos)


def agent_bundle(role, y, edges, smoke=False):
    name = 'Smoke Agent' if smoke else {'extract':'Action Extractor','reconcile':'Action Reconciler','summary':'Summary Writer','verify':'Evidence Reviewer','qa':'Meeting QA'}[role]
    params = {'promptType': 'define', 'text': 'Call smoke_echo with value local-ok. Then report the returned value. /no_think' if smoke else '={{ $json.prompt_input }}',
              'options': {'systemMessage': 'Use the provided tool. Do not pretend to call it.' if smoke else '={{ $json.system_prompt }}',
                          'maxIterations': 4, 'returnIntermediateSteps': smoke, 'enableStreaming': False, 'autoSaveHighlightedData': False}}
    nodes = [node(name, AI+'agent', params, (620,y), 3.1, onError='continueErrorOutput')]
    modelname = f'Ollama {role}'
    nodes.append(node(modelname, AI+'lmChatOllama', {'model': 'qwen3:4b-instruct-2507-q4_K_M' if smoke else "={{ $('Context').first().json.model }}", 'options': {'temperature': 0, 'think': False, 'numCtx': 4096, 'numPredict': 1200, 'keepAlive': '30s', 'numBatch': 256}}, (540,y+180), 1, CRED_OLLAMA))
    link(edges, modelname, name, kind='ai_languageModel')
    names = ['smoke_echo'] if smoke else TOOLS[role]
    for i, toolname in enumerate(names):
        label = toolname if smoke else f'{role}_{toolname}'
        values = {'value': "={{ $fromAI('value', 'Value to echo', 'string') }}"} if smoke else {
            'scope_token': "={{ $('Context').first().json.scope_token }}",
            'arguments_json': "={{ $fromAI('arguments_json', 'JSON object of tool arguments. '+"+json.dumps(DESCRIPTIONS[toolname])+", 'string') }}"}
        params = {'name': toolname, 'description': 'Echo input value. Use for local tool verification.' if smoke else DESCRIPTIONS[toolname],
                  'source': 'database', 'workflowId': {'__rl': True, 'value': TOOL_IDS[toolname], 'mode': 'list'}, 'workflowInputs': mapper(values)}
        nodes.append(node(label, AI+'toolWorkflow', params, (760+i*190,y+190), 2.1))
        link(edges, label, name, kind='ai_tool')
    return nodes, name


all_workflows=[]
for index, name in enumerate(TOOL_NAMES):
    edges={}
    nodes=[trigger(('value',) if index==0 else ('scope_token','arguments_json'))]
    if index==0:
        nodes.append(node('Return local value', BASE+'set', {'mode':'raw','jsonOutput':'={{ JSON.stringify({value: $json.value, source: "local-tool"}) }}','options':{}}, (260,0), 3.4))
    else:
        nodes.append(http('Scoped tool gateway', '/internal/tools/'+name, pos=(260,0)))
    link(edges,'Input',nodes[-1]['name'])
    nodes.append(note('## '+name+'\nScope comes from the parent workflow, never from the language model. Backend checks the current revision and access.', (0,-200)))
    all_workflows.append(workflow(f'SW{index:02d}',name,nodes,edges))

# Real model and real tool smoke.
edges={}; nodes=[node('Manual',BASE+'manualTrigger'),http('Readiness','/health/ready',pos=(220,0),method='GET'),http('Embedding test','/internal/smoke/validate-model-results',pos=(420,0))]
bundle, agent = agent_bundle('smoke',0,edges,True); nodes+=bundle
nodes.append(http('Verify observed tool call','/internal/smoke/finish',pos=(1000,0)))
for a,b in [('Manual','Readiness'),('Readiness','Embedding test'),('Embedding test',agent),(agent,'Verify observed tool call')]:link(edges,a,b)
all_workflows.append(workflow('WF00','Проверка Ollama + embeddings + tool calling',nodes,edges))

# Event router.
event_types=['media.uploaded','transcript.ready','meeting.confirmed','document.approved','question.created','export.requested','meeting.deleted']
edges={}; nodes=[node('Meeting event',BASE+'webhook',{'httpMethod':'POST','path':'meeting-events','authentication':'headerAuth','responseMode':'responseNode','options':{}},(0,0),2.1,CRED_EVENT,webhookId='hackalem-meeting-events'),
http('Accept known outbox event','/internal/events/accept','={{ JSON.stringify($json.body) }}',(240,0)),switch('Event type','event_type',event_types,(480,0)),
node('Accepted',BASE+'respondToWebhook',{'respondWith':'json','responseBody':'={{ JSON.stringify($json) }}','options':{'responseCode':202}},(1150,0),1.4)]
link(edges,'Meeting event','Accept known outbox event');link(edges,'Accept known outbox event','Event type')
for i,event in enumerate(event_types):
    if event in ('meeting.confirmed','document.approved'):
        target=sub('Index '+str(i),'WF04',{'event_id':'={{ $json.event_id }}'},(780,i*140))
    elif event=='export.requested':target=sub('Export','WF06',{'event_id':'={{ $json.event_id }}'},(780,i*140))
    else:target=http('Dispatch '+event,'/internal/events/dispatch',pos=(780,i*140))
    nodes.append(target);link(edges,'Event type',target['name'],i);link(edges,target['name'],'Accepted')
nodes.append(note('## Durable events\nUI → authenticated API → transactional outbox → this webhook. No audio blobs or public anonymous uploads.',(0,-230)))
all_workflows.append(workflow('WF01','События совещаний',nodes,edges,True))

# Dispatcher: global lease prevents concurrent heavy units.
edges={}; nodes=[node('Every 10 seconds',BASE+'scheduleTrigger',{'rule':{'interval':[{'field':'seconds','secondsInterval':10}]}},(0,0),1.2),node('Manual dispatch',BASE+'manualTrigger',pos=(0,200)),
http('Claim one work unit','/internal/work/claim','={{ JSON.stringify({}) }}',(230,0)),condition('Has work','has_work',(440,0)),switch('Work type','kind',['qa'],(650,0)),sub('Question answering','WF05',pos=(900,-80)),sub('Meeting analysis','WF03',pos=(900,120))]
for source in ('Every 10 seconds','Manual dispatch'):link(edges,source,'Claim one work unit')
link(edges,'Claim one work unit','Has work');link(edges,'Has work','Work type',0);link(edges,'Work type','Question answering',0);link(edges,'Work type','Meeting analysis',1)
all_workflows.append(workflow('WF02','Диспетчер очереди',nodes,edges,True))

for code, roles, title in [('WF03',['extract','reconcile','summary','verify'],'Агенты анализа совещаний'),('WF05',['qa'],'Вопросы, RAG и источники')]:
    edges={};nodes=[trigger(),http('Context','/internal/work/context',pos=(230,0))];link(edges,'Input','Context')
    if len(roles)>1:
        nodes.append(switch('Agent role','kind',roles,(440,0)));link(edges,'Context','Agent role')
    for index,role in enumerate(roles):
        bundle,agent=agent_bundle(role,index*480,edges);nodes+=bundle
        link(edges,'Agent role' if len(roles)>1 else 'Context',agent,index if len(roles)>1 else 0)
        link(edges,agent,'Validate',0);link(edges,agent,'Fail safely',1)
    nodes += [http('Validate','/internal/work/validate',"={{ JSON.stringify({scope_token: $('Context').first().json.scope_token, output: $json.output}) }}",(1600,0)),condition('Valid','valid',(1830,0)),
              http('Commit revision','/internal/work/commit',"={{ JSON.stringify({scope_token:$json.scope_token, output:$json.output}) }}",(2520,0)),http('Repair once','/internal/work/repair',pos=(2000,220)),http('Revalidate','/internal/work/validate',pos=(2210,220)),condition('Repaired','valid',(2420,220)),
              http('Fail safely','/internal/work/fail',"={{ JSON.stringify({scope_token:$('Context').first().json.scope_token,code:'agent_or_validation_failed'}) }}",(2650,420))]
    for a,b,p in [('Validate','Valid',0),('Valid','Commit revision',0),('Valid','Repair once',1),('Repair once','Revalidate',0),('Revalidate','Repaired',0),('Repaired','Commit revision',0),('Repaired','Fail safely',1)]:link(edges,a,b,p)
    nodes.append(note('## Local AI pipeline\nEach execution handles one bounded work unit. Full transcript coverage is tracked in PostgreSQL. Tools are read-only. The final protocol requires human confirmation.\n\nOllama qwen3:4b-instruct-2507-q4_K_M · no cloud models · no shared chat memory',(0,-270),600,220))
    all_workflows.append(workflow(code,title,nodes,edges))

for code,title,path in [('WF04','Индексирование RAG','/internal/rag/index-jobs'),('WF06','Экспорт PDF и DOCX','/internal/export-jobs')]:
    edges={};nodes=[trigger(('event_id',)),http('Durable worker job',path,pos=(260,0))];link(edges,'Input','Durable worker job')
    nodes.append(note('## Background job\nThe backend validates confirmation and enqueues durable work. The local worker performs embedding/indexing or document generation. PostgreSQL publishes the result only for the current revision.',(0,-220),530,190))
    all_workflows.append(workflow(code,title,nodes,edges))
for code,title,path,interval in [('WF07','Напоминания','/internal/reminders/scan',{'field':'hours','hoursInterval':1}),('WF09','Восстановление очереди','/internal/maintenance/reconcile',{'field':'minutes','minutesInterval':10})]:
    edges={};nodes=[node('Schedule',BASE+'scheduleTrigger',{'rule':{'interval':[interval]}},(0,0),1.2),node('Manual',BASE+'manualTrigger',pos=(0,180)),http('Run maintenance',path,'={{ JSON.stringify({}) }}',(300,0))]
    link(edges,'Schedule','Run maintenance');link(edges,'Manual','Run maintenance')
    all_workflows.append(workflow(code,title,nodes,edges,True))
edges={};nodes=[node('Error Trigger',BASE+'errorTrigger'),http('Safe error metadata','/internal/workflow-errors',"={{ JSON.stringify({execution_id:$json.execution?.id||'', workflow_id:$json.workflow?.id||''}) }}",(300,0))];link(edges,'Error Trigger','Safe error metadata')
all_workflows.append(workflow('WF08','Ошибки workflow',nodes,edges))

(ROOT/'workflows'/'all.json').write_text(json.dumps(all_workflows,ensure_ascii=False,indent=2),encoding='utf-8')
print(f'Generated {len(all_workflows)} workflows. Credentials are created separately by scripts/start.ps1.')
