"""Offline Chromium visual check of the actual rendered access page.

The runtime's Chromium cannot reach even a trivial ephemeral loopback server;
interactive HTTP checks are covered by Flask integration tests, not claimed here.
No real database, browser profile, telemetry or HMI service is used.
"""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from web_hmi.test_support import TestEnvironment, PASSWORD


def main():
    out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
    env=TestEnvironment()
    try:
        env.store.add_user('chief',PASSWORD,'superadmin')
        env.store.add_user('viewer',PASSWORD,'observer')
        app=env.app();client=app.test_client()
        page=client.get('/login')
        csrf=re.search(r'name="csrf_token" value="([^"]+)"',page.text)[1]
        assert client.post('/login',data=dict(username='chief',password=PASSWORD,csrf_token=csrf)).status_code==303
        html=client.get('/access').text
        html=re.sub(r'<script\b[^>]*>.*?</script>','',html,flags=re.S)
        html=re.sub(r'<meta name="csrf-token" content="[^"]+">','',html)
        html=re.sub(r'<input type="hidden" name="csrf_token" value="[^"]+">','',html)
        html=html.replace('<link rel="stylesheet" href="/static/style.css">',
                          '<style>'+Path('web_hmi/static/style.css').read_text()+'</style>')
        mock = """<script>
        const browserCalls=[];let rejectNext=false;
        const timer=window.setTimeout.bind(window);
        window.setTimeout=(fn,ms,...args)=>ms===900?0:timer(fn,ms,...args);
        window.hmiFetch=async(url,options)=>{browserCalls.push({url,body:JSON.parse(options.body)});
          const denied=rejectNext;rejectNext=false;
          return {ok:!denied,json:async()=>denied?{error:'Перевірена відмова'}:{message:'Перевірено'}};};
        </script>"""
        visual = mock + '<script>' + Path('web_hmi/static/access.js').read_text() + '</script>' + """<script>
        (async()=>{try{
          const check=(ok)=>{if(!ok)throw Error('visual-check');};
          const wait=()=>new Promise(resolve=>timer(resolve,15));
          const user=document.querySelector('[data-kind="user-create"]');
          user.elements.username.value='visualuser';user.elements.password.value='visual-test-password';
          user.requestSubmit();await wait();
          check(browserCalls.length===1 && browserCalls[0].body.username==='visualuser');
          check(user.elements.password.value==='');
          const role=document.querySelector('[data-kind="role-create"]');
          role.elements.name.value='visualrole';role.elements.rank.value='50';
          role.querySelector('[value="status.view"]').checked=true;
          role.requestSubmit();await wait();
          check(browserCalls[1].body.rank===50 && browserCalls[1].body.permissions.includes('status.view'));
          const deletion=document.querySelector('[data-kind="user-delete"]');
          window.confirm=()=>false;deletion.requestSubmit();await wait();check(browserCalls.length===2);
          window.confirm=()=>true;deletion.requestSubmit();await wait();check(browserCalls[2].body.action==='delete');
          rejectNext=true;user.elements.password.value='visual-test-password';user.requestSubmit();await wait();
          check(document.getElementById('access-message').textContent==='Перевірена відмова');
          check([...document.querySelectorAll('.access-form button')].every(b=>!b.disabled));
          check(document.documentElement.scrollWidth<=innerWidth+2);
          check(document.querySelectorAll('.access-card').length>=7);
          check([...document.querySelectorAll('a')].some(a=>a.textContent==='Журнал подій'));
          user.reset();role.reset();document.getElementById('access-message').textContent='';
          document.body.dataset.visual='PASS';
        }catch(error){document.body.dataset.visual='FAIL';}})();
        </script>"""
        html=html.replace('</body>',visual+'</body>')
        with tempfile.TemporaryDirectory(prefix='ugv-visual-',ignore_cleanup_errors=True) as directory:
            root=Path(directory);source=root/'access.html';source.write_text(html)
            command=['chromium','--headless','--no-sandbox','--disable-gpu','--disable-dev-shm-usage',
                     '--disable-background-networking','--no-first-run','--no-proxy-server',
                     '--user-data-dir='+str(root/'profile'),'--window-size=800,1100',
                     '--virtual-time-budget=1500','--dump-dom','--screenshot='+str(out/'tablet-access.png'),source.as_uri()]
            process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,start_new_session=True)
            try:
                stdout,_=process.communicate(timeout=20)
                passed=process.returncode==0 and 'data-visual="PASS"' in stdout
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGTERM);process.communicate(timeout=5);passed=False
        result=dict(visual_passed=passed,offline_ui_javascript_passed=passed,viewport=[800,1100],temporary_state_only=True,
                    interactive_browser_passed=False,interactive_blocker='Chromium loopback HTTP navigation timed out; even a trivial server received no browser request',
                    telemetry_started=bool(app.extensions['pixhawk_telemetry'].thread))
        (out/'browser-result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
        return 0 if passed else 1
    finally:env.close()

if __name__=='__main__':raise SystemExit(main())
