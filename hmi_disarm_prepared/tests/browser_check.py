"""Offline Chromium checks against rendered prepared HTML and fake hmiFetch.

No HTTP server, serial, GPIO, production auth state or live HMI is used.
Run with prepared auth and common package on PYTHONPATH.
"""
import json
from pathlib import Path
import re
import subprocess
import tempfile
from unittest.mock import Mock

from web_hmi.app import create_app
from web_hmi.test_support import TestEnvironment, CONFIG

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = Path('/tmp/ugv-disarm-browser')


def main():
    ARTIFACTS.mkdir(exist_ok=True)
    env = TestEnvironment()
    with env.store.transaction() as db:
        db.execute("INSERT INTO role_permissions VALUES('admin','pixhawk.disarm')")
    try:
        env.app = lambda *args, **kwargs: create_app(config=dict(CONFIG, DISARM_ENABLED=True),
            auth_store=env.store, disarm_client=Mock())
        client = env.client()
        html = client.get('/').text
    finally:
        env.close()
    # Session secrets are test-only and removed from preview output.
    html = re.sub(r'<meta name="csrf-token"[^>]+>', '', html)
    html = re.sub(r'<input[^>]+name="csrf_token"[^>]+>', '', html)
    html = re.sub(r'<script[^>]*src="[^"]+"[^>]*></script>', '', html)
    css = (ROOT/'auth/web_hmi/static/style.css').as_uri()
    html = html.replace('/static/style.css', css)
    html = html.replace('id="armed">UNKNOWN', 'id="armed">DISARMED · тестові дані')
    html = html.replace('id="connection">UNKNOWN', 'id="connection">FAKE · без обладнання')
    script = r'''
<script>
let scenario='observed', posts=0, gets=0;
function record(delivery='SENT', observation='UNKNOWN', pending=false) {
  return {state:delivery,delivery_state:delivery,command_outcome:'UNKNOWN',
    observation_state:observation,observation_pending:pending,
    heartbeat_age_s:observation==='UNKNOWN'?null:0.01,
    heartbeat_source:observation==='UNKNOWN'?null:[1,1],
    heartbeat_freshness_s:scenario==='stale'?0.25:3};
}
window.hmiFetch=async(url,options={})=>{
  if(options.method==='POST'){
    posts++;
    if(scenario==='delivery')throw new Error('Канал недоступний');
    if(scenario==='rejected')return {ok:false,json:async()=>({error:'DISARM_DISABLED'})};
    return {ok:true,json:async()=>record('QUEUED','UNKNOWN',true)};
  }
  gets++;
  if(['lost','delivery','rejected'].includes(scenario))throw new Error('Канал недоступний');
  if(scenario==='invalid')return {ok:true,json:async()=>({state:'CONFIRMED'})};
  if(scenario==='timeout')return {ok:true,json:async()=>record('QUEUED','UNKNOWN',true)};
  return {ok:true,json:async()=>record('SENT',scenario==='rearmed'?'ARMED_OBSERVED':'DISARMED_OBSERVED')};
};
</script>
'''
    js = (ROOT/'auth/web_hmi/static/disarm.js').as_uri()
    setup = script + f'<script src="{js}"></script>'
    preview = ARTIFACTS/'preview.html'
    preview.write_text(html.replace('</body>', setup+'</body>'))
    checks = r'''
<script>
(async()=>{
const results=[];
const button=document.getElementById('pixhawk-disarm');
const recheck=document.getElementById('disarm-recheck');
const notice=document.getElementById('disarm-notice');
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const wait=async(predicate)=>{const limit=performance.now()+15000;while(!predicate()){if(performance.now()>limit)throw new Error('check timeout');await sleep(50);}};
const rect=button.getBoundingClientRect();
const arm=document.getElementById('pixhawk-arm'),armRect=arm.getBoundingClientRect();
results.push({name:'touch-target',ok:rect.height>=60&&rect.width>=120});
results.push({name:'arm-placeholder-and-equal-row',ok:arm.disabled&&arm.textContent==='ARM'&&button.textContent==='DISARM'&&Math.abs(rect.width-armRect.width)<1&&rect.height===armRect.height&&rect.y===armRect.y});
results.push({name:'no-horizontal-overflow',ok:document.documentElement.scrollWidth<=innerWidth});
for(const mode of ['observed','delivery','rejected','invalid','timeout','stale']){
  scenario=mode;posts=0;gets=0;
  button.click();button.click();
  if(['observed','stale'].includes(mode))await wait(()=>notice.textContent.includes('Спостерігається DISARMED'));
  else await wait(()=>!recheck.hidden);
  let ok=posts===1&&button.disabled&&!notice.textContent.includes('DISARM підтверджено');
  if(mode==='observed'){
    ok=ok&&notice.textContent.includes('НЕ підтверджено');
    scenario='rearmed';await wait(()=>notice.textContent.includes('Спостерігається ARMED'));
    results.push({name:'rearmed-degrades-live-state',ok:!notice.textContent.includes('Спостерігається DISARMED')});
    scenario='lost';await wait(()=>notice.textContent.includes('Стан Pixhawk UNKNOWN'));
    results.push({name:'loss-clears-observation',ok:!notice.textContent.includes('Спостерігається DISARMED')});
    scenario='observed';await wait(()=>notice.textContent.includes('Спостерігається DISARMED'));
    Object.defineProperty(document,'hidden',{configurable:true,value:true});
    document.dispatchEvent(new Event('visibilitychange'));
    const oldGets=gets;await sleep(600);
    results.push({name:'hidden-clears-and-pauses',ok:notice.textContent.includes('UNKNOWN')&&gets===oldGets});
    Object.defineProperty(document,'hidden',{configurable:true,value:false});
    document.dispatchEvent(new Event('visibilitychange'));
    await wait(()=>notice.textContent.includes('Спостерігається DISARMED'));
  } else if(mode==='stale') {
    await wait(()=>notice.textContent.includes('застаріло'));
    ok=ok&&notice.textContent.includes('UNKNOWN');
  } else ok=ok&&notice.textContent.includes('UNKNOWN');
  results.push({name:mode,ok});
  recheck.click();
  results.push({name:mode+'-explicit-new-request-only',ok:!button.disabled&&posts===1});
}
window.dispatchEvent(new Event('pagehide'));
const output=document.createElement('pre');output.id='browser-results';output.textContent=JSON.stringify(results);
document.body.append(output);document.title=results.every(r=>r.ok)?'TESTS_PASS':'TESTS_FAIL';
})().catch(error=>{document.title='TESTS_FAIL';document.body.dataset.error=error.message;});
</script>
'''
    page = ARTIFACTS/'checks.html'
    page.write_text(html.replace('</body>', setup+checks+'</body>'))
    report = []
    for width, height in ((1280,800),(800,1280)):
        with tempfile.TemporaryDirectory(prefix='ugv-chromium-') as profile:
            common = ['/usr/bin/chromium','--headless','--no-sandbox','--disable-gpu',
                '--disable-dev-shm-usage','--disable-background-networking','--no-first-run',
                '--no-default-browser-check','--allow-file-access-from-files',
                '--password-store=basic','--hide-scrollbars',f'--user-data-dir={profile}',
                f'--window-size={width},{height}']
            result = subprocess.run(common+['--dump-dom','--virtual-time-budget=45000',page.as_uri()],
                                    capture_output=True,text=True,timeout=45)
            (ARTIFACTS/f'dom-{width}.html').write_text(result.stdout)
            assert result.returncode == 0 and '<title>TESTS_PASS</title>' in result.stdout, result.stderr[-1000:]
            match = re.search(r'<pre id="browser-results">([^<]+)</pre>',result.stdout)
            values = json.loads(match[1])
            report.append(dict(viewport=[width,height],checks=values))
            screenshot = ARTIFACTS/f'preview-{width}x{height}.png'
            subprocess.run(common+['--virtual-time-budget=1000',f'--screenshot={screenshot}',preview.as_uri()],
                           check=True,capture_output=True,timeout=30)
    (ARTIFACTS/'results.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,ensure_ascii=False))
    print('Screenshots and results:', ARTIFACTS)


if __name__ == '__main__':
    main()
