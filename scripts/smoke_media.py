"""Verify local upload -> n8n webhook -> speech worker, without external uploads."""
import argparse
import json
import os
import time
from pathlib import Path
import httpx

parser=argparse.ArgumentParser()
parser.add_argument('recording',type=Path)
parser.add_argument('--base-url',default='http://web')
parser.add_argument('--report',type=Path,default=Path('/data/media-smoke-report.json'))
args=parser.parse_args()
client=httpx.Client(base_url=args.base_url,timeout=60)
def request(method,path,**kwargs):
    response=client.request(method,path,**kwargs)
    response.raise_for_status()
    return response.json()
request('POST','/api/login',json={'email':os.environ['ADMIN_EMAIL'],'password':os.environ['ADMIN_PASSWORD']})
meeting=request('POST','/api/meetings',json={'title':'Проверка загрузки · 45 секунд записи №1','consent':True,'participants':['Участник 1','Участник 2']})
mid=meeting['id']
with args.recording.open('rb') as recording:
    request('POST',f'/api/meetings/{mid}/media',files={'file':(args.recording.name,recording,'audio/wav')})
deadline=time.monotonic()+600
while time.monotonic()<deadline:
    meeting=request('GET',f'/api/meetings/{mid}')
    speech=next((j for j in meeting['jobs'] if j['kind']=='speech'),None)
    if speech and speech['state']=='failed':raise RuntimeError(speech['error'])
    if speech and speech['state']=='succeeded':break
    time.sleep(3)
else:raise TimeoutError('Speech worker did not finish')
assert meeting['document']['segments']
audio=client.get(f'/api/meetings/{mid}/audio');audio.raise_for_status()
assert audio.content.startswith(b'RIFF')
report={'passed':True,'meeting_id':mid,'scope':'upload_webhook_speech_audio_playback','segment_count':len(meeting['document']['segments']),
        'analysis_state':meeting['state'],'quality_evaluated':False}
args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
