"""Test status parsing and exit changes without router modules or live mutations."""
import ast
import json
from pathlib import Path
import subprocess
import time
import unittest
from unittest.mock import Mock

SOURCE = Path(__file__).resolve().parents[1] / 'src/dashboard.py'
NAMES = {'normalize_tailscale_status', 'get_tailscale_status', '_get_tailscale_status', 'set_tailscale_run_exit', 'tailscale_exit_items', 'set_tailscale_exit'}

class TailscaleTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(SOURCE.read_text())
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in NAMES]
        self.env = {'time': time, 'json': json, 'subprocess': Mock(), 'uci_get': lambda k: '0'}
        self.env['subprocess'].TimeoutExpired = subprocess.TimeoutExpired
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), self.env)
        self.sample = {'BackendState': 'Running', 'Self': {'Online': True, 'HostName': 'Mudi'},
            'TailscaleIPs': ['fd7a::1', '100.64.0.1'], 'Peer': {
                'a': {'ID': 'a', 'HostName': 'Home', 'Online': True, 'ExitNodeOption': True, 'TailscaleIPs': ['100.64.0.11']},
                'b': {'HostName': 'Office', 'Online': False, 'ExitNodeOption': True, 'TailscaleIPs': ['100.64.0.12']},
                'c': {'HostName': 'Phone', 'Online': True}},
            'ExitNodeStatus': {'ID': 'a', 'Online': True, 'TailscaleIPs': ['100.64.0.11/32']}}

    def test_status_and_candidates(self):
        result = self.env['normalize_tailscale_status'](self.sample)
        self.assertEqual(result['state'], 'Connected')
        self.assertEqual(result['ip'], '100.64.0.1')
        self.assertEqual(result['selected_ip'], '100.64.0.11')
        self.assertEqual(result['peers'], '2 / 3 online')
        items = self.env['tailscale_exit_items'](result)
        self.assertEqual(items, [('', 'None (direct connection)'), ('100.64.0.11', 'Home'), ('100.64.0.12', 'Office [offline]')])

    def test_exit_name_prefers_tailnet_dns_over_localhost(self):
        peer = self.sample['Peer']['a']
        peer.update(HostName='localhost', DNSName='shanghai-ecs-example.tailnet.ts.net.')
        result = self.env['normalize_tailscale_status'](self.sample)
        self.assertEqual(result['exit_nodes'][0]['name'], 'shanghai-ecs-example')
        self.assertEqual(result['exit_nodes'][0]['ip'], '100.64.0.11')
        self.assertEqual(result['exit'], 'shanghai-ecs-example / up')

    def test_disconnected_states(self):
        f = self.env['normalize_tailscale_status']
        self.assertEqual(f({'BackendState':'NeedsLogin'})['state'], 'Login required')
        self.assertEqual(f({'BackendState':'Stopped'})['state'], 'Stopped')
        self.assertEqual(f({'BackendState':'Running','Self':{'Online':False}})['state'], 'Offline')

    def test_daemon_unavailable_and_timeout(self):
        proc = self.env['subprocess']
        proc.run.return_value = Mock(stdout='', stderr='failed to connect to local tailscaled', returncode=1)
        self.assertEqual(self.env['get_tailscale_status']()['state'], 'Not running')
        proc.run.side_effect = subprocess.TimeoutExpired('tailscale', 3)
        self.assertEqual(self.env['get_tailscale_status']()['state'], 'Timed out')

    def setup_apply(self):
        self.saved = ''
        self.calls = []
        self.cfg = {'enabled':True, 'lan_enabled':True, 'wan_enabled':False, 'masq':True, 'run_exit_node':False}
        def rpc(module, method, params, timeout=8):
            if method == 'get_config':
                return True, dict(self.cfg, exit_node_ip=self.saved)
            self.calls.append(params)
            self.saved = params['exit_node_ip']
            return True, {}
        def status():
            result = self.env['normalize_tailscale_status'](self.sample)
            result['selected_ip'] = self.saved
            return result
        self.env.update(gl_lua_rpc=rpc, get_tailscale_status=status,
                        uci_get=lambda k: self.saved if k=='tailscale.settings.exit_node_ip' else '0')

    def test_preserves_configuration_and_clears(self):
        self.setup_apply()
        f = self.env['set_tailscale_exit']
        self.assertEqual(f('100.64.0.11'), 'Exit node selected')
        self.assertEqual(self.calls[-1], dict(self.cfg, exit_node_ip='100.64.0.11'))
        self.assertIn('Exit cleared', f(''))
        self.assertEqual(self.calls[-1], dict(self.cfg, exit_node_ip=''))

    def test_offline_disabled_and_advertising_rejected(self):
        self.setup_apply()
        f = self.env['set_tailscale_exit']
        self.assertEqual(f('100.64.0.12'), 'Exit node unavailable')
        self.cfg['enabled'] = False
        self.assertEqual(f('100.64.0.11'), 'Enable / sign in via web UI')
        self.cfg.update(enabled=True, run_exit_node=True)
        self.assertEqual(f('100.64.0.11'), 'Disable exit-node advertising first')
        self.assertEqual(self.calls, [])

    def test_run_exit_preserves_other_settings(self):
        self.setup_apply()
        def rpc(module, method, params, timeout=8):
            if method == 'get_config':
                return True, dict(self.cfg, exit_node_ip=self.saved)
            self.calls.append(params)
            self.cfg.update(params)
            return True, {}
        self.env['gl_lua_rpc'] = rpc
        self.env['uci_get'] = lambda k: '1' if self.cfg['run_exit_node'] else '0'
        f = self.env['set_tailscale_run_exit']
        self.assertIn('Saved On', f(True))
        self.assertEqual(self.calls[-1], dict(enabled=True, lan_enabled=True, wan_enabled=False,
                                             masq=True, run_exit_node=True, exit_node_ip=''))
        self.assertIn('Saved Off', f(False))
        self.assertFalse(self.calls[-1]['run_exit_node'])

    def test_run_exit_rejects_conflicts_and_disabled(self):
        self.setup_apply()
        f = self.env['set_tailscale_run_exit']
        self.saved = '100.64.0.11'
        self.assertIn('Select None', f(True))
        self.saved = ''
        self.cfg['enabled'] = False
        self.assertIn('Enable / sign in', f(True))
        self.assertEqual(self.calls, [])

    def test_run_exit_failed_save(self):
        self.setup_apply()
        self.assertEqual(self.env['set_tailscale_run_exit'](True), 'Setting was not saved')

    def test_failed_api_does_not_report_success(self):
        self.setup_apply()
        self.env['gl_lua_rpc'] = lambda *a, **k: (False, None)
        self.assertEqual(self.env['set_tailscale_exit']('100.64.0.11'), 'Cannot read Tailscale settings')

if __name__ == '__main__':
    unittest.main()
