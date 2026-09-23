"""Exercise the REAL n8n + local Ollama pipeline on a synthetic meeting."""
import argparse
import json
import os
import time
from pathlib import Path
import httpx

parser=argparse.ArgumentParser()
parser.add_argument('--base-url',default='http://localhost:8080')
parser.add_argument('--env-file',default='.env')
parser.add_argument('--report',default='.runtime/e2e-report.json')
parser.add_argument('--meeting-id',help='Resume a previously interrupted synthetic test')
parser.add_argument('--timeout',type=int,default=1800)
args=parser.parse_args()
values=dict(os.environ)
if Path(args.env_file).exists():
    values.update(dict(line.split('=',1) for line in Path(args.env_file).read_text(encoding='utf-8-sig').splitlines() if '=' in line and not line.startswith('#')))
fixture=json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/synthetic/meeting.json').read_text(encoding='utf-8'))
http=httpx.Client(base_url=args.base_url,timeout=60)
def request(method,path,**kwargs):
    r=http.request(method,path,**kwargs);r.raise_for_status();return r.json()
def wait(meeting_id,predicate):
    end=time.time()+args.timeout
    last=None
    while time.time()<end:
        meeting=request('GET',f'/api/meetings/{meeting_id}')
        state=[(j['kind'],j['state'],j['attempts']) for j in meeting['jobs']]
        if state!=last:print(json.dumps(state),flush=True);last=state
        failed=[j for j in meeting['jobs'] if j['state']=='failed']
        if failed:raise RuntimeError(json.dumps(failed,ensure_ascii=False))
        if predicate(meeting):return meeting
        time.sleep(3)
    raise TimeoutError('Real pipeline did not complete')

request('POST','/api/login',json={'email':values['ADMIN_EMAIL'],'password':values['ADMIN_PASSWORD']})
if args.meeting_id:
    meeting_id=args.meeting_id
else:
    meeting=request('POST','/api/meetings',json={k:v for k,v in fixture.items() if k not in ('text','question')})
    meeting_id=meeting['id']
    request('POST',f'/api/meetings/{meeting_id}/transcript',json={'text':fixture['text'],'expected_revision':meeting['revision']})
print(json.dumps({'meeting_id':meeting_id}),flush=True)
meeting=wait(meeting_id,lambda m:m['state'] in ('ready_for_review','confirmed'))
actions=meeting['document']['actions']
assert len(actions)==2, f'Expected two distinct tasks, got {len(actions)}'
assert {a['assignee_mention'] for a in actions}=={'Айдана','Ерлан'}
assert next(a for a in actions if a['assignee_mention']=='Ерлан')['due']['date'] is None
assert next(a for a in actions if a['assignee_mention']=='Айдана')['due']['date']=='2026-09-25'
assert sum(j['tool_calls'] for j in meeting['jobs'])>0, 'No real backend tool invocation'
if meeting['state']!='confirmed':
    request('POST',f'/api/meetings/{meeting_id}/confirm',json={'expected_revision':meeting['revision']})
meeting=wait(meeting_id,lambda m:m['indexed_revision']==m['revision'])
request('POST',f'/api/meetings/{meeting_id}/questions',json={'question':fixture['question']})
meeting=wait(meeting_id,lambda m:any(j['kind']=='qa' and j['state']=='succeeded' for j in m['jobs']))
answer=next(j['result'] for j in meeting['jobs'] if j['kind']=='qa' and j['state']=='succeeded')
assert answer['status']=='answered' and answer['source_segment_ids']
exports={}
for format in ('pdf','docx'):
    request('POST',f'/api/meetings/{meeting_id}/exports',json={'format':format})
    meeting=wait(meeting_id,lambda m:any(j['kind']=='export' and j['state']=='succeeded' and j['result'].get('format')==format for j in m['jobs']))
    job=next(j for j in meeting['jobs'] if j['kind']=='export' and j['result'].get('format')==format)
    r=http.get('/api/exports/'+job['id']);r.raise_for_status()
    assert r.content.startswith(b'%PDF' if format=='pdf' else b'PK')
    exports[format]={'bytes':len(r.content),'job_id':job['id']}
report={'mode':'real','meeting_id':meeting_id,'model':'qwen3:4b','actions':actions,'answer':answer,'exports':exports,'jobs':meeting['jobs']}
Path(args.report).parent.mkdir(parents=True,exist_ok=True)
Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'passed':True,'meeting_id':meeting_id,'actions':len(actions),'exports':list(exports)}))
