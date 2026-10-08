"""Offline API checks. No real model calls or order writes."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class ServerTests(unittest.TestCase):
    def setUp(self):
        server.conversations.clear()
        self.client = server.app.test_client()
        self.env = patch.dict(os.environ, OPENAI_API_KEY="test-only", OPENAI_MODEL="fake")
        self.env.start()
        self.addCleanup(self.env.stop)

    def state(self, client=None):
        client = client or self.client
        client.post('/api/reset', json={})
        with client.session_transaction() as session:
            return server.conversations[session['chat_id']]

    def test_streaming_and_session_isolation(self):
        def fake_chat(client, model, memory, text, on_text, on_status):
            memory.start_turn(text)
            on_status('Looking up catalog data…')
            on_text('Wine A')
            memory.add_reply('Wine A')
            memory.pending_order = {'order_id': 'draft-1'}
            return 'Wine A'
        with patch('server.chat', side_effect=fake_chat):
            response = self.client.post('/api/chat', json={'text': 'search'})
            events = [json.loads(line) for line in response.data.splitlines()]
        self.assertEqual([event['type'] for event in events], ['status', 'text', 'done'])
        self.assertEqual(events[-1]['draft']['order_id'], 'draft-1')
        other = self.state(server.app.test_client())
        self.assertEqual(other['memory'].messages, [])

    def test_confirm_uses_server_draft_and_prevents_duplicate(self):
        state = self.state()
        draft = {'order_id': 'draft-1', 'quantity': 2, 'total_cents': 2400}
        state['memory'].pending_order = draft
        with patch('server.submit_order', return_value={'order_id': 'draft-1'}) as submit:
            response = self.client.post('/api/order', json={
                'action': 'confirm', 'order_id': 'draft-1', 'total_cents': 1})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['confirmation']['order_id'], 'draft-1')
            submit.assert_called_once_with(draft)
            response = self.client.post('/api/order', json={'action': 'confirm', 'order_id': 'draft-1'})
            self.assertEqual(response.status_code, 409)
            submit.assert_called_once()

    def test_export_failure_retains_draft(self):
        state = self.state()
        state['memory'].pending_order = {'order_id': 'draft-1'}
        with patch('server.submit_order', side_effect=OSError('test')):
            response = self.client.post('/api/order', json={'action': 'confirm', 'order_id': 'draft-1'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json['draft']['order_id'], 'draft-1')

    def test_cancel_does_not_submit(self):
        state = self.state()
        state['memory'].pending_order = {'order_id': 'draft-1'}
        with patch('server.submit_order') as submit:
            response = self.client.post('/api/order', json={'action': 'cancel', 'order_id': 'draft-1'})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(state['memory'].pending_order)
        submit.assert_not_called()
        self.assertIsNone(response.json['confirmation'])

    def test_changed_stock_does_not_return_confirmation(self):
        state = self.state()
        state['memory'].pending_order = {'order_id': 'draft-1'}
        with patch('server.submit_order', return_value={'error': 'Stock changed. Prepare a new order.'}):
            response = self.client.post('/api/order', json={'action': 'confirm', 'order_id': 'draft-1'})
        self.assertIsNone(response.json['confirmation'])
        self.assertIn('Stock changed', response.json['reply'])

    def test_validation_and_origin(self):
        for text in ('', None, 5, 'x' * 4001):
            self.assertEqual(self.client.post('/api/chat', json={'text': text}).status_code, 400)
        response = self.client.post('/api/reset', json={}, headers={'Origin': 'https://evil.example'})
        self.assertEqual(response.status_code, 403)

    def test_health_does_not_require_provider_settings(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get('/api/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {'ok': True, 'storage': 'local'})

    def test_homepage_does_not_reuse_stale_bundle_html(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'index.html'
            path.write_text('<p>old bundle</p>')
            os.utime(path, (1540000000, 1540000000))
            with patch.object(server, 'FRONTEND', Path(folder)):
                first = self.client.get('/')
                stale_tag = first.headers.get('ETag', '"previous-deployment"')
                first.close()
                path.write_text('<p>new bundle</p>')
                os.utime(path, (1540000000, 1540000000))
                second = self.client.get('/', headers={
                    'If-None-Match': stale_tag,
                    'If-Modified-Since': 'Sat, 20 Oct 2018 01:46:40 GMT'})
                self.addCleanup(second.close)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data, b'<p>new bundle</p>')
        self.assertEqual(second.headers['Cache-Control'], 'no-store')
        self.assertNotIn('ETag', second.headers)

    def test_busy_session_rejected(self):
        state = self.state()
        state['lock'].acquire()
        try:
            response = self.client.post('/api/chat', json={'text': 'hello'})
            self.assertEqual(response.status_code, 409)
        finally:
            state['lock'].release()


if __name__ == '__main__':
    unittest.main()
