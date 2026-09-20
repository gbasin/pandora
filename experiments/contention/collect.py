"""Collect receipts and task-relevant agent events after controller completion."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

p=argparse.ArgumentParser()
p.add_argument('--state',type=Path,required=True)
p.add_argument('--controller',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
lanes=json.loads((a.state/'setup/lanes.json').read_text())
summary={'agents':{},'journeys':[]}
for phase,info in lanes.items():
    lane=a.controller/'lanes'/phase
    assert (lane/'status').read_text().strip()=='succeeded',(phase,(lane/'status').read_text())
    worktree=Path(info['worktree'])
    assert (worktree/'value.txt').read_text().strip()==phase
    assert json.loads((worktree/'dist/result.json').read_text())=={'value':phase}
    events=[]
    source=lane/('events.jsonl' if phase.startswith('codex') else 'result.md')
    for line in source.read_text().splitlines():
        try:event=json.loads(line)
        except json.JSONDecodeError:continue
        if phase.startswith('codex'):
            item=event.get('item',{})
            if event.get('type')=='item.completed' and item.get('type') in ['command_execution','agent_message']:
                events.append(item)
        elif event.get('type')=='assistant':
            for item in event.get('message',{}).get('content',[]):
                if item.get('type')=='tool_use':
                    events.append({'type':'tool_use','id':item.get('id'),'name':item.get('name'),'input':item.get('input')})
                elif item.get('type')=='text':
                    events.append({'type':'agent_message','text':item['text']})
        elif event.get('type')=='user':
            for item in event.get('message',{}).get('content',[]):
                if item.get('type')=='tool_result':events.append(item)
        elif event.get('type')=='result':
            events.append({k:v for k,v in event.items() if k in ['type','subtype','is_error','result','duration_ms','num_turns']})
    (a.output/(phase+'-events.json')).write_text(json.dumps(events,indent=2)+'\n')
    shutil.copyfile(a.state/'setup'/(phase+'.md'),a.output/(phase+'-brief.md'))
    key=hashlib.sha256(str(worktree.resolve()).encode()).hexdigest()
    attempts=[]
    for submitted in sorted((a.state/'requests'/key).glob('*/submission.json')):
        d=submitted.parent;m=json.loads(submitted.read_text());t=json.loads((d/'terminal.json').read_text())
        assert t['cleanup_verified']
        attempts.append({'attempt':m['attempt'],'request':m['docker']['request'],
                         'source_digest':m['source_digest'],'total_seconds':m.get('total_seconds'),
                         'local_request_created_at':d.stat().st_birthtime,
                         'metrics':json.loads((d/'metrics.json').read_text()),'terminal':t,
                         'report':json.loads((d/'results/docker.json').read_text())})
    assert len(attempts)==4,(phase,len(attempts))
    assert sorted(x['terminal']['exit_code'] for x in attempts)==[0,0,0,1]
    summary['agents'][phase]={'worktree':str(worktree),'runs':attempts,'local_output':{'value':phase}}
for f in (a.state/'journey').glob('*/*/terminal.json'):
    d=f.parent;t=json.loads(f.read_text());m=json.loads((d/'submission.json').read_text())
    assert t['cleanup_verified'] and t['exit_code']==0
    summary['journeys'].append({'attempt':t['attempt'],'terminal':t,'metrics':json.loads((d/'metrics.json').read_text()),'total_seconds':m.get('total_seconds'),'report':json.loads((d/'results/journey.json').read_text())})
summary['recovery']=json.loads((a.state/'recovery/results.json').read_text())
recovery_worktree=Path((a.controller/'lanes/recovery/worktree').read_text().strip()).resolve()
recovery_key=hashlib.sha256(str(recovery_worktree).encode()).hexdigest()
summary['recovery_accepted_attempts']=[f.parent.name for f in sorted((a.state/'requests'/recovery_key).glob('*/submission.json'))]
assert len(summary['recovery_accepted_attempts'])==3
assert len(summary['recovery'])==2
for r in summary['recovery']:assert r['same_attempt'] and r['terminal']['cleanup_verified']
(a.output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
for name in ['resources.jsonl','resources-corrected.jsonl','journey.log']:
    shutil.copyfile(a.state/name,a.output/name)
shutil.copytree(a.state/'recovery',a.output/'recovery',dirs_exist_ok=True)
print('Collected four agents, journey, and two recovery cases.')
