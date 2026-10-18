import json,pathlib
root=pathlib.Path(__file__).resolve().parent
dest=root/"config"/"config.json"
if not dest.exists():
 d=json.loads((root/"config"/"config.example.json").read_text(encoding="utf-8"))
 for k in ("taskRoot","materialRoot","templateRoot","finishedRoot","templateRegistry"):d[k]=str((root/d[k]).resolve())
 dest.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding="utf-8")
cfg=json.loads(dest.read_text(encoding="utf-8-sig"));task=pathlib.Path(cfg["taskRoot"]);task.mkdir(parents=True,exist_ok=True);(task/"supervisor").mkdir(exist_ok=True)
for a in cfg["accounts"]:
 p=task/("queue-"+a["id"]+".json")
 if not p.exists():p.write_text(json.dumps({"account":a["id"],"cdpPort":a["cdpPort"],"tasks":[]},indent=2),encoding="utf-8")
for k in ("materialRoot","templateRoot","finishedRoot"):pathlib.Path(cfg[k]).mkdir(parents=True,exist_ok=True)
print("Local empty workspace configured; no platform request submitted.")
