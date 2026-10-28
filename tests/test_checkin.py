import json
import logging
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import checkin as app


def response(payload, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode('utf-8')
    return result


class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.previous_log_level = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, self.previous_log_level)
        network_guard = patch(
            'requests.sessions.Session.request',
            side_effect=AssertionError('Regression tests must not use the network'),
        )
        network_guard.start()
        self.addCleanup(network_guard.stop)


class CookieCoverageTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.cookies = ['fake-cookie-1', 'fake-cookie-2']
        with patch.dict(os.environ, {app.Config.ENV_COOKIES: '&'.join(self.cookies)}, clear=True):
            self.config = app.Config()
        self.probes = {}
        self.checkins = {}
        self.attempts = []

        def fake_request(api, url, method, data=None, cookies='', user_agent=None):
            key = (self.cookies.index(cookies) + 1, api.domain)
            if url.endswith('/status'):
                code = self.probes.get(key, -2)
                return None if code is None else response({'code': code, 'data': {'leftDays': '12.8'}})
            if url.endswith('/checkin'):
                self.attempts.append((key, user_agent))
                raw = self.checkins.get(key, {'code': -2, 'message': 'No permission'})
                if isinstance(raw, list):
                    raw = raw.pop(0)
                return None if raw is None else response(raw)
            if url.endswith('/points'):
                return response({'code': 0, 'points': '123.0'})
            raise AssertionError(f'Unexpected request: {method} {url}')

        request_patch = patch.object(app.API, '_make_request', new=fake_request)
        request_patch.start()
        self.addCleanup(request_patch.stop)

    def allow(self, index, domain, code=0, points=5, probe=0):
        key = (index, domain)
        self.probes[key] = probe
        self.checkins[key] = {'code': code, 'message': 'ok', 'points': points}

    def run_checker(self):
        checker = app.Checker(self.config)
        checker.checkin_all()
        return checker, checker.format_results()

    def run_main(self):
        with patch.dict(os.environ, {app.Config.ENV_COOKIES: '&'.join(self.cookies)}, clear=True):
            with patch.object(app, 'PushService') as push:
                exit_code = app.main()
        return exit_code, push.return_value.send.call_args.args[1]

    def test_one_invalid_cookie_is_not_hidden_by_another_cookie(self):
        self.allow(2, 'railgun.info')
        _, payload = self.run_checker()
        self.assertEqual({row['cookie'] for row in payload['rows']}, {1, 2})
        self.assertEqual(payload['success'], 1)
        self.assertEqual(payload['fail'], 2)
        self.assertIn('Cookie 1', payload['note'])
        self.assertIn('Cookie认证失败', payload['title'])

    def test_all_invalid_cookies_keep_diagnostics(self):
        _, payload = self.run_checker()
        self.assertEqual(payload['success'], 0)
        self.assertEqual(payload['fail'], 4)
        self.assertEqual(len(payload['rows']), 4)
        self.assertIn('GLADOS_COOKIES', payload['note'])
        self.assertIn('No permission', app.render_payload(payload))

    def test_separate_domain_cookies_do_not_produce_cross_domain_failures(self):
        self.allow(1, 'glados.cloud')
        self.allow(2, 'railgun.info')
        _, payload = self.run_checker()
        self.assertEqual((payload['success'], payload['fail']), (2, 0))
        self.assertEqual({row['domain'] for row in payload['rows']}, set(app.Config.DOMAINS))

    def test_known_cross_domain_denials_are_not_checkin_tasks(self):
        self.config.cookies_list = self.cookies[:1]
        self.allow(1, 'glados.cloud')
        _, payload = self.run_checker()
        self.assertEqual([key for key, _ in self.attempts], [(1, 'glados.cloud')])
        self.assertEqual(payload['fail'], 0)

    def test_network_probe_failure_does_not_skip_a_valid_domain(self):
        self.config.cookies_list = self.cookies[:1]
        self.allow(1, 'glados.cloud', probe=None)
        self.allow(1, 'railgun.info')
        _, payload = self.run_checker()
        self.assertEqual((payload['success'], payload['fail']), (2, 0))

    def test_unknown_probe_code_does_not_skip_device_recovery(self):
        self.config.cookies_list = self.cookies[:1]
        self.allow(1, 'glados.cloud', probe=4)
        self.allow(1, 'railgun.info')
        self.checkins[(1, 'glados.cloud')] = [
            {'code': 4, 'reason': 'device-mismatch', 'loginDevice': 'Linux'},
            {'code': 0, 'message': 'ok', 'points': 5},
        ]
        _, payload = self.run_checker()
        self.assertEqual((payload['success'], payload['fail']), (2, 0))
        glados_uas = [ua for key, ua in self.attempts if key == (1, 'glados.cloud')]
        self.assertEqual(glados_uas, [app.PLATFORM_UA['Windows'], app.PLATFORM_UA['Linux']])

    def test_fallback_can_recover_without_reporting_cross_domain_denial(self):
        self.config.cookies_list = self.cookies[:1]
        self.allow(1, 'glados.cloud', probe=None)
        self.probes[(1, 'railgun.info')] = None
        _, payload = self.run_checker()
        self.assertEqual((payload['success'], payload['fail']), (1, 0))
        self.assertEqual([row['domain'] for row in payload['rows']], ['glados.cloud'])

    def test_network_checkin_failure_is_preserved_next_to_success(self):
        self.config.cookies_list = self.cookies[:1]
        self.probes[(1, 'glados.cloud')] = None
        self.checkins[(1, 'glados.cloud')] = None
        self.allow(1, 'railgun.info')
        _, payload = self.run_checker()
        self.assertEqual((payload['success'], payload['fail']), (1, 1))
        self.assertIn('网络请求失败', app.render_payload(payload))
        self.assertNotIn('Cookie认证失败', payload['title'])

    def test_previously_authenticated_domain_failure_is_not_hidden(self):
        self.config.cookies_list = self.cookies[:1]
        self.probes[(1, 'glados.cloud')] = 0
        self.allow(1, 'railgun.info')
        _, payload = self.run_checker()
        self.assertEqual((payload['success'], payload['fail']), (1, 1))
        self.assertIn('glados.cloud', payload['note'])

    def test_repeat_is_complete_and_carries_no_failure(self):
        self.allow(1, 'glados.cloud', code=1)
        self.allow(2, 'railgun.info', code=1)
        _, payload = self.run_checker()
        self.assertEqual((payload['success'], payload['repeat'], payload['fail']), (0, 2, 0))

    def test_report_uses_points_from_the_checkin_response(self):
        self.allow(1, 'glados.cloud', points=7)
        self.allow(2, 'railgun.info', points=3)
        _, payload = self.run_checker()
        self.assertEqual([row['points'] for row in payload['rows']], ['7', '3'])
        self.assertIn('获得 7 积分', app.render_payload(payload))

    def test_reusing_checker_does_not_accumulate_stale_results(self):
        self.allow(1, 'glados.cloud')
        self.allow(2, 'railgun.info')
        checker, _ = self.run_checker()
        self.allow(1, 'glados.cloud', code=1)
        self.allow(2, 'railgun.info', code=1)
        checker.checkin_all()
        payload = checker.format_results()
        self.assertEqual((payload['success'], payload['repeat'], payload['fail']), (0, 2, 0))
        self.assertEqual(len(payload['rows']), 2)

    def test_main_returns_failure_after_pushing_all_failed_results(self):
        exit_code, payload = self.run_main()
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload['fail'], 4)

    def test_main_returns_failure_after_pushing_partial_failure(self):
        self.allow(2, 'railgun.info')
        exit_code, payload = self.run_main()
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload['fail'], 2)

    def test_main_returns_success_for_completed_signins(self):
        self.allow(1, 'glados.cloud')
        self.allow(2, 'railgun.info')
        exit_code, payload = self.run_main()
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload['success'], 2)

    def test_main_returns_success_for_already_completed_signins(self):
        self.allow(1, 'glados.cloud', code=1)
        self.allow(2, 'railgun.info', code=1)
        exit_code, payload = self.run_main()
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload['repeat'], 2)


class APIDiagnosticsTests(OfflineTest):
    def status(self, value):
        with app.API('glados.cloud') as api:
            with patch.object(api, '_make_request', return_value=value):
                return api.get_status('fake-cookie')

    def test_network_failure_is_distinct_from_auth_failure(self):
        self.assertIsNone(self.status(None)[1])
        self.assertEqual(self.status(response({'code': -2, 'message': '没有权限'}))[1], -2)

    def test_malformed_response_is_uncertain(self):
        bad = response({})
        bad._content = b'<html>gateway error</html>'
        self.assertIsNone(self.status(bad)[1])

    def test_missing_code_is_uncertain(self):
        self.assertIsNone(self.status(response({'data': {}}))[1])

    def test_invalid_days_do_not_invalidate_authentication(self):
        self.assertEqual(self.status(response({'code': 0, 'data': {'leftDays': 'unknown'}})), ('None 天', 0))

    def test_missing_days_do_not_invalidate_authentication(self):
        self.assertEqual(self.status(response({'code': 0, 'data': {}})), ('None 天', 0))

    def test_only_explicit_auth_rejection_is_classified_as_auth_failure(self):
        with app.API('glados.cloud') as api:
            self.assertTrue(api._parse_checkin({'code': -2, 'message': 'No permission'})['auth_failed'])
            self.assertFalse(api._parse_checkin(None)['auth_failed'])
            self.assertFalse(api._parse_checkin({'message': 'gateway error'})['auth_failed'])
            self.assertFalse(api._parse_checkin({'code': 4, 'reason': 'blocked'})['auth_failed'])

    def test_http_failure_does_not_masquerade_as_authentication_failure(self):
        with app.API('glados.cloud') as api:
            with patch.object(api.session, 'get', return_value=response({'message': 'unavailable'}, 503)):
                self.assertIsNone(api.get_status('fake-cookie')[1])

    def test_request_exception_is_reported_as_uncertain(self):
        with app.API('glados.cloud') as api:
            with patch.object(api.session, 'get', side_effect=requests.Timeout('test timeout')):
                self.assertIsNone(api.get_status('fake-cookie')[1])


class MainConfigurationTests(OfflineTest):
    def test_missing_cookies_report_failure_after_push(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(app, 'PushService') as push:
            self.assertEqual(app.main(), 1)
        push.return_value.send.assert_called_once()
        self.assertIn('GLADOS_COOKIES', push.return_value.send.call_args.args[1]['note'])

    def test_invalid_cookie_configuration_reports_failure_after_push(self):
        with patch.dict(os.environ, {app.Config.ENV_COOKIES: '&'}, clear=True), patch.object(app, 'PushService') as push:
            self.assertEqual(app.main(), 1)
        push.return_value.send.assert_called_once()

    def test_unexpected_error_reports_failure_after_push(self):
        with patch.object(app, 'Config', side_effect=RuntimeError('test config error')), patch.object(app, 'PushService') as push:
            self.assertEqual(app.main(), 1)
        push.return_value.send.assert_called_once()
        self.assertIn('test config error', push.return_value.send.call_args.args[1]['note'])

    def test_command_line_exits_nonzero_without_cookies(self):
        env = dict(os.environ)
        for name in (app.Config.ENV_COOKIES, app.Config.ENV_PUSH_KEY, app.Config.ENV_SERVERCHAN_KEY, app.Config.ENV_EXCHANGE_PLAN, app.Config.ENV_VERBOSE):
            env.pop(name, None)
        env['PYTHONUTF8'] = '1'
        completed = subprocess.run(
            [sys.executable, str(Path(app.__file__).resolve())],
            env=env, capture_output=True, timeout=15,
        )
        self.assertEqual(completed.returncode, 1, completed.stderr.decode('utf-8', errors='replace'))


if __name__ == '__main__':
    unittest.main()
