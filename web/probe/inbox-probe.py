#!/usr/bin/env python3
"""Mail conversation browser regression, with isolated API fixtures.

Run with system Python against the web dev server. No real messages are sent.
"""
import json
import os
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

URL = os.environ.get('PULSAR_UI_URL', 'http://127.0.0.1:5292/')


def message(id, body, time, **overrides):
    return dict(id=id, body=body, created_at=f'2026-09-16T12:00:{time:02}+00:00',
                channel='projects', thread_key='fixture-mail', source_kind='pulse',
                source_label='Fixture pulse', source_pulse_id='fixture-pulse',
                title='Mail fixture', read_at=None, needs_response=False,
                priority='quiet', **overrides)


class MailFixture:
    def __init__(self, standalone=False):
        self.latest = message('latest', 'Question for you', 2)
        self.older = message('older', 'Older archived message' + '\n\nEarlier detail' * 60, 1)
        self.older['read_at'] = self.older['created_at']
        if standalone:
            self.latest['thread_key'] = None
        self.messages = [self.latest] if standalone else [self.older, self.latest]
        self.fail_reply = False
        self.fetches = 0
        self.sent = 0

    def handle(self, route):
        req = route.request
        parsed = urlparse(req.url)
        path = parsed.path.removeprefix('/api')
        def send(value, status=200):
            payload = {'ok': True, 'value': value} if status == 200 else {
                'ok': False, 'error': {'code': 'fixture_failure', 'message': value}}
            route.fulfill(status=status, content_type='application/json', body=json.dumps(payload))
        if path == '/inbox':
            # Only the newest received message is in the list page.
            return send([self.latest])
        if path == '/inbox/unread-count':
            return send(dict(total=0, channels={}, threads={}))
        if path == '/inbox/channels':
            return send([])
        if path == '/pulses':
            return send([])
        if path.endswith('/conversation'):
            self.fetches += 1
            return send(self.messages)
        if path.endswith('/reply'):
            if self.fail_reply:
                return send('Reply delivery failed', 502)
            self.sent += 1
            self.latest['thread_key'] = 'fixture-mail'
            self.latest['replied_at'] = '2026-09-16T12:00:03+00:00'
            reply = message(f'sent-{self.sent}', req.post_data_json['text'], 2 + self.sent)
            reply.update(source_kind='user', source_label='You', read_at=reply['created_at'])
            self.messages.append(reply)
            return send(self.latest)
        if path.endswith('/read'):
            self.latest['read_at'] = self.latest['created_at']
            return send(self.latest)
        # Don't allow the fixture to write to production.
        if req.method != 'GET':
            return send(None)
        route.continue_()


def scenario(browser, width, standalone):
    fixture = MailFixture(standalone)
    context = browser.new_context(viewport={'width': width, 'height': 900})
    context.route('**/api/**', fixture.handle)
    context.add_init_script("""
        localStorage.setItem('pulsar-show-metrics-bar', '0');
        window.WebSocket = class { constructor() { throw new Error('No live fixture sockets'); } };
    """)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(URL + '?surface=inbox&item=latest')
    expect(page.locator('.d-body')).to_contain_text('Question for you')
    if not standalone:
        expect(page.locator('.d-body')).to_contain_text('Older archived message')
    composer = page.locator('.composer textarea')
    composer.fill('Yes, go ahead')
    page.locator('button.send').click()
    expect(page.locator('.msg.from-user')).to_contain_text('Yes, go ahead')
    expect(page.locator('.msg.from-user .msg-who')).to_have_text('You')
    expect(page.locator('.msg.from-user')).to_be_in_viewport()
    expect(composer).to_have_value('')
    expect(page.locator('.msg-card').first).to_have_text('Yes, go ahead')
    assert page.locator('.d-body').evaluate('(el) => el.scrollTop') == 0
    page.reload()
    page.get_by_role('tab', name='Archive', exact=True).click()
    page.locator('.row').filter(has_text='Mail fixture').click()
    expect(page.locator('.msg.from-user')).to_contain_text('Yes, go ahead')
    # A pulse answer joins the conversation on the next poll/visibility resume.
    fixture.messages.append(message('answer', 'Work completed', 8))
    page.evaluate("""() => {
        Object.defineProperty(document, 'visibilityState', {value:'hidden', configurable:true});
        document.dispatchEvent(new Event('visibilitychange'));
        Object.defineProperty(document, 'visibilityState', {value:'visible', configurable:true});
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    expect(page.locator('.d-body')).to_contain_text('Work completed')
    bodies = page.locator('.msg-card').all_text_contents()
    assert bodies[:3] == ['Work completed', 'Yes, go ahead', 'Question for you'], bodies
    fixture.fail_reply = True
    composer.fill('This should fail')
    page.locator('button.send').click()
    expect(page.locator('.err-strip')).to_contain_text('Reply delivery failed')
    expect(composer).to_have_value('This should fail')
    expect(page.locator('.msg.from-user')).to_have_count(1)
    assert fixture.sent == 1
    assert fixture.fetches < 10, f'Conversation fetch loop: {fixture.fetches}'
    assert not errors, errors
    context.close()
    print(f'PASS mail conversation: width={width}, standalone={standalone}')


if __name__ == '__main__':
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        scenario(browser, 1440, False)
        scenario(browser, 390, True)
        browser.close()
