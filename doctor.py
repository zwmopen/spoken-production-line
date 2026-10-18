import importlib.util,json,pathlib,sys
root=pathlib.Path(__file__).resolve().parent
checks={"python310":sys.version_info>=(3,10)}
for m in ("websocket","PIL","requests"):checks[m]=importlib.util.find_spec(m) is not None
p=root/"config"/"config.json";checks["configured"]=p.exists()
if p.exists():
 d=json.loads(p.read_text(encoding="utf-8-sig"));checks["taskRoot"]=pathlib.Path(d["taskRoot"]).is_dir();checks["templateRegistry"]=pathlib.Path(d["templateRegistry"]).is_file()
print(json.dumps({"checks":checks,"productionReady":False,"note":"Offline check only; browser, templates, adapters and end-to-end production require verification."},indent=2))
sys.exit(0 if all(checks.values()) else 1)
