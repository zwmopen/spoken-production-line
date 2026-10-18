"""Copyable natural-language workflows; no browser production writes."""
import html,json
from pathlib import Path

def render_commands():
    rows=json.loads((Path(__file__).resolve().parent.parent/'references'/'快捷命令目录.json').read_text(encoding='utf-8'))
    out=['<section id="quick-commands"><h2>快捷命令 · 复制后发给 AI</h2><p>平时按原队列继续生产；临时要求可复制命令、替换【方括号】后发送。这里只复制文字，不直接操作生产页面。插队先保存游标，当前生成终态后执行，完成回原队列。</p><input id="command-filter" placeholder="搜索：补料 / 出成品 / 插队 / 元数据 / 额度" oninput="filterCommands(this.value)" style="width:90%;padding:10px;margin-bottom:12px">']
    group=None
    for i,x in enumerate(rows):
        if x['group']!=group:
            if group is not None:out.append('</div></details>')
            group=x['group'];out.append('<details open><summary>'+html.escape(group)+'</summary><div class="cards">')
        out.append('<article class="command-item" style="border:1px solid #dce3ed;border-radius:10px;padding:12px;min-width:240px;flex:1"><h3>'+html.escape(x['title'])+'</h3><small>'+html.escape(x['note'])+'</small><textarea readonly id="cmd-'+str(i)+'" style="display:block;box-sizing:border-box;width:100%;height:110px;margin:10px 0;padding:8px">'+html.escape(x['command'])+'</textarea><button type="button" onclick="copyCommand('+str(i)+',this)">复制命令</button></article>')
    out.append('</div></details><p id="copy-feedback" role="status"></p></section>')
    out.append("""<script>async function copyCommand(n,b){const t=document.getElementById('cmd-'+n);let ok=false;try{await navigator.clipboard.writeText(t.value);ok=true}catch(e){t.focus();t.select();try{ok=document.execCommand('copy')}catch(e){}}document.getElementById('copy-feedback').textContent=ok?'已复制，粘贴到对话并补充要求即可。':'请在已选中的命令中按 Ctrl+C 复制。';b.textContent=ok?'已复制':'手动复制'}function filterCommands(v){document.querySelectorAll('.command-item').forEach(e=>e.hidden=!(e.innerText+e.querySelector('textarea').value).includes(v.trim()))}</script>""")
    return ''.join(out)
