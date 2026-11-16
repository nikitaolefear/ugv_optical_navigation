"""Run isolated checks in separate interpreters to avoid package-name overlap."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent


def main():
    reports=[]
    groups=[
        ('engine',None,ROOT/'tests',None,'test_engine.py'),
        ('ipc',None,ROOT/'tests',None,'test_ipc.py'),
        ('gateway',None,ROOT/'tests',None,'test_gateway.py'),
        ('inhibit',None,ROOT/'tests',None,'test_inhibit.py'),
        ('deploy-profile',None,ROOT/'tests',None,'test_deploy_profile.py'),
        ('owner','owner',ROOT/'tests',None,'test_owner.py'),
        ('owner-config','owner',ROOT/'tests',None,'test_owner_config.py'),
        ('legacy-readonly','owner',ROOT/'tests',None,'test_legacy_readonly.py'),
        ('web','auth',ROOT/'tests',None,'test_web.py'),
        ('e2e','auth',ROOT/'tests',None,'test_e2e.py'),
        ('auth-regression','auth',ROOT/'auth/web_hmi',ROOT/'auth','test_auth.py'),
        ('access-regression','auth',ROOT/'auth/web_hmi',ROOT/'auth','test_access.py'),
        ('demo-regression','auth',ROOT/'auth/web_hmi',ROOT/'auth','test_hmi.py'),
        ('production-stop',None,REPO/'tests',None,'test_production_stop*.py'),
    ]
    for name,package,start,top,pattern in groups:
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',
                 PYTHONPATH=os.pathsep.join(str(p) for p in
                     ([ROOT/package] if package else [])+[ROOT,REPO]))
        env.pop('UGV_HMI_ENABLE_DISARM',None)
        env.pop('UGV_HMI_TELEMETRY',None)
        command=[sys.executable,'-m','unittest','discover','-s',str(start),'-p',pattern,'-q']
        if top:command+=['-t',str(top)]
        result=subprocess.run(command,capture_output=True,text=True,env=env,cwd=REPO,timeout=120)
        count=re.search(r'Ran (\d+) tests?',result.stderr)
        report=dict(group=name,tests=int(count[1]) if count else 0,passed=result.returncode==0)
        reports.append(report)
        print(json.dumps(report),flush=True)
        if result.returncode:
            print(result.stdout+result.stderr)
            raise SystemExit(result.returncode)
    summary=dict(groups=reports,total=sum(r['tests'] for r in reports))
    Path('/tmp/ugv-disarm-test-results.json').write_text(json.dumps(summary,indent=2))
    print('ALL PASSED:',summary['total'])


if __name__=='__main__':
    main()
