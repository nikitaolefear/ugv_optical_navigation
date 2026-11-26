"""Static profile checks only; no users/units/configuration are installed."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DeployProfileTests(unittest.TestCase):
    def test_gateway_template_is_unprivileged_and_disabled_by_default(self):
        lines = (ROOT/'deploy/ugv-command-gateway.service.example').read_text().splitlines()
        values = dict(line.split('=',1) for line in lines if '=' in line and not line.startswith('#'))
        for key, value in {'User':'ugv-command','NoNewPrivileges':'yes','PrivateDevices':'yes',
                           'ProtectSystem':'strict','ProtectHome':'yes','RestrictAddressFamilies':'AF_UNIX',
                           'CapabilityBoundingSet':'','AmbientCapabilities':''}.items():
            self.assertEqual(values[key],value)
        self.assertNotIn('--enable-disarm',values['ExecStart'])
        self.assertNotIn('dialout',values.get('SupplementaryGroups',''))
        self.assertNotIn('gpio',values.get('SupplementaryGroups',''))
        self.assertNotIn('sudo',values.get('SupplementaryGroups',''))

    def test_gateway_does_not_open_serial_spawn_or_pass_fds(self):
        source = (ROOT/'disarm_control/gateway.py').read_text()
        tree = ast.parse(source)
        calls = {node.func.attr for node in ast.walk(tree)
                 if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)}
        self.assertFalse(calls & {'mavlink_connection','sendmsg','Popen','system','open','write'})
        self.assertNotIn('SCM_RIGHTS',source)
        self.assertNotIn('/dev/',source)

    def test_identity_configuration_has_only_invalid_placeholders_and_feature_off(self):
        lines = (ROOT/'deploy/identities.env.example').read_text().splitlines()
        values = dict(line.split('=',1) for line in lines if '=' in line and not line.startswith('#'))
        for key in ('UGV_FRONTEND_UID','UGV_GATEWAY_UID','UGV_OWNER_UID',
                    'UGV_FRONTEND_RPC_GID','UGV_OWNER_RPC_GID'):
            self.assertEqual(values[key],'0')
        self.assertEqual(values['UGV_HMI_ENABLE_DISARM'],'0')
        self.assertEqual(values['UGV_HMI_TELEMETRY'],'0')
