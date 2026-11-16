"""Actual rendered template + JS in offline Chromium, temporary authenticated state."""
import sys,json,re,subprocess,tempfile,os,signal
from pathlib import Path
root=Path(__file__).resolve().parents[1]
if len(sys.argv)!=2:
    raise SystemExit('Usage: browser_telemetry_check.py OUTPUT_DIRECTORY')
out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(root))
from web_hmi.test_support import TestEnvironment
from web_hmi.telemetry_bridge import select_voltage
from web_hmi.test_telemetry_bridge import sample
source=root/'web_hmi'
# Synthetic fixture, never a current rover measurement. No live HTTP/serial access.
data=sample(age=0.2)
data.update(armed={'state':'FRESH','value':'DISARMED'},
            flight_mode={'state':'FRESH','value':{'autopilot':3,'vehicle_type':10,'base_mode':65,'custom_mode':0}},
            yaw_deg={'state':'FRESH','value':0.1},
            ekf={'EKF_STATUS_REPORT':{'state':'FRESH','value':{'flags':167}},
                 'ESTIMATOR_STATUS':{'state':'UNKNOWN','value':None}},
            messages={name:{'state':'FRESH','age_s':0.2} for name in
                      ('EKF_STATUS_REPORT','ATTITUDE','HEARTBEAT','SYS_STATUS','BATTERY_STATUS')})
data=select_voltage(data)
data['telemetry_bridge']={'state':'OK'}
env=TestEnvironment()
try:
 client=env.client();response=client.get('/');assert response.status_code==200
 html=response.text
 html=re.sub(r'<script\b[^>]*>.*?</script>','',html,flags=re.S)
 html=re.sub(r'<meta name="csrf-token" content="[^"]+">','',html)
 html=re.sub(r'<input type="hidden" name="csrf_token" value="[^"]+">','',html)
 html=html.replace('<link rel="stylesheet" href="/static/style.css">','<style>'+(source/'static/style.css').read_text()+'</style>')
 mock='''<script>
 const originalTimeout=window.setTimeout.bind(window);
 window.setTimeout=()=>0;window.setInterval=()=>0;
 const browserTelemetry=__DATA__;
 window.hmiFetch=async(url)=>({ok:true,json:async()=>url==='/api/telemetry'?browserTelemetry:__STATUS__});
 </script>'''.replace('__DATA__',json.dumps(data)).replace('__STATUS__',json.dumps(client.get('/api/status').json))
 checks='''<script>
 originalTimeout(async()=>{try{
 const check=(ok,why)=>{if(!ok)throw Error(why)};
 const savedVoltage=browserTelemetry.voltage_v.value;
 for(let i=0;i<4;i++) {
   browserTelemetry.flight_mode.value.custom_mode=i%2 ? 4 : 0;
   browserTelemetry.voltage_v.value=62+i/10;
   await pollTelemetry();
   check(document.getElementById('flight-mode').textContent.includes(i%2 ? 'HOLD' : 'MANUAL'),'mode updates');
   check(document.getElementById('voltage').textContent===(62+i/10).toFixed(2)+' V','voltage updates');
   check(!document.getElementById('freshness'),'freshness recreated');
 }
 browserTelemetry.flight_mode.value.custom_mode=0;
 browserTelemetry.voltage_v.value=savedVoltage;
 unavailableTelemetry();
 await pollTelemetry();
 const text=document.body.innerText;
 check(!/BATTERY_STATUS|ESTIMATOR_STATUS|\\bALT\\b/.test(text),'removed fields visible');
 check(!/EKF_STATUS_REPORT|flags=|EKF \\(/.test(text),'EKF visible');
 check(!document.getElementById('ekf'),'empty EKF section');
 check(!document.getElementById('freshness'),'freshness container');
 check(!/ATTITUDE|HEARTBEAT|SYS_STATUS/.test(text),'freshness labels visible');
 check(document.getElementById('connection').textContent==='CONNECTED','connection');
 check(document.getElementById('armed').textContent==='DISARMED','armed');
 check(document.getElementById('flight-mode').textContent.includes('MANUAL'),'mode');
 check(document.getElementById('voltage').textContent.includes(' V'),'voltage');
 check(document.getElementById('voltage-source').textContent==='Вхід ESC №3','source');
 check(document.getElementById('voltage-age').textContent.includes('с'),'age');
 check(document.documentElement.scrollWidth<=innerWidth+2,'horizontal overflow');
 voltagePacket.renderedAt=performance.now()-4000;renderVoltage();
 check(document.getElementById('voltage').textContent==='Застаріло','voltage expiry');
 voltagePacket={state:'UNKNOWN',value:null,source:null,age_s:null,renderedAt:performance.now()};renderVoltage();
 check(document.getElementById('voltage').textContent==='Немає даних','missing voltage');
 await pollTelemetry();
 document.body.dataset.visual='PASS';
 }catch(e){document.body.dataset.visual='FAIL';document.body.dataset.failure=e.message;}},50);
 </script>'''
 html=html.replace('</body>',mock+'<script>'+(source/'static/app.js').read_text()+'</script>'+checks+'</body>')
 results=[]
 for width,height in [(800,1100),(1280,800)]:
  with tempfile.TemporaryDirectory(prefix='ugv-tablet-',ignore_cleanup_errors=True) as d:
   p=Path(d);(p/'page.html').write_text(html)
   cmd=['chromium','--headless','--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--disable-background-networking','--no-first-run','--no-proxy-server','--user-data-dir='+str(p/'profile'),f'--window-size={width},{height}','--virtual-time-budget=1000','--dump-dom','--screenshot='+str(out/f'tablet-{width}.png'),(p/'page.html').as_uri()]
   proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,start_new_session=True)
   try:dom,_=proc.communicate(timeout=25)
   except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGTERM);proc.communicate();raise
   ok=proc.returncode==0 and 'data-visual="PASS"' in dom
   (out/f'tablet-{width}.html').write_text(dom)
   results.append({'viewport':[width,height],'passed':ok,'offline':True})
 print(json.dumps(results));(out/'browser-result.json').write_text(json.dumps(results,indent=2))
 assert all(r['passed'] for r in results)
finally:env.close()
