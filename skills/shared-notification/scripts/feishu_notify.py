"""Bot credentials are environment-only."""
import os,requests
def load_feishu_config():
 a=os.environ.get("FEISHU_APP_ID");s=os.environ.get("FEISHU_APP_SECRET")
 if not a or not s:raise RuntimeError("Set FEISHU_APP_ID and FEISHU_APP_SECRET")
 return {"app_id":a,"app_secret":s}
def get_tenant_access_token(app_id,app_secret):
 r=requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",json={"app_id":app_id,"app_secret":app_secret},timeout=15)
 r.raise_for_status();d=r.json()
 if d.get("code")!=0:raise RuntimeError("Feishu bot authentication failed")
 return d["tenant_access_token"]
