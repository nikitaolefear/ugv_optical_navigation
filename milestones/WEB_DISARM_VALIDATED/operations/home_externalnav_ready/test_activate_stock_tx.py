"""Targeted stock activation guard tests; no hardware/service calls."""
import copy
import contextlib
import hashlib
import sqlite3
import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('stock_activation_under_test',ROOT/'activate_stock_tx.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class ActivationGuards(unittest.TestCase):
    def nav(self,flipped=False):
        # A known transform, independent of the production reconstruction.
        heading=math.radians(-7.0289256693)
        W_C=np.eye(4)
        W_C[:3,:3]=np.array([[math.sin(heading),0,math.cos(heading)],
                             [-math.cos(heading),0,math.sin(heading)], [0,-1,0]])
        W_C[:3,3]=[-.4817338034,.4723714267,1.215]
        W_M=np.eye(4);W_M[:3,:3]=np.array([[0,1,0],[-1,0,0],[0,0,1]])
        W_M[:3,3]=[3,0,0]
        C_M=np.linalg.inv(W_C)@W_M
        if flipped:C_M=C_M@np.diag([-1,-1,1,1])
        return dict(geometry_mode='LEGACY_PLANAR_Z0_YAW_ONLY',pose_fresh=True,
                    marker=23,fault=False,camera_thread_alive=True,
                    optical_measurement=dict(source_marker=23,frame='C_OPTICAL_RIGHT_DOWN_FORWARD',
                        fresh=True,nominal_marker_side_m=.283,acquired_at=99.95,T_Coptical_M=C_M.tolist()))

    def evidence(self,assets):
        return dict(schema_version=1,review_status='REVIEWED_STOCK_HOME',boot_id='fixture-boot',
                    package_root=str(m.PACKAGE),sha256=assets,operator_alignment_confirmed=True,
                    secondary_marker_reviewed=True,expected_home=dict(x_m=0.,y_m=0.,yaw_flu_deg=0.),
                    home_tolerances=dict(xy_m=.15,yaw_deg=5.))

    def test_stock_home_keeps_actual_pose_and_rejects_180_print(self):
        with patch.object(m.time,'monotonic',return_value=100.):
            h=m.stock_home(self.nav())
            self.assertAlmostEqual(h['x_m'],0.,places=9)
            self.assertAlmostEqual(h['y_m'],0.,places=9)
            self.assertAlmostEqual(h['yaw_flu_deg'],0.,places=9)
            e=self.evidence({})
            self.assertTrue(m.within_home(h,e))
            wrong=m.stock_home(self.nav(flipped=True))
            self.assertFalse(m.within_home(wrong,e))

    def test_stale_source_or_improper_pose_rejected(self):
        for change in ('stale','source','improper'):
            n=self.nav()
            if change=='stale':n['optical_measurement']['acquired_at']=98.
            elif change=='source':n['marker']=24
            else:n['optical_measurement']['T_Coptical_M'][0][0]=42.
            with self.subTest(change=change),patch.object(m.time,'monotonic',return_value=100.),self.assertRaises(RuntimeError):
                m.stock_home(n)

    def test_evidence_requires_independent_review_and_explicit_small_bounds(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);boot=root/'boot';boot.write_text('fixture-boot');e=self.evidence({'a':'b'})
            path=root/'e.json';path.write_text(json.dumps(e))
            with patch.object(m,'BOOT',boot):
                self.assertEqual(m.read_evidence(path,{'a':'b'}),e)
                for field,value in (('secondary_marker_reviewed',False),('operator_alignment_confirmed',False),
                                    ('boot_id','other'),('expected_home',dict(x_m=1.,y_m=0.,yaw_flu_deg=0.)),
                                    ('home_tolerances',dict(xy_m=5.,yaw_deg=180.))):
                    bad=dict(e,**{field:value});path.write_text(json.dumps(bad))
                    with self.subTest(field=field),self.assertRaises(RuntimeError):m.read_evidence(path,{'a':'b'})

    def test_cached_rc_requires_disarm_neutral_without_queries(self):
        safe=dict(rc=dict(age_s=.2,channels={'3':1496,'5':999}))
        with patch.object(m,'get',return_value=safe) as getter:
            self.assertEqual(m.rc_safe()['rc5'],999);getter.assert_called_once_with('/api/owner-diagnostics')
        for rc in (dict(age_s=4.,channels={'3':1496,'5':999}),
                   dict(age_s=.2,channels={'3':1496,'5':2000}),
                   dict(age_s=.2,channels={'3':1700,'5':999})):
            with self.subTest(rc=rc),patch.object(m,'get',return_value={'rc':rc}),self.assertRaises(RuntimeError):m.rc_safe()

    def activation_scenario(self,changed=False):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);ledger=root/'ledger.sqlite3';grant=root/'policy/alignment-verified.json';tx=root/'zz.conf'
            with contextlib.closing(sqlite3.connect(ledger)) as db:
                db.execute('CREATE TABLE history(id PRIMARY KEY, state)')
                db.execute("INSERT INTO history VALUES(1,'SENT')");db.commit()
            before=m.dump(ledger)
            states={u:'active' for u in (m.OWNER,m.GATEWAY,m.AUTH)};started=[False]
            def run(*args):
                if args[:2]==('systemctl','stop'):
                    for u in args[2:]:states[u]='inactive'
                if args[:2]==('systemctl','start'):
                    for u in args[2:]:states[u]='active'
                    if m.OWNER in args[2:] and not started[0]:
                        started[0]=True
                        if changed:
                            with contextlib.closing(sqlite3.connect(ledger)) as db:
                                db.execute("INSERT INTO history VALUES(2,'new audit preserved')");db.commit()
                return 'unit fixture' if args[:2]==('systemctl','cat') else ''
            def prop(u,key):
                if key=='MainPID':return '303' if u==m.AUTH else '101'
                if key=='ActiveState':return states[u]
                raise AssertionError((u,key))
            def rootdir(p,mode):p.mkdir(mode=mode,parents=True,exist_ok=True)
            def inspect(p):return dict(status='LEDGER_IDLE',logical_sha256=hashlib.sha256(m.dump(p).encode()).hexdigest())
            ctx=dict(evidence=self.evidence({}),assets={},auth_pid='303',tx_drop='[Service]\nExecStart=fixture --externalnav-tx\n')
            with patch.object(m,'LEDGER',ledger),patch.object(m,'GRANT',grant),patch.object(m,'TX_DROP',tx),\
                 patch.object(m,'BACKUPS',root/'backups'),patch.object(m,'run',side_effect=run),\
                 patch.object(m,'prop',side_effect=prop),patch.object(m,'rootdir',side_effect=rootdir),\
                 patch.object(m,'inspector',return_value=inspect),patch.object(m,'immutable'),\
                 patch.object(m,'disarmed'),patch.object(m,'rc_safe'),patch.object(m,'clean_socket'),\
                 patch.object(m,'stock_home',return_value=dict(x_m=.1,y_m=-.05,yaw_flu_deg=-2.)),\
                 patch.object(m,'get',return_value={}),patch.object(m.os,'chown'),\
                 patch.object(m,'wait_owner',return_value=dict(tx_enabled=True,sent=1)),\
                 patch.object(m,'sole_owner',return_value=101),patch('builtins.print'):
                if changed:
                    with self.assertRaises(RuntimeError):m.enable(ctx)
                else:m.enable(ctx)
            if changed:
                self.assertFalse(grant.exists());self.assertFalse(tx.exists())
                self.assertIn('new audit preserved',m.dump(ledger))
            else:
                self.assertEqual(m.dump(ledger),before)
                self.assertTrue(grant.exists());self.assertTrue(tx.exists())
            self.assertEqual(states,{u:'active' for u in states})

    def test_activation_preserves_full_ledger(self):self.activation_scenario()

    def test_rollback_preserves_newer_db_and_restores_observe(self):self.activation_scenario(changed=True)

    def test_atomic_enable_cannot_overwrite_concurrent_denial(self):
        with tempfile.TemporaryDirectory() as raw:
            path=Path(raw)/'grant';path.write_text('denied')
            with self.assertRaises(RuntimeError):m.atomic(path,b'allow')
            self.assertEqual(path.read_text(),'denied')
            path.unlink();real_link=m.os.link
            def concurrent_deny(src,dst,**kw):
                path.write_text('denied during enable')
                return real_link(src,dst,**kw)
            with patch.object(m.os,'link',side_effect=concurrent_deny),patch.object(m.os,'chown'),self.assertRaises(FileExistsError):
                m.atomic(path,b'allow')
            self.assertEqual(path.read_text(),'denied during enable')

    def test_disable_revokes_before_any_optional_service_change(self):
        calls=[]
        with tempfile.TemporaryDirectory() as raw:
            tx=Path(raw)/'zz.conf';tx.write_text('fixture')
            with patch.object(m,'TX_DROP',tx),patch.object(m,'immutable'),\
                 patch.object(m,'block_grant',side_effect=lambda:calls.append('revoke')),\
                 patch.object(m,'run',side_effect=lambda *a:calls.append(a)),\
                 patch.object(m.time,'sleep'),\
                 patch.object(m,'get',return_value=dict(alignment_verified=False,sent=10,tx_enabled=True)),\
                 patch('builtins.print'):
                m.disable()
            self.assertEqual(calls[0],'revoke')
            self.assertFalse(tx.exists())
            self.assertEqual(calls[1],('systemctl','daemon-reload'))


if __name__=='__main__':unittest.main(verbosity=2)
