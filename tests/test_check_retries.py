#!/usr/bin/env python3
"""Exercise paced multi-path checks, fixed-sample catch-up and fail-closed retry boundaries."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'explorer'))
import public_checks as CHECK
import ingress_checks as INGRESS
import public_config as CONFIG
import usdb_public as PUBLIC
from common.check_timing import CheckClock
from common.public_services import PublicRpcFixture, rpc_response


def missing():
    return CHECK.RpcFailure('RPC_HTTP', 'HTTP endpoint returned status 404; check route')


class CheckRetryTests(unittest.TestCase):
    def setUp(self):
        self.clock = CheckClock()
        self.requests = CHECK.CheckRequests(clock=self.clock.now, sleep=self.clock.sleep)
        self.identity = CONFIG.read_json(ROOT / 'explorer/networks/usdb-testnet-v1.json')
        self.rpc = PublicRpcFixture(self.identity)
        self.config = CONFIG.read_json(ROOT / 'explorer/config.example.json')
        self.config['rpc'] = {name: 'http://private.invalid' for name in ('read_url', 'trace_url', 'broadcast_url')}
        self.upstream = CHECK.preflight(self.config, self.identity, rpc_factory=self.client)

    def client(self, url):
        return CHECK.ReadRpc(url, fetcher=lambda _, payload: rpc_response(self.rpc, payload))

    def check(self, api=None, **kwargs):
        return CHECK.check_explorer(self.config, self.identity, preflight_report=self.upstream,
                                    rpc_factory=self.client, api_fetch=api or self.rpc.api,
                                    requests=self.requests, **kwargs)

    def test_public_rpc_rate_limit_recovers_and_reports_retries(self):
        attempts = []
        def client(method, params):
            attempts.append((method, deepcopy(params), self.clock.now()))
            if len(attempts) <= 2:
                raise CHECK.RpcFailure('RPC_RATE_LIMIT', 'HTTP endpoint returned status 429')
            return self.rpc(method, params)
        report = self.check(public_rpc_factory=lambda _: client, explorer_url='https://visitor.invalid')
        self.assertEqual(report['status'], 'CHECKED_NO_TRANSACTION_SAMPLE')
        self.assertEqual(report['check_retries'], {'rate_limit': 2, 'index_wait': 0})
        self.assertEqual([item[2] for item in attempts[:3]], [0, 1, 3])
        self.assertEqual(attempts[0][:2], attempts[2][:2])
        self.assertEqual(report['ingress_check'], 'override_origin')
        output = io.StringIO()
        with redirect_stdout(output):
            PUBLIC.print_check_report('check', report)
        self.assertIn('Check retries: rate-limit=2, index-wait=0', output.getvalue())

    def test_persistent_rate_limit_fails_without_nat_or_private_quota_advice(self):
        client = mock.Mock(side_effect=CHECK.RpcFailure('RPC_RATE_LIMIT', 'HTTP endpoint returned status 429'))
        with self.assertRaises(CHECK.RpcFailure) as caught:
            self.check(public_rpc_factory=lambda _: client)
        self.assertEqual(client.call_count, 3)
        self.assertEqual(caught.exception.category, 'RPC_RATE_LIMIT')
        self.assertIn('two rate-limit retries', str(caught.exception))
        self.assertNotIn('NAT', str(caught.exception))
        self.assertNotIn('private RPC quota', str(caught.exception))

    def test_non_transient_failures_are_not_retried(self):
        for category in ('RPC_TLS', 'RPC_AUTH', 'RPC_CONNECTION_REFUSED', 'RPC_TIMEOUT', 'RPC_INVALID_RESPONSE'):
            with self.subTest(category=category):
                operation = mock.Mock(side_effect=CHECK.RpcFailure(category, 'fixture failure'))
                with self.assertRaises(CHECK.RpcFailure):
                    self.requests.call(operation, indexed=True)
                self.assertEqual(operation.call_count, 1)
        self.assertEqual(self.requests.counts(), {'rate_limit': 0, 'index_wait': 0})

    def test_index_catchup_keeps_original_checkpoint_while_chain_advances(self):
        sample = self.rpc.api('/api/v2/blocks/sample')
        paths = []
        def api(url):
            paths.append(url)
            if len(paths) <= 2:
                self.rpc.height += 1
                raise missing()
            return sample
        report = self.check(api)
        self.assertEqual(report['checkpoint'], self.upstream['checkpoint'])
        self.assertEqual(len(set(paths)), 1)
        self.assertTrue(paths[0].endswith(self.upstream['checkpoint']['hash']))
        self.assertEqual(report['check_retries']['index_wait'], 2)
        self.assertEqual(report['status'], 'CHECKED_NO_TRANSACTION_SAMPLE')

    def test_reorg_or_wrong_indexed_hash_after_wait_is_still_rejected(self):
        for reorg in (True, False):
            with self.subTest(reorg=reorg):
                self.rpc.fork = False
                sample = self.rpc.api('/api/v2/blocks/sample')
                attempts = []
                def api(url):
                    attempts.append(url)
                    if len(attempts) == 1:
                        raise missing()
                    if reorg:
                        self.rpc.fork = True
                    else:
                        sample['hash'] = '0x' + 'ff' * 32
                    return sample
                with self.assertRaisesRegex(ValueError, 'canonical checkpoint|not indexed'):
                    self.check(api)
                self.assertEqual(len(attempts), 2)

    def test_index_window_is_shared_and_not_renewed_by_another_route(self):
        api = mock.Mock(side_effect=missing())
        with self.assertRaises(CHECK.RpcFailure) as caught:
            self.check(api)
        self.assertEqual(caught.exception.category, 'EXPLORER_SAMPLE_UNAVAILABLE')
        self.assertIn('30-second', str(caught.exception))
        self.assertGreaterEqual(self.clock.now(), 30)
        self.assertLess(self.clock.now(), 33)
        before = api.call_count
        with self.assertRaises(CHECK.RpcFailure):
            self.check(api, route_label='ingress.later-route')
        self.assertEqual(api.call_count - before, 1)
        self.assertEqual(self.requests.counts()['index_wait'], 15)

    def test_missing_list_route_is_not_treated_as_pending_indexing(self):
        self.rpc.height = 0
        self.rpc.transaction_block = None
        self.upstream = CHECK.preflight(self.config, self.identity, rpc_factory=self.client)
        api = mock.Mock(side_effect=missing())
        with self.assertRaises(CHECK.RpcFailure) as caught:
            self.check(api)
        self.assertEqual(caught.exception.category, 'RPC_HTTP')
        self.assertEqual(api.call_count, 1)
        self.assertEqual(self.requests.counts()['index_wait'], 0)

    def test_transaction_index_catchup_preserves_receipt_checks(self):
        self.config['rpc']['transaction'] = self.rpc.transaction
        self.upstream = CHECK.preflight(self.config, self.identity, rpc_factory=self.client)
        attempts = []
        def api(url):
            if '/transactions/' in url:
                attempts.append(url)
                if len(attempts) == 1:
                    raise missing()
            return self.rpc.api(url)
        self.assertEqual(self.check(api)['status'], 'CHECKED')
        self.assertEqual(len(attempts), 2)
        with self.assertRaisesRegex(ValueError, 'fee differs'):
            self.check(lambda url: {**self.rpc.api(url), 'fee': {'value': '0'}})

    def test_six_routes_share_one_budget_and_pass_gateway_quota(self):
        self.config['ingress'].update(bind_address='0.0.0.0', bind_address_ipv6='::')
        calls = []
        tokens, previous = 40.0, 0.0
        def transport(url, payload=None, **kwargs):
            nonlocal tokens, previous
            now = self.clock.now()
            tokens = min(40, tokens + (now - previous) * 10)
            previous = now
            if tokens < 1:
                raise CHECK.RpcFailure('RPC_RATE_LIMIT', 'shared proxy quota exceeded')
            tokens -= 1
            calls.append((now, url))
            return rpc_response(self.rpc, payload) if payload else self.rpc.api(url)
        def canonical(config, identity, **kwargs):
            return CHECK.check_explorer(config, identity, rpc_factory=self.client, **kwargs)
        with mock.patch.object(INGRESS, 'fetch', side_effect=transport), \
                mock.patch.object(INGRESS, 'check_explorer', side_effect=canonical):
            report = INGRESS.check_ingresses(self.config, self.identity, requests=self.requests,
                discover=lambda: (['192.0.2.10'], None), discover_ipv6=lambda: (['2001:db8::1', 'fd42::1'], None),
                upstream_check=lambda *_: self.upstream)
        self.assertEqual(len(report['ingress_results']), 6)
        self.assertTrue(all(row['status'] == 'PASSED' for row in report['ingress_results']))
        self.assertEqual(len(calls), 54)
        self.assertGreaterEqual(self.clock.now(), 10.5)
        self.assertEqual(report['check_retries'], {'rate_limit': 0, 'index_wait': 0})
        self.assertEqual(report['checkpoint'], self.upstream['checkpoint'])

    def test_rate_and_index_diagnoses_do_not_suggest_tls_or_nat_changes(self):
        for category in ('RPC_RATE_LIMIT', 'EXPLORER_SAMPLE_UNAVAILABLE'):
            report = {'upstream': {'status': 'PASSED'}, 'ingress_results': [
                {'name': 'loopback', 'status': 'PASSED'},
                {'name': 'configured', 'status': 'FAILED', 'error': {'category': category}},
            ]}
            diagnosis = INGRESS.diagnosis(report)
            self.assertNotIn('TLS', diagnosis)
            self.assertNotIn('NAT', diagnosis)
            self.assertIn('quota' if category == 'RPC_RATE_LIMIT' else 'indexing progress', diagnosis)


if __name__ == '__main__':
    unittest.main()
