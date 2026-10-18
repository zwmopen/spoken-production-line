#!/usr/bin/env python3
"""Read one ChatGPT generation turn and extract only its image asset IDs.

This is a read-only recovery helper. It never writes to the chat. The browser's
authenticated session is used in-page; access tokens and signed URLs are not
returned or persisted.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

from cdp_adapter import CDP


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--conversation-id", required=True)
    ap.add_argument("--start-message-id", required=True)
    ap.add_argument("--out", required=True, help="JSON receipt path")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    pages = json.load(urllib.request.urlopen(
        f"http://127.0.0.1:{args.port}/json/list", timeout=5
    ))
    target = next((x for x in pages if x.get("type") == "page" and
                   args.conversation_id in x.get("url", "")), None)
    if not target:
        raise SystemExit("matching conversation page not found")

    page = CDP(target["webSocketDebuggerUrl"], timeout=15)
    try:
        js = r"""(async()=>{
          const cid=CID, start=START;
          const session=await fetch('/api/auth/session').then(r=>r.json());
          const response=await fetch('/backend-api/conversation/'+cid,
            {headers:{Authorization:'Bearer '+session.accessToken}});
          if(!response.ok) return JSON.stringify({ok:false,httpStatus:response.status});
          const data=await response.json();
          const chain=[]; let node=data.current_node, found=false;
          while(node && data.mapping[node]) {
            const message=data.mapping[node].message;
            if(message) {
              if(message.id===start) {
                found=message.author?.role==='user';
                break;
              }
              const serialized=JSON.stringify({content:message.content,metadata:message.metadata});
              const refs=[...new Set([...serialized.matchAll(/(?:sediment:\/\/)?file_[A-Za-z0-9_-]+/g)]
                .map(m=>m[0].startsWith('sediment://')?m[0]:'sediment://'+m[0]))];
              const strings=(message.content?.parts||[]).filter(x=>typeof x==='string').join(' ');
              chain.push({id:message.id,role:message.author?.role,status:message.status,
                refs,text:strings.slice(-1200)});
            }
            node=data.mapping[node].parent;
          }
          return JSON.stringify({ok:true,conversationId:data.conversation_id,title:data.title,
            startMessageFound:found,messages:chain.reverse()});
        })()""".replace("CID", json.dumps(args.conversation_id)).replace(
            "START", json.dumps(args.start_message_id)
        )
        result = json.loads(page.evaluate(js, timeout=45))
    finally:
        page.close()

    if not result.get("ok") or result.get("conversationId") != args.conversation_id:
        raise SystemExit(f"conversation read failed: {result}")
    if not result.get("startMessageFound"):
        raise SystemExit("start message is not present as a user message in this conversation")
    refs = sorted({ref for message in result["messages"] for ref in message["refs"]})
    result["assetIds"] = refs
    result["assetCount"] = len(refs)
    result["accountingNote"] = "These are asset references after the exact start message; not proof each image is a finished independent output."
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"conversationId": args.conversation_id,
        "startMessageId": args.start_message_id,
        "messageCount": len(result["messages"]),
        "assetCount": len(refs), "assetIds": refs, "receipt": str(output)},
        ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
