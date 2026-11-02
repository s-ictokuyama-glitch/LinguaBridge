"""Replay known Japanese fixtures through Edge capture and verify real recording."""

import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import traceback
import wave

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--run-dir', type=Path, required=True)
parser.add_argument('--qa-tools', type=Path, default=ROOT / '.qa-tools')
parser.add_argument('--fixture-dir', type=Path, default=ROOT / 'tests/fixtures/ja')
parser.add_argument('--browser', default='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
args = parser.parse_args()
RUN = args.run_dir.resolve()
sys.path.insert(0, str(args.qa_tools.resolve()))
from playwright.sync_api import expect, sync_playwright

helper = ROOT / '.agents/skills/verify/scripts/instance.py'
doctor = subprocess.run([sys.executable, str(helper), 'doctor', '--run-dir', str(RUN)], capture_output=True, text=True, encoding='utf-8')
(RUN / 'doctor.json').write_text(doctor.stdout, encoding='utf-8')
assert doctor.returncode == 0, doctor.stderr
state = json.loads((RUN / 'instance.json').read_text(encoding='utf-8'))
effective = json.loads((RUN / 'effective-config.json').read_text(encoding='utf-8'))
assert effective['asr']['engine'] == 'sherpa'
assert effective['mt']['engine'] == 'hy-mt2'
assert effective['vad']['engine'] == 'silero'
out = RUN / 'audio-recording'
out.mkdir(exist_ok=False)
capture = out / 'microphone-input.wav'
inputs = []
cursor = 0
with wave.open(str(capture), 'wb') as writer:
    writer.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
    writer.writeframes(b'\0\0' * 16000 * 8)
    cursor += 8
    for fixture, recording in [('01.wav', False), ('03.wav', True), ('07.wav', False)]:
        path = args.fixture_dir / fixture
        with wave.open(str(path), 'rb') as reader:
            assert reader.getnchannels() == 1 and reader.getsampwidth() == 2 and reader.getframerate() == 16000
            frames = reader.readframes(reader.getnframes())
            duration = reader.getnframes() / 16000
        writer.writeframes(frames)
        inputs.append({'fixture': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'start_s': cursor, 'duration_s': duration, 'recording_expected': recording})
        writer.writeframes(b'\0\0' * 16000 * 12)
        cursor += duration + 12
    writer.writeframes(b'\0\0' * 16000 * 20)

result = {'status': 'fail', 'scope': 'Production CLI, real Edge getUserMedia/AudioWorklet/HTTP/WS, Silero VAD, ReazonSpeech K2 v2, Hy-MT2. Hardware microphone replaced by fixture WAV capture.', 'inputs': inputs, 'capture_sha256': hashlib.sha256(capture.read_bytes()).hexdigest(), 'actions': [], 'snapshots': {}}
frames = []
sent_pcm = bytearray()
page_errors = []
pages = {}
started = time.monotonic()

def save_frame(role, direction, payload):
    if isinstance(payload, bytes):
        if role == 'teacher' and direction == 'sent':
            sent_pcm.extend(payload)
        frames.append({'role': role, 'direction': direction, 't_s': time.monotonic() - started, 'binary_bytes': len(payload)})
    else:
        frames.append({'role': role, 'direction': direction, 't_s': time.monotonic() - started, 'message': json.loads(payload)})

def observe(label):
    snapshot = {}
    for role, page in pages.items():
        page.screenshot(path=str(out / f'{label}-{role}.png'), full_page=True)
        (out / f'{label}-{role}.aria.txt').write_text(page.locator('body').aria_snapshot(), encoding='utf-8')
        if role != 'teacher':
            snapshot[role] = page.locator('#cards .card').evaluate_all('(cards) => cards.map(c => ({seq:Number(c.dataset.seq),ja:c.querySelector(".ja").textContent,text:c.querySelector(".text").textContent}))')
        else:
            snapshot[role] = page.locator('#transcript li').all_text_contents()
    result['snapshots'][label] = snapshot

try:
    with sync_playwright() as pw, ExitStack() as resources:
        browser = pw.chromium.launch(executable_path=args.browser, headless=True, args=['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', f'--use-file-for-fake-audio-capture={capture}%noloop'])
        resources.callback(browser.close)
        result['browser'] = browser.version
        for role in ['teacher', 'en', 'zh']:
            context = browser.new_context(viewport={'width':1280,'height':900}, permissions=['microphone'] if role == 'teacher' else [])
            resources.callback(context.close)
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            resources.callback(context.tracing.stop, path=str(out / f'{role}-trace.zip'))
            page = context.new_page()
            pages[role] = page
            page.on('pageerror', lambda error: page_errors.append(str(error)))
            page.on('websocket', lambda ws, role=role: (ws.on('framesent', lambda payload: save_frame(role,'sent',payload)), ws.on('framereceived',lambda payload: save_frame(role,'received',payload))))
            page.goto(state['url'] + ('/teacher' if role == 'teacher' else '/'))
            if role == 'teacher':
                expect(page.locator('#start-btn')).to_be_enabled()
                code = page.locator('#join-code').inner_text()
            else:
                page.locator('#code-input').fill(code)
                page.locator(f'#lang-options button[data-code="{role}"]').click()
                page.locator('#join-btn').click()
                expect(page.locator('#caption-screen')).to_be_visible()
        teacher = pages['teacher']
        expect(teacher.locator('#stat-students')).to_have_text('2')
        expect(teacher.locator('#record-toggle')).not_to_be_checked()
        for page in pages.values():
            expect(page.locator('#record-indicator')).to_be_hidden()
        observe('00-before-start')
        teacher.locator('#start-btn').click()
        expect(teacher.locator('#session-state')).to_have_text('配信中')
        expect(pages['en'].locator('#session-banner')).to_have_text('Live')
        expect(pages['zh'].locator('#session-banner')).to_have_text('直播中')
        result['actions'].append({'action':'start_recording_off','t_s':time.monotonic()-started})
        for number, keyword in [(1,'植物'),(2,'教科書'),(3,'質問')]:
            expect(teacher.locator('#transcript li')).to_have_count(number, timeout=35000)
            for role in ['en','zh']:
                expect(pages[role].locator('#cards .card')).to_have_count(number, timeout=20000)
                expect(pages[role].locator('#cards .ja').last).to_contain_text(keyword)
                assert pages[role].locator('#cards .text').last.inner_text().strip()
            observe(f'0{number}-caption')
            assert not list(Path(state['data_dir']).rglob('transcript*')), 'Files were written before End'
            if number == 1:
                teacher.locator('#record-toggle').check()
                for page in pages.values():
                    expect(page.locator('#record-indicator')).to_be_visible()
                result['actions'].append({'action':'recording_on_after_seq','seq':1,'t_s':time.monotonic()-started})
            elif number == 2:
                teacher.locator('#record-toggle').uncheck()
                for page in pages.values():
                    expect(page.locator('#record-indicator')).to_be_hidden()
                result['actions'].append({'action':'recording_off_after_seq','seq':2,'t_s':time.monotonic()-started})
        result['capture_state'] = teacher.evaluate('({secure:window.isSecureContext,audio:state.audioCtx.state,rate:state.audioCtx.sampleRate,micReady:state.micReady,tracks:state.micStream.getAudioTracks().map(t=>({state:t.readyState,label:t.label}))})')
        assert result['capture_state']['micReady'] and result['capture_state']['audio'] == 'running'
        assert any(f.get('binary_bytes',0)>0 and f['role']=='teacher' for f in frames)
        teacher.locator('#end-btn').click()
        expect(teacher.locator('#session-state')).to_have_text('終了', timeout=20000)
        expect(pages['en'].locator('#session-banner')).to_have_text('Class has ended')
        expect(pages['zh'].locator('#session-banner')).to_have_text('课程已结束')
        result['actions'].append({'action':'end','t_s':time.monotonic()-started})
        observe('04-ended')
        saved = list(Path(state['data_dir']).rglob('transcript.jsonl'))
        assert len(saved)==1, saved
        entries = [json.loads(line) for line in saved[0].read_text(encoding='utf-8').splitlines()]
        assert [entry['seq'] for entry in entries] == [2], entries
        entry = entries[0]
        assert set(entry) == {'seq','created_at','t_start','t_end','ja','asr_ms','translations'}, entry
        assert set(entry['translations']) == {'en','zh'}
        for lang in ['en','zh']:
            cards = result['snapshots']['04-ended'][lang]
            assert [c['seq'] for c in cards] == [1,2,3], cards
            assert cards[1]['ja'] == entry['ja']
            assert cards[1]['text'] == entry['translations'][lang]['text']
            assert entry['translations'][lang]['engine'] == 'hy-mt2'
            assert not cards[1]['text'].startswith(f'[{lang}]')
            markdown = saved[0].with_name(f'transcript.{lang}.md').read_text(encoding='utf-8')
            assert entry['translations'][lang]['text'] in markdown
            assert len([line for line in markdown.splitlines() if line.startswith('- ')]) == 1
        assert re.search(r'textbook',entry['translations']['en']['text'],re.I)
        assert re.search(r'42|forty.two',entry['translations']['en']['text'],re.I)
        assert re.search(r'教科书|课本',entry['translations']['zh']['text'])
        assert '42' in entry['translations']['zh']['text'] or '四十二' in entry['translations']['zh']['text']
        ja_markdown=saved[0].with_name('transcript.ja.md').read_text(encoding='utf-8')
        assert entry['ja'] in ja_markdown
        assert len([line for line in ja_markdown.splitlines() if line.startswith('- ')]) == 1
        assert not page_errors, page_errors
        evidence = RUN / 'recording-evidence'
        shutil.copytree(saved[0].parent,evidence)
        result.update(status='pass',saved_entries=entries,page_errors=page_errors,recording_evidence=str(evidence),preserved_files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in evidence.iterdir()})
except Exception:
    result['status']='fail'
    result['error']=traceback.format_exc()
finally:
    (out/'sent-pcm16le.raw').write_bytes(sent_pcm)
    result['sent_pcm'] = {'bytes': len(sent_pcm), 'sha256': hashlib.sha256(sent_pcm).hexdigest(), 'format': 'PCM16LE mono 16000 Hz, application teacher WebSocket binary frames'}
    (out/'websocket-frames.json').write_text(json.dumps(frames,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':result['status'],'evidence':str(out),'error':result.get('error')},ensure_ascii=False),flush=True)

raise SystemExit(0 if result['status']=='pass' else 1)
