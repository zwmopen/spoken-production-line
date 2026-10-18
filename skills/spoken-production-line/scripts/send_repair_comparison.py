"""Send user-requested original/template/repair image comparisons with readback receipts."""
import argparse,datetime,hashlib,json,sys,uuid
from pathlib import Path
import requests

def send(spec_path):
    spec_path=Path(spec_path); spec=json.loads(spec_path.read_text(encoding='utf-8-sig'))
    receipt_path=Path(spec['receiptPath'])
    if receipt_path.exists():
        prior=json.loads(receipt_path.read_text(encoding='utf-8-sig'))
        if prior.get('readbackVerified'): return prior
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'shared-notification'/'scripts'))
    from feishu_notify import load_feishu_config,get_tenant_access_token,upload_image
    cfg=load_feishu_config();token=get_tenant_access_token(cfg['app_id'],cfg['app_secret'])
    headers={'Authorization':'Bearer '+token}
    group=requests.get('https://open.feishu.cn/open-apis/im/v1/chats/'+spec['chatId'],headers=headers,timeout=20).json()
    if group.get('code')!=0 or spec['chatId']!='YOUR_FEISHU_CHAT_ID':raise RuntimeError('Authorized production group not verified')
    images=[]; rows=[[{'tag':'text','text':spec['summary']}]]
    for item in spec['images']:
        p=Path(item['path'])
        if not p.is_file():raise FileNotFoundError(str(p))
        key=upload_image(token,str(p))
        if not key:raise RuntimeError('Image upload failed: '+item['label'])
        images.append({'label':item['label'],'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'imageKey':key})
        rows.extend([[{'tag':'text','text':item['label']}],[{'tag':'img','image_key':key}]])
    rows.append([{'tag':'text','text':'这是返修对照，不代表新增成品；请直接说哪一版更合适、是否需要返修。当前生产继续。'}])
    payload={'receive_id':spec['chatId'],'msg_type':'post','content':json.dumps({'zh_cn':{'title':spec['title'],'content':rows}},ensure_ascii=False),'uuid':str(uuid.uuid5(uuid.NAMESPACE_URL,spec['comparisonId']))}
    result=requests.post('https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id',headers=headers,json=payload,timeout=30).json()
    if result.get('code')!=0:raise RuntimeError('Comparison message rejected code='+str(result.get('code')))
    mid=result['data']['message_id']
    receipt={'comparisonId':spec['comparisonId'],'workId':spec['workId'],'chatId':spec['chatId'],'groupName':group['data']['name'],'messageId':mid,'images':images,'sentAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),'readbackVerified':False}
    receipt_path.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    response=requests.get('https://open.feishu.cn/open-apis/im/v1/messages/'+mid,headers=headers,timeout=20).json()
    items=response.get('data',{}).get('items',[])
    content=items[0].get('body',{}).get('content','') if items else ''
    receipt['readbackVerified']=response.get('code')==0 and bool(items) and items[0].get('chat_id')==spec['chatId'] and all(i['imageKey'] in content for i in images)
    receipt['readbackAt']=datetime.datetime.now(datetime.timezone.utc).isoformat()
    receipt_path.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if not receipt['readbackVerified']:raise RuntimeError('Sent but readback unverified; do not duplicate')
    return receipt

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('spec');a=p.parse_args()
    r=send(a.spec);sys.stdout.reconfigure(encoding='utf-8');print(json.dumps({k:r[k] for k in ['comparisonId','groupName','messageId','readbackVerified']},ensure_ascii=False))
