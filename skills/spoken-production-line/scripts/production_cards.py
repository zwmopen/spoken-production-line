"""Canonical bot-only production cards; credentials stay in shared notifier."""
import json, datetime as dt, hashlib, pathlib, re, sys, uuid
import requests

def markdown(text):
    if text.startswith("```\n"):
        return {"tag":"markdown","content":text}
    # Use plain text for labels; Feishu clients have shown literal Markdown stars.
    return {"tag":"div","text":{"tag":"plain_text","content":text}}
def card(title,elements): return {"schema":"2.0","config":{"update_multi":True},"header":{"title":{"tag":"plain_text","content":title},"template":"green"},"body":{"elements":elements}}
def detail(label,value):
    value=str(value or "").strip()
    if not value:
        raise ValueError("Required notification detail is empty: "+label)
    # Feishu's markdown card element renders an unlabelled fenced code block
    # consistently; language tags such as `text` were returned as
    # `plain_text` and appeared blank in some clients.
    return [{"tag":"markdown","content":label+"\n```\n"+value+"\n```"}]

def _path_value(m,keys,label,product_fallback=None):
    value=next((m.get(k) for k in keys if m.get(k)),None)
    if not value and product_fallback is not None:
        value=str(product_fallback)
    if not value:
        raise ValueError("Cannot send completion card: missing "+label)
    p=pathlib.Path(str(value))
    if not p.exists():
        raise ValueError("Cannot send completion card: "+label+" does not exist: "+str(p))
    return str(p)

def _copy_file(product,m):
    candidates=[m.get(k) for k in ("copyFilePath","copyTextPath","copyPath","textFilePath") if m.get(k)]
    candidates += [str(product/"三平台文案.txt"),str(product/"文案.txt")]
    for candidate in candidates:
        p=pathlib.Path(str(candidate))
        if p.is_file() and p.stat().st_size:
            content=p.read_text(encoding="utf-8-sig")
            if all(x in content for x in ("小红书1","小红书2","抖音")):
                return p
    raise ValueError("Cannot send completion card: three-version copy file is missing or incomplete")

def _accepted_assets(m,expected):
    acceptance=m.get("acceptance") or {}
    evidence=acceptance.get("evidence") or {} if isinstance(acceptance,dict) else {}
    accepted=(isinstance(acceptance,dict) and str(acceptance.get("status","")).upper() in ("PASS","PASSED","ACCEPTED")) or bool(evidence.get("accepted")) or str(m.get("acceptanceStatus","")).upper() in ("PASS","PASSED","ACCEPTED")
    if not accepted:
        raise ValueError("Cannot send completion card: visual acceptance evidence is missing")
    pages=m.get("pages")
    if not isinstance(pages,list) or len(pages)!=expected:
        raise ValueError("Cannot send completion card: per-page native asset manifest does not match accepted count")
    for page in pages:
        path=page.get("path") or page.get("localPath") or page.get("outputPath")
        if not path or not pathlib.Path(path).is_file():
            raise ValueError("Cannot send completion card: accepted image path is missing")
        digest=page.get("sha256")
        if digest and hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()!=digest:
            raise ValueError("Cannot send completion card: accepted image hash mismatch")

def completed(m,quota):
    a=str(m.get("account") or m.get("accountId") or ""); line={"A":"网页 GPT-A","B":"网页 GPT-B","account-2":"网页 GPT-B","Codex":"Codex","codex":"Codex"}.get(a)
    if not line: raise ValueError("Cannot send completion card: unknown production line")
    title=str(m.get("displayTitle") or m.get("title") or "").strip()
    work_id=str(m.get("workId") or "").strip()
    if not title or not work_id: raise ValueError("Cannot send completion card: title or ledger Work ID is missing")
    archived=m.get("archivedAt") or m.get("producedAt")
    if not archived: raise ValueError("Cannot send completion card: production time is missing")
    try: stamp=dt.datetime.fromisoformat(str(archived).replace("Z","+00:00")).astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    except Exception as exc: raise ValueError("Cannot send completion card: invalid production time") from exc
    product_fallback=pathlib.Path(m["manifestPath"]).parent if m.get("manifestPath") else None
    product=pathlib.Path(_path_value(m,("productFolderPath","localFolderPath"),"product folder",product_fallback))
    local_template=_path_value(m,("localTemplatePath","templateFolderPath"),"local template path")
    source=_path_value(m,("sourceFolderPath","sourceMaterialPath","rawMaterialPath"),"source material path")
    accepted=m.get("acceptedPageCount")
    planned=m.get("plannedPageCount")
    if type(accepted) is not int or accepted<1 or type(planned) is not int or planned!=accepted:
        raise ValueError("Cannot send completion card: planned and accepted image counts do not match")
    _accepted_assets(m,accepted)
    _copy_file(product,m)
    if m.get("usageRecorded") is not True:
        raise ValueError("Cannot send completion card: usage has not been recorded")
    if str(m.get("mode","")).lower().find("cockpit")>=0:
        conversation="无网页对话（本套通过 Cockpit API 调用本地模板生产）"
    else:
        conversation=m.get("conversationUrl") or m.get("onlineTemplateConversationUrl")
        if not conversation: raise ValueError("Cannot send completion card: online template conversation is missing")
    if not m.get("templateId"): raise ValueError("Cannot send completion card: template ID is missing")
    images=str((quota or {}).get("images","未知")); uploads=str((quota or {}).get("uploads","未知")); upload_works=str((quota or {}).get("uploadWorks","未知"))
    els=[markdown("整套制作完成\n作品名："+title+"\n生产时间："+stamp+"\n图片："+str(accepted)+"张\n文案：三版齐全\n验收：通过"),markdown("最近3小时用量（估算）\n生成："+images+" / 50 张\n上传："+uploads+" / 83 个附件\n上传作品："+upload_works+" 套\n仅本地已知记录；手机及外部用量未知，非官方余额。"),markdown("台账 Work ID："+work_id+"\n本地模板编号："+str(m["templateId"]))]
    for label,value in [("在线模板对话",conversation),("本地模板路径",local_template),("素材路径",source),("成品路径",str(product))]: els.extend(detail(label,value))
    return card("团建口述生产线（"+line+"）",els)
def daily(summary,window,pending=""):
    total=sum(x["sets"] for x in summary.values());els=[markdown("本时段完成　"+str(total)+" 套\n"+window),markdown("网页 GPT-A："+str(summary["A"]["sets"])+" 套\n网页 GPT-B："+str(summary["B"]["sets"])+" 套\nCodex："+str(summary["Codex"]["sets"])+" 套")]
    if pending:els.append(markdown(pending))
    return card("团建口述生产线｜产出日报",els)
def bot_session():
    sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[2]/"shared-notification"/"scripts"))
    from feishu_notify import load_feishu_config,get_tenant_access_token
    c=load_feishu_config();s=requests.Session();s.headers["Authorization"]="Bearer "+get_tenant_access_token(c["app_id"],c["app_secret"]);return s

def deliver(payload,chat_id,key,receipt_path,message_id=None):
    p=pathlib.Path(receipt_path);p.parent.mkdir(parents=True,exist_ok=True)
    old=json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    if old.get("key") not in (None,key):
        raise RuntimeError("Receipt key mismatch; refusing to reuse another work notification")
    client_uuid=old.get("clientUuid") or str(uuid.uuid5(uuid.NAMESPACE_URL,key))
    mid=message_id or old.get("messageId")
    content=json.dumps(payload,ensure_ascii=False)
    # Persist the exact payload and deterministic idempotency key before any external write.
    receipt={"messageId":mid,"chatId":chat_id,"key":key,"clientUuid":client_uuid,
             "status":"PREPARING","readbackVerified":False,"card":payload}
    p.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding="utf-8")
    s=bot_session()
    if mid:
        receipt["status"]="PATCHING_EXISTING"
        p.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding="utf-8")
        try: result=s.patch("https://open.feishu.cn/open-apis/im/v1/messages/"+mid,json={"content":content},timeout=30).json()
        except Exception as exc:
            receipt["status"]="PATCH_UNKNOWN";receipt["lastError"]=type(exc).__name__
            p.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding="utf-8");raise
    else:
        receipt["status"]="SUBMITTING"
        p.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding="utf-8")
        try:
            result=s.post("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                          json={"receive_id":chat_id,"msg_type":"interactive","content":content,"uuid":client_uuid},timeout=30).json()
        except Exception as exc:
            receipt["status"]="SUBMISSION_UNKNOWN";receipt["lastError"]=type(exc).__name__
            p.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding="utf-8");raise
        if result.get("code")==0:mid=result["data"]["message_id"]
    if result.get("code")!=0:
        receipt["status"]="REJECTED";receipt["apiCode"]=result.get("code");receipt["apiMessage"]=result.get("msg")
        p.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding="utf-8")
        raise RuntimeError("Card rejected: "+str(result.get("code"))+" "+str(result.get("msg")))
    receipt.update(messageId=mid,status="SENT_READBACK_PENDING",apiCode=result.get("code"))
    p.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding="utf-8")
    try:
        got=s.get("https://open.feishu.cn/open-apis/im/v1/messages/"+mid,params={"card_msg_content_type":"user_card_content"},timeout=20).json()
    except Exception as exc:
        receipt["status"]="READBACK_PENDING";receipt["lastError"]=type(exc).__name__
        p.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding="utf-8");raise
    items=got.get("data",{}).get("items",[])
    body=(items[0].get("body") or {}).get("content") if items else None
    if isinstance(body,str):
        try: body=json.loads(body)
        except Exception: pass
    expected_text=[]
    def collect(x):
        if isinstance(x,dict):
            for k,v in x.items():
                if k in ("content",) and isinstance(v,str): expected_text.append(v)
                collect(v)
        elif isinstance(x,list):
            for v in x: collect(v)
    collect(payload)
    actual_text=[]
    def collect_actual(x):
        if isinstance(x,dict):
            for k,v in x.items():
                if k=="content" and isinstance(v,str): actual_text.append(v)
                collect_actual(v)
        elif isinstance(x,list):
            for v in x: collect_actual(v)
    collect_actual(body)
    def normalize_text(value):
        value=value.replace("\r\n","\n").replace("\r","\n")
        value=re.sub(r"```(?:plain_text|text)?[ \t]*\n?","",value)
        value=value.replace("```","")
        return re.sub(r"\s+"," ",value).strip()
    joined_actual=normalize_text("\n".join(actual_text))
    # Feishu's user-card GET may concatenate a markdown label directly with
    # its fenced code block and rewrite the fence language. Compare the
    # complete normalized text without whitespace so the copyable path value
    # is still required, while avoiding false failures on renderer spacing.
    compact_actual=re.sub(r"\s+","",joined_actual)
    content_ok=bool(body is not None and all(re.sub(r"\s+","",normalize_text(t)) in compact_actual for t in expected_text))
    ok=bool(got.get("code")==0 and items and items[0].get("chat_id")==chat_id and items[0].get("sender",{}).get("sender_type")=="app" and items[0].get("msg_type")=="interactive" and content_ok)
    receipt.update(status="READBACK_VERIFIED" if ok else "READBACK_FAILED",readbackVerified=ok,contentReadbackVerified=content_ok,clientVisualVerified=False,readbackMode="user_card_content",senderType=items[0].get("sender",{}).get("sender_type") if items else None,readbackCard=body);p.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding="utf-8")
    if not ok:raise RuntimeError("Card sent or updated; content GET verification failed; do not resend")
    return receipt
