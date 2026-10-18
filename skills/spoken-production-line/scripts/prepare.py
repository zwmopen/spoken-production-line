"""Prepare a deduplicated upload-only queue; never contact an online account."""
import argparse, hashlib, json, random, re
from pathlib import Path

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--config',required=True); p.add_argument('--out',required=True)
    a=p.parse_args(); cfg=json.loads(Path(a.config).read_text(encoding='utf-8-sig'))
    root=Path(cfg['materialRoot']); registry=json.loads(Path(cfg['templateRegistry']).read_text(encoding='utf-8-sig'))
    templates=[t for t in registry['templates'] if '精·方案' in Path(t['localPath']).name and all(t.get('conversationLinks',{}).get(ac['provider'],{}).get('status')=='ready' for ac in cfg['accounts'])]
    if not templates: raise RuntimeError('No ready precise plan templates')
    rng=random.Random(cfg.get('seed',20261014)); budgets={a['id']:0 for a in cfg['accounts']}; seen=set(cfg.get('excludedSourceIdentities',[])); rows=[]
    candidates=sorted(root.rglob('文案.txt')); rng.shuffle(candidates)
    for txt in candidates:
        folder=txt.parent; text=folder.name+' '+txt.read_text(encoding='utf-8-sig',errors='replace')
        if '团建' not in folder.name or not re.search('方案|行程|路线|攻略|[一二两123]天|[一二123]日',folder.name): continue
        if re.search('夏日|夏季|避暑|漂流',folder.name) and not re.search('秋|9月|10月|11月',folder.name): continue
        lead=text[:len(folder.name)+500]
        if cfg.get('seasonPattern') and not re.search(cfg['seasonPattern'],lead): continue
        # Select against the dominant title/opening, not incidental footer seasons.
        primary=folder.name+' '+txt.read_text(encoding='utf-8-sig',errors='replace')[:180]
        if cfg.get('seasonPattern') and re.search(r'春季|夏季|五月|六月|七月|八月|(?<!\d)[3-8]月',primary) and not re.search(cfg['seasonPattern'],primary): continue
        imgs=sorted([x for x in folder.iterdir() if x.is_file() and x.suffix.lower() in {'.jpg','.jpeg','.png','.webp'}],key=lambda x:(0 if '封面' in x.name else 1,int(re.search(r'内页(\d+)',x.name).group(1)) if re.search(r'内页(\d+)',x.name) else 0,x.name))
        if len(imgs)<2: continue
        paths=[*imgs,txt]
        if not any(budgets[a['id']]+len(paths)<=a.get('attachmentBudget',80) for a in cfg['accounts']):continue
        hashes=[hashlib.sha256(x.read_bytes()).hexdigest() for x in paths]
        identity=hashlib.sha256(''.join(hashes).encode()).hexdigest()
        if identity in seen: continue
        accounts=[a for a in cfg['accounts'] if budgets[a['id']]+len(paths)<=a.get('attachmentBudget',80)]
        if not accounts: continue
        ac=min(accounts,key=lambda a:budgets[a['id']]); pool=[t for t in templates if t['id'] not in cfg.get('excludedTemplateIdsByAccount',{}).get(ac['id'],[]) and not any(r['account']==ac['id'] and r['templateId']==t['id'] for r in rows)]
        if not pool:continue
        t=rng.choice(pool); seen.add(identity); budgets[ac['id']]+=len(paths)
        rows.append({'id':f'PLAN-{identity[:12]}','account':ac['id'],'sourcePath':str(folder),'title':folder.name,'sourceIdentity':identity,'files':[str(x) for x in paths],'sha256':hashes,'images':len(imgs),'txt':1,'templateId':t['id'],'templateTitle':Path(t['localPath']).name,'templateUrl':t['conversationLinks'][ac['provider']]['url'],'prompt':'出计划','status':'PREPARED_NOT_UPLOADED'})
    prompt_template=(Path(__file__).resolve().parent.parent/'references'/'plan-prompt.txt').read_text(encoding='utf-8')
    for row in rows:
        row['prompt']=prompt_template.format(templateId=row['templateId'],templateTitle=row['templateTitle'],sourceTitle=row['title'],imageCount=row['images'],textCount=row['txt'],scope=cfg.get('scope','以用户当前要求为准'))
    out=Path(a.out); out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps({'mode':'upload-plan-only','seed':cfg.get('seed',20261014),'accountAttachments':budgets,'templatePoolSize':len(templates),'tasks':rows},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'notes':len(rows),'attachments':budgets,'templatePool':len(templates),'queue':str(out)},ensure_ascii=False))

if __name__=='__main__': main()
