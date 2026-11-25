#!/usr/bin/env python3
"""Changed RC values from existing owner's read() via strace; never opens serial.
Run as owner OS user. No MAVLink sender, parameter requests, control or restart.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import time
from zoneinfo import ZoneInfo
from pymavlink.dialects.v20 import ardupilotmega as mav


def owner_identity():
    pid=int(subprocess.check_output(['systemctl','show','ugv-hmi.service','-p','MainPID','--value'],text=True))
    if pid<=0:raise RuntimeError('Owner not running')
    matches=[]
    for p in Path(f'/proc/{pid}/fd').iterdir():
        try:target=os.readlink(p)
        except FileNotFoundError:continue
        if target.startswith('/dev/ttyACM'):matches.append((int(p.name),target))
    if len(matches)!=1:raise RuntimeError('Expected one owner serial descriptor')
    threads=sorted(int(p.name) for p in Path(f'/proc/{pid}/task').iterdir())
    return pid,*matches[0],threads


def consume(lines, emit, log, delta_us=5, messages=False, status=False):
    parser=mav.MAVLink(None);parser.robust_parsing=True
    previous=None;message_seen=set();last_rc=None;stale_shown=False;counts={'RC_CHANNELS':0,'changes':0,'HEARTBEAT':0,'armed_heartbeats':0}
    # This accepts exactly hex strings produced by strace -xx, never eval().
    pattern=re.compile(r'(\d{10}\.\d+)\s+read\(\d+, "((?:\\x[0-9a-f]{2})*)", \d+\)\s*=\s*(\d+)')
    for line in lines:
        if line is None:
            if status and last_rc is not None and time.time()-last_rc>3 and not stale_shown:
                emit('RC STALE: no RC_CHANNELS for more than3s');stale_shown=True
            continue
        match=pattern.search(line)
        if not match:continue
        data=bytes.fromhex(match[2].replace('\\x',''))
        if len(data)!=int(match[3]):raise RuntimeError('Truncated read capture')
        stamp=float(match[1]);label=datetime.fromtimestamp(stamp,ZoneInfo('Europe/Kyiv')).strftime('%H:%M:%S.%f')[:-3]
        for msg in parser.parse_buffer(data) or []:
            if (msg.get_srcSystem(),msg.get_srcComponent())!=(1,1):continue
            kind=msg.get_type()
            if kind=='HEARTBEAT':
                counts['HEARTBEAT']+=1
                counts['armed_heartbeats']+=bool(msg.base_mode&128)
            if kind=='PARAM_VALUE':
                ident=str(msg.param_id)
                if ident in ('RC5_OPTION','RCMAP_THROTTLE','ARMING_CHECK','ARMING_REQUIRE'):
                    emit(f'{label} LIVE_PARAM {ident}={msg.param_value}')
                    log({'event':'LIVE_PARAM','unix_s':stamp,'name':ident,'value':msg.param_value})
            if kind=='SYS_STATUS':
                vision=bool(msg.onboard_control_sensors_health & mav.MAV_SYS_STATUS_SENSOR_VISION_POSITION)
                log({'event':'VISION_HEALTH','unix_s':stamp,'healthy':vision})
            if kind=='STATUSTEXT':
                text=str(msg.text)
                log({'event':'STATUSTEXT','unix_s':stamp,'severity':msg.severity,'text':text})
                if messages and text not in message_seen:emit(f'{label} {text}')
                message_seen.add(text)
            if kind!='RC_CHANNELS':continue
            counts['RC_CHANNELS']+=1
            gap=stamp-last_rc if last_rc is not None else None
            last_rc=stamp;stale_shown=False
            if status:
                age=max(0.0,time.time()-stamp)
                rssi_label='255(UNKNOWN)' if msg.rssi==255 else str(msg.rssi)
                emit(f'{label} CH5={msg.chan5_raw} CH3={msg.chan3_raw} RSSI={rssi_label} RX_age={age:.3f}s '+('FRESH' if age<1.5 else 'STALE'))
                log({'event':'RC_STATUS','unix_s':stamp,'ch5':msg.chan5_raw,'ch3':msg.chan3_raw,'rssi':msg.rssi,'capture_age_s':age,'interarrival_s':gap,'time_boot_ms':msg.time_boot_ms})
            current={i:getattr(msg,f'chan{i}_raw') for i in range(1,min(msg.chancount,18)+1)}
            if previous is None:
                previous=current
                log({'event':'BASELINE','unix_s':stamp,'channels':current,'chancount':msg.chancount,'rssi':msg.rssi})
                emit(f'{label} RC baseline: {msg.chancount} channels. Waiting for changes; no channel labelled ARM by assumption.')
                continue
            changed={i:(previous.get(i),v) for i,v in current.items()
                     if i not in previous or (previous[i] in (0,65535))!=(v in (0,65535)) or abs(v-previous[i])>=delta_us}
            if changed:
                counts['changes']+=1
                emit(label+' '+', '.join(f'CH{i}: {old} → {new}' for i,(old,new) in changed.items()))
                log({'event':'RC_CHANGED','unix_s':stamp,'changes':{str(i):list(values) for i,values in changed.items()},'time_boot_ms':msg.time_boot_ms})
                for i,(_,value) in changed.items():previous[i]=value
    return counts


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--seconds',type=int,default=90)
    p.add_argument('--delta-us',type=int,default=5,help='Cumulative change threshold, default5us; use1 for exact changes')
    p.add_argument('--status',action='store_true',help='Show CH5/CH3/RSSI and capture freshness for every RC packet')
    p.add_argument('--messages',action='store_true',help='Also show unique STATUSTEXT')
    p.add_argument('--log',help='New private JSONL file; no overwrite')
    p.add_argument('--replay',help='Decode an existing strace file; no process attach')
    a=p.parse_args()
    if not 1<=a.seconds<=120 or not 1<=a.delta_us<=100:p.error('Invalid duration/threshold')
    output=None;trace=None
    def record(data):
        if output is not None:output.write(json.dumps(data,ensure_ascii=False)+'\n');output.flush()
    def emit(text):print(text,flush=True)
    try:
        if a.log:
            fd=os.open(a.log,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            output=os.fdopen(fd,'w')
        if a.replay:
            with open(a.replay) as f:counts=consume(f,emit,record,a.delta_us,a.messages,a.status)
        else:
            pid,fd,device,threads=owner_identity()
            command=['strace','-qq','-ttt','-xx','-s','8192','-e','trace=read',f'--trace-fds={fd}','-o','/dev/stdout']
            for tid in threads:command+=['-p',str(tid)]
            # Attach existing threads only; never follow camera/GPIO child processes.
            trace=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
            emit(f'PASSIVE RC MONITOR owner={pid} fd={fd} device={device}; window={a.seconds}s; throttle neutral; thrust physically blocked.')
            deadline=time.monotonic()+a.seconds
            def lines():
                buffered=b''
                while time.monotonic()<deadline:
                    if select.select([trace.stdout],[],[],.25)[0]:
                        chunk=os.read(trace.stdout.fileno(),65536)
                        if not chunk:
                            error=trace.stderr.read(1000)
                            if trace.poll() is not None:raise RuntimeError('Trace ended: '+error)
                        else:
                            buffered+=chunk
                            while b'\n' in buffered:
                                line,buffered=buffered.split(b'\n',1)
                                yield line.decode('utf-8',errors='replace')
                    if a.status:yield None
                    if not Path(f'/proc/{pid}').exists():raise RuntimeError('Owner exited; do not autoattach another owner')
            try:counts=consume(lines(),emit,record,a.delta_us,a.messages,a.status)
            except KeyboardInterrupt:
                emit('Operator stopped passive monitor; owner remains running.')
                return
        emit('SUMMARY '+json.dumps(counts))
        record({'event':'SUMMARY',**counts})
    finally:
        if trace is not None:
            if trace.poll() is None:trace.send_signal(signal.SIGINT)
            try:trace.communicate(timeout=5)
            except subprocess.TimeoutExpired:trace.kill();trace.communicate()
        if output is not None:output.close()

if __name__=='__main__':main()
