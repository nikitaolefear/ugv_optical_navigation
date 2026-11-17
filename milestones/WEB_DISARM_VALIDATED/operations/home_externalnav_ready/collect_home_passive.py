#!/usr/bin/python3
"""Observe existing owner HTTP only; save distinct stock planar marker poses."""
import argparse, importlib.util, json, math, statistics, time, hashlib
from pathlib import Path
from datetime import datetime, timezone
p=argparse.ArgumentParser();p.add_argument('--marker',type=int,choices=(23,24),required=True);p.add_argument('--duration',type=float,default=15);a=p.parse_args()
root=Path(__file__).resolve().parent
s=importlib.util.spec_from_file_location('activation',root/'activate_stock_tx.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
s=importlib.util.spec_from_file_location('planar',m.PACKAGE/'optical/pose_transform_2d.py');planar=importlib.util.module_from_spec(s);s.loader.exec_module(planar)
boot=m.BOOT.read_text().strip();stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
out=root.parent/'home_externalnav_observations'/(stamp+'-boot-home-id'+str(a.marker));out.mkdir()
assets={n:m.sha(m.PACKAGE/'optical'/n) for n in m.ASSETS}
marker=next(x for x in json.loads((m.PACKAGE/'optical/marker_map.json').read_text())['markers'] if x['id']==a.marker)
o=marker['orientation'];W=m.euler(o['roll'],o['pitch'],o['yaw']);seen=set();samples=[];end=time.monotonic()+a.duration
with (out/'raw.jsonl').open('w') as f:
 while time.monotonic()<end:
  nav=m.get('/api/externalnav');state=m.get('/api/state');measurement=nav.get('optical_measurement') or {}
  row={'utc':datetime.now(timezone.utc).isoformat(),'nav':nav,'state':state};f.write(json.dumps(row)+'\n');f.flush()
  m.require(m.BOOT.read_text().strip()==boot,'Boot changed')
  m.require(nav.get('tx_enabled') is False and nav.get('sent')==0,'Expected observe-only')
  m.require(state.get('pixhawk_disarmed') is True and state.get('heartbeat_age_s',99)<1.5,'Fresh DISARMED required')
  tstamp=measurement.get('acquired_at')
  if nav.get('pose_fresh') and not nav.get('fault') and measurement.get('fresh') and measurement.get('source_marker')==a.marker and tstamp not in seen and 0<=time.monotonic()-tstamp<=.2:
   T=measurement['T_Coptical_M'];R=m.matrix_rotation(T);t=[T[i][3] for i in range(3)]
   C=[[sum(W[i][k]*R[j][k] for k in range(3)) for j in range(3)] for i in range(3)]
   pos=[marker['position'][axis]-sum(C[i][j]*t[j] for j in range(3)) for i,axis in enumerate(('x','y','z'))]
   yaw=math.degrees(math.atan2(C[1][2],C[0][2]));pose=planar.camera_to_rover_pose(pos[0],pos[1],yaw)
   samples.append(list(pose));seen.add(tstamp)
  time.sleep(.1)
summary={'boot_id':boot,'marker':a.marker,'unique_frames':len(samples),'sha256':assets,'package_root':str(m.PACKAGE),'directory':str(out),'control_actions':False}
if samples:
 summary['mean_xyyaw_flu']=[statistics.mean(x[i] for x in samples) for i in range(3)]
 summary['std_xyyaw_flu']=[statistics.pstdev(x[i] for x in samples) for i in range(3)]
 summary['all_samples_within_home']=all(math.hypot(x[0],x[1])<=.15 and abs(x[2])<=5 for x in samples)
(out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
(out/'SHA256SUMS').write_text(''.join(hashlib.sha256((out/n).read_bytes()).hexdigest()+'  '+n+'\n' for n in ('raw.jsonl','summary.json')))
print(json.dumps(summary,indent=2))
