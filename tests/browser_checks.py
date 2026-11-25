"""Браузерная приёмка локальной страницы. Только синтетические данные, без сети."""
import json
import os
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent.parent
CHROME = os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')


def snapshot(offset=0, event_offset=0):
    now = datetime.now(timezone.utc)
    return {'schema_version': 1, 'generated_at': (now+timedelta(seconds=offset)).isoformat(),
            'agents': [{'id': 'codex', 'status': 'editing', 'event_at': (now+timedelta(seconds=event_offset)).isoformat(),
                        'task': 'Браузерная проверка', 'source': 'codex_log'}],
            'events': [], 'observer': {'mode': 'read_only'}}


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=CHROME, headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.ctx = self.browser.new_context(viewport={'width': 1440, 'height': 960}, offline=True)
        self.external = []
        def guard(route):
            if route.request.url.startswith(('file:', 'data:', 'blob:')):
                route.continue_()
            else:
                self.external.append(route.request.url)
                route.abort()
        self.ctx.route('**/*', guard)
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on('pageerror', lambda e: self.errors.append(str(e)))
        self.page.goto((HERE/'index.html').as_uri())
        self.page.wait_for_function('() => window.agentOffice !== undefined')

    def tearDown(self):
        self.ctx.close()
        self.assertEqual(self.external, [])
        self.assertEqual(self.errors, [])

    def state(self):
        return self.page.evaluate('window.agentOffice.getState()')

    def test_wide_scene_and_states(self):
        self.assertTrue(self.state()['three'])
        self.assertEqual(self.state()['connection'], 'idle')
        self.assertFalse(any(s['typing'] for s in self.state()['seats']))
        self.page.locator('#roster button').nth(1).click()
        self.assertEqual(self.page.locator('#selName').inner_text(), 'Свободное место')
        self.page.locator('#camRight').click()
        self.page.locator('#btnPause').click()
        self.assertTrue(self.state()['paused'])
        self.page.evaluate('d => window.agentOffice.applySnapshot(d)', snapshot())
        self.assertTrue(self.state()['seats'][0]['typing'])
        self.page.evaluate('d => window.agentOffice.applySnapshot(d)', snapshot(event_offset=-100))
        self.assertFalse(self.state()['seats'][0]['typing'])
        self.page.evaluate('d => window.agentOffice.applySnapshot(d)', snapshot(offset=-20))
        self.assertEqual(self.state()['connection'], 'stale')
        self.page.evaluate('d => window.agentOffice.applySnapshot(d,{static:true})', snapshot())
        self.assertEqual(self.state()['connection'], 'static')
        self.assertFalse(any(s['typing'] for s in self.state()['seats']))
        (HERE/'qa').mkdir(exist_ok=True)
        self.page.screenshot(path=str(HERE/'qa/office-wide-initial.png'), full_page=True)

    def test_mobile_layout(self):
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.page.wait_for_timeout(400)
        self.assertTrue(self.state()['three'])
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), 390)
        self.page.locator('#roster button').nth(2).click()
        self.assertEqual(self.page.locator('#selName').inner_text(), 'Свободное место')
        (HERE/'qa').mkdir(exist_ok=True)
        self.page.screenshot(path=str(HERE/'qa/office-mobile-initial.png'), full_page=True)

    def test_live_read_error_stops_animation(self):
        # Подмена только в тестовом headless-контексте; пользовательские разрешения не обходятся
        self.page.evaluate('d => {window.testFileText=JSON.stringify(d); window.showOpenFilePicker=async()=>[{kind:"file",queryPermission:async()=>"granted",getFile:async()=>new File([window.testFileText],"test.json")}]}', snapshot())
        self.page.locator('#btnLive').click()
        self.page.wait_for_function('() => window.agentOffice.getState().connection === "live"')
        self.page.evaluate('window.testFileText="{broken"')
        self.page.wait_for_function('() => document.querySelector("#notice").dataset.level === "error"')
        self.assertNotEqual(self.state()['connection'], 'live')
        self.assertFalse(any(s['typing'] for s in self.state()['seats']))

    def test_future_agent_event_not_active(self):
        self.page.evaluate('d => window.agentOffice.applySnapshot(d)', snapshot(event_offset=3600))
        self.assertFalse(any(s['typing'] for s in self.state()['seats']))

    def test_helper_appears_and_leaves_without_duplicate_seats(self):
        data = snapshot()
        helper = {'id': 'team-office_review', 'name': 'Ревью офиса',
                  'role': 'Помощник Codex', 'task': 'Изолированное поручение',
                  'status': 'working', 'source': 'codex_team',
                  'event_at': data['generated_at']}
        data['agents'].extend([helper, helper.copy()])
        self.page.evaluate('d => window.agentOffice.applySnapshot(d)', data)
        self.assertEqual([s['id'] for s in self.state()['seats']].count(helper['id']), 1)
        self.page.locator('#roster button').nth(1).click()
        self.assertEqual(self.page.locator('#selName').inner_text(), 'Ревью офиса')
        self.page.evaluate('d => window.agentOffice.applySnapshot(d)', snapshot())
        self.assertNotIn(helper['id'], [s['id'] for s in self.state()['seats']])
        self.assertLess(self.state()['selected'], len(self.state()['seats']))

    def test_helper_text_is_not_html(self):
        data = snapshot()
        data['agents'].append({'id': 'helper', 'name': '<img src=x onerror=alert(1)>',
                              'status': 'done', 'event_at': data['generated_at'],
                              'source': 'codex_team'})
        self.page.evaluate('d => window.agentOffice.applySnapshot(d)', data)
        self.assertEqual(self.page.locator('#roster img').count(), 0)
        self.assertLessEqual(len(self.state()['seats']), 6)

    def test_helper_ids_cannot_shadow_object_properties(self):
        data = snapshot()
        for name in ('constructor', '__proto__', 'toString'):
            data['agents'].append({'id': name, 'name': name, 'status': 'done',
                                   'source': 'codex_team', 'event_at': data['generated_at']})
        self.page.evaluate('d => window.agentOffice.applySnapshot(d, {static:true})', data)
        self.page.wait_for_timeout(150)
        self.assertTrue(self.state()['three'])
        self.assertEqual(len(self.state()['actors']), 4)
        self.assertEqual(len({a['id'] for a in self.state()['actors']}), 4)
        self.page.evaluate('d => window.agentOffice.applySnapshot(d, {static:true})', data)
        self.assertEqual(len(self.state()['actors']), 4)
        self.page.evaluate('d => window.agentOffice.applySnapshot(d, {static:true})', snapshot())
        self.page.wait_for_timeout(150)
        self.assertEqual(len(self.state()['actors']), 1)

    def test_coffee_walk_chat_and_pause_keep_work_status(self):
        data = snapshot()
        for sid in ('rest-1', 'rest-2'):
            data['agents'].append({'id': sid, 'name': sid, 'status': 'done', 'event_at': data['generated_at']})
        # Seat the fixture first: arrival and spontaneous breaks are tested separately.
        # Waiting for all actors to sit simultaneously races their independent schedules.
        self.page.evaluate('d => window.agentOffice.applySnapshot(d, {static:true})', data)
        self.page.evaluate('''d => {
          const pulse = () => {
            d.generated_at = new Date().toISOString();
            d.agents[0].event_at = d.generated_at;
            window.agentOffice.applySnapshot(d);
          };
          pulse(); window.testPulse = setInterval(pulse, 1000);
        }''', data)
        self.page.locator('#btnCoffee').click()
        self.page.wait_for_function('() => window.agentOffice.getState().actors.some(a => a.pose === "walking")', timeout=15000)
        codex = next(a for a in self.state()['actors'] if a['id'] == 'codex')
        self.assertEqual(codex['pose'], 'seated')
        self.assertEqual(self.state()['seats'][0]['kind'], 'editing')
        self.page.wait_for_function('() => window.agentOffice.getState().actors.some(a => a.pose === "chat" && a.holding)', timeout=35000)
        (HERE/'qa').mkdir(exist_ok=True)
        self.page.screenshot(path=str(HERE/'qa/office-living-coffee.png'), full_page=True)
        self.page.locator('#btnPause').click()
        self.page.wait_for_timeout(200)
        before = self.page.locator('#c').screenshot()
        self.page.wait_for_timeout(800)
        self.assertEqual(before, self.page.locator('#c').screenshot(), 'Пауза должна замораживать изображение всей сцены')
        self.assertFalse(self.state()['motion'])
        self.assertEqual(self.state()['seats'][0]['kind'], 'editing')
        self.page.locator('#btnPause').click()
        self.assertTrue(self.state()['motion'])

    def test_static_snapshot_shows_helper_without_motion(self):
        data = snapshot()
        data['agents'].append({'id': 'helper', 'name': 'Помощник', 'status': 'assigned',
                              'source': 'codex_team', 'event_at': data['generated_at']})
        self.page.evaluate('d => window.agentOffice.applySnapshot(d, {static:true})', data)
        self.page.wait_for_timeout(300)
        helper = next(a for a in self.state()['actors'] if a['id'] == 'helper')
        self.assertTrue(helper['visible'])
        self.assertEqual(helper['pose'], 'seated')
        self.assertFalse(self.state()['motion'])
        self.assertEqual(self.page.evaluate('window.agentOffice.gatherCoffee()'), 0)

    def test_new_helper_enters_and_leaves_scene(self):
        data = snapshot()
        data['agents'].append({'id': 'helper', 'name': 'Помощник', 'status': 'assigned',
                              'source': 'codex_team', 'event_at': data['generated_at']})
        self.page.evaluate('d => window.agentOffice.applySnapshot(d)', data)
        self.page.wait_for_function('() => window.agentOffice.getState().actors.some(a => a.id === "helper" && a.pose === "walking" && a.visible)', timeout=15000)
        self.page.evaluate('d => window.agentOffice.applySnapshot(d)', snapshot())
        self.assertTrue(next(a for a in self.state()['actors'] if a['id'] == 'helper')['leaving'])
        self.page.wait_for_function('() => !window.agentOffice.getState().actors.some(a => a.id === "helper")', timeout=40000)

    def test_cancelled_old_read_cannot_replace_static_snapshot(self):
        self.page.evaluate('d => {window.testFileText=JSON.stringify(d); window.delayRead=false; window.showOpenFilePicker=async()=>[{kind:"file",queryPermission:async()=>"granted",getFile:async()=>{if(window.delayRead) await new Promise(r=>window.releaseRead=r);return new File([window.testFileText],"test.json")}}]}', snapshot())
        self.page.locator('#btnLive').click()
        self.page.wait_for_function('() => window.agentOffice.getState().connection === "live"')
        self.page.evaluate('window.delayRead=true')
        self.page.wait_for_function('() => typeof window.releaseRead === "function"')
        self.page.locator('#snapInput').set_input_files({'name': 'snapshot.json', 'mimeType': 'application/json', 'buffer': json.dumps(snapshot()).encode()})
        self.page.wait_for_function('() => window.agentOffice.getState().connection === "static"')
        self.page.evaluate('window.releaseRead()')
        self.page.wait_for_timeout(200)
        self.assertEqual(self.state()['connection'], 'static')

    def test_reduced_motion_and_no_three_fallback(self):
        self.page.emulate_media(reduced_motion='reduce')
        self.page.reload()
        self.assertTrue(self.state()['paused'])
        self.page.evaluate('d => window.agentOffice.applySnapshot(d)', snapshot())
        self.page.wait_for_timeout(200)
        before = self.page.locator('#labels .tag').first.evaluate('(e) => e.style.transform')
        self.page.locator('#camRight').click()
        self.page.wait_for_timeout(250)
        after = self.page.locator('#labels .tag').first.evaluate('(e) => e.style.transform')
        self.assertNotEqual(before, after, 'Положение метки в проекции должно меняться при повороте камеры, включая reduced motion')
        self.ctx.route('**/three.min.js', lambda route: route.abort())
        self.page.reload()
        self.assertFalse(self.state()['three'])
        self.assertIn('3D комната недоступна', self.page.locator('#sceneNote').inner_text())
        self.page.locator('#roster button').nth(1).click()
        self.assertEqual(self.page.locator('#selName').inner_text(), 'Свободное место')

    def test_demo_is_labelled_and_can_be_stopped(self):
        self.page.locator('#btnDemo').click()
        self.assertEqual(self.state()['connection'], 'demo')
        self.assertIn('ДЕМОНСТРАЦИЯ', self.page.locator('#connLabel').inner_text())
        self.assertEqual(len([s for s in self.state()['seats'] if s['type'] == 'agent']), 3)
        self.page.locator('#btnPause').click()
        self.page.wait_for_timeout(200)
        before = self.page.locator('#c').screenshot()
        self.page.wait_for_timeout(6500)
        self.assertEqual(before, self.page.locator('#c').screenshot())
        self.page.locator('#btnDemo').click()
        self.assertEqual(self.state()['connection'], 'idle')
        self.assertFalse(any(s['type'] == 'agent' for s in self.state()['seats']))

    def test_live_data_replaces_demo_and_keeps_custom_name(self):
        self.page.locator('#btnDemo').click()
        data = snapshot()
        data['agents'][0]['name'] = 'Мой разработчик'
        self.page.evaluate('d => {window.testFileText=JSON.stringify(d); window.showOpenFilePicker=async()=>[{kind:"file",queryPermission:async()=>"granted",getFile:async()=>new File([window.testFileText],"test.json")}]}', data)
        self.page.locator('#btnLive').click()
        self.page.wait_for_function('() => window.agentOffice.getState().connection === "live"')
        self.assertEqual([s['id'] for s in self.state()['seats'] if s['type'] == 'agent'], ['codex'])
        self.assertIn('Мой разработчик', self.page.locator('#roster').inner_text())
        self.page.wait_for_timeout(6500)
        self.assertEqual(self.state()['connection'], 'live')

    def test_demo_to_static_stays_static(self):
        self.page.locator('#btnDemo').click()
        self.page.locator('#snapInput').set_input_files({'name': 'snapshot.json', 'mimeType': 'application/json', 'buffer': json.dumps(snapshot()).encode()})
        self.page.wait_for_function('() => window.agentOffice.getState().connection === "static"')
        self.page.wait_for_timeout(6500)
        self.assertEqual(self.state()['connection'], 'static')
        self.assertFalse(self.state()['motion'])

    def test_demo_screenshots_and_guide(self):
        self.page.goto((HERE/'index.html').as_uri() + '#demo')
        self.page.wait_for_function('() => window.agentOffice.getState().connection === "demo"')
        self.page.wait_for_timeout(12000)
        self.assertTrue(self.state()['three'])
        self.page.screenshot(path=str(HERE/'docs/office-demo.png'), full_page=True)
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.page.wait_for_timeout(200)
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), 390)
        self.page.screenshot(path=str(HERE/'qa/office-demo-mobile.png'), full_page=True)
        self.page.goto((HERE/'guide.html').as_uri())
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), 390)
        self.assertIn('Поселите своих агентов', self.page.locator('h1').inner_text())


if __name__ == '__main__':
    unittest.main()
