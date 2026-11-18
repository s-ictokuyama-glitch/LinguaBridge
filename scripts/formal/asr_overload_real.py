"""Compare real-model captions before, during, and after shipped ASR capacity overflow."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
import wave

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.formal.lesson_endurance import pid_exists
from scripts.formal.lesson_liveness_real import Socket
from server.audio.vad import Segment, SileroVAD
from server.config import load_config
from server.inference_process import ProcessInferenceRunner
from server.main import create_app
from server.publication import Publication


async def check_case(config, inputs, overloaded):
    pipeline = create_app(config, Publication(config.server)).state.pipeline
    teacher = Socket()
    students = {lang: Socket() for lang in ('en', 'zh')}
    row = {'offers': [], 'phases': {}}
    pids = []
    offer = pipeline._asr_backlog.offer

    def observe(event):
        outcome = offer(event)
        if isinstance(event, Segment):
            row['offers'].append({'block': block, 'samples': int(event.pcm.size),
                                  'sha256': hashlib.sha256(event.pcm.tobytes()).hexdigest(),
                                  'result': outcome.value})
        return outcome

    async def drain():
        await pipeline.drain(timeout=180)
        await asyncio.wait_for(asyncio.gather(*(
            sender._queue.join() for sender in pipeline.roster._senders.values())), timeout=10)
        assert pipeline.queue_depth == pipeline.audio_queue_seconds == pipeline.asr_active_seconds == 0
        assert not pipeline._assembler.has_pending

    try:
        await pipeline.roster.add(teacher, 'teacher')
        for lang, socket in students.items():
            await pipeline.roster.add(socket, 'student', lang)
        await pipeline.start()
        await asyncio.wait_for(pipeline._warmup_task, timeout=180)
        assert pipeline.ready and isinstance(pipeline._segmenter._vad, SileroVAD)
        runners = (pipeline._asr_runner, pipeline.captions._mt_runner)
        assert all(isinstance(runner, ProcessInferenceRunner) for runner in runners)
        pids = [runner._generation.process.pid for runner in runners]
        assert all(pid_exists(pid) for pid in pids)
        row['child_pids'] = pids
        row['engines'] = [type(pipeline._asr).__name__, type(pipeline.captions._mt).__name__]
        await pipeline.set_accepting_audio(True)
        await drain()
        pipeline._asr_backlog.offer = observe
        for label, pcm in inputs.items():
            teacher_start = len(teacher.frames)
            starts = {lang: len(socket.frames) for lang, socket in students.items()}
            calls = (pipeline.asr_calls, pipeline.captions.metrics().calls)
            batch = overloaded and label == 'burst'
            probe = []
            if batch:
                asyncio.get_running_loop().call_soon(probe.append, True)
            for index in range(12 if label == 'burst' else 1):
                block = f'{label}-{index}'
                offer_start, final_start = len(row['offers']), len(teacher.frames)
                for offset in range(0, len(pcm), 3200):
                    await pipeline.feed_audio(pcm[offset:offset + 3200])
                for _ in range(12):
                    await pipeline.feed_audio(bytes(3200))
                await pipeline.flush_audio()
                if not batch:
                    await drain()
                    if label == 'burst':
                        assert len(row['offers']) - offer_start == 1, 'burst fixture must yield one segment per block'
                        assert len([f for f in teacher.frames[final_start:] if f['type'] == 'asr_final']) == 1
            pending = pipeline.audio_queue_seconds
            if batch:
                assert not probe and pipeline.asr_calls == calls[0], 'burst yielded to the ASR worker'
                assert 0 < pending <= config.limits.asr_queue_seconds
            await drain()
            finals = [f for f in teacher.frames[teacher_start:] if f['type'] == 'asr_final']
            captions = {
                lang: [f for f in socket.frames[starts[lang]:] if f['type'] == 'caption']
                for lang, socket in students.items()
            }
            errors = [f for f in teacher.frames[teacher_start:] if f['type'] == 'error']
            assert finals and all(f['code'] == 'audio_dropped' and batch for f in errors), errors
            for lang, frames in captions.items():
                assert [(f['seq'], f['ja']) for f in frames] == [(f['seq'], f['ja']) for f in finals]
                assert all(f['lang'] == lang and f['text'].strip() for f in frames)
            row['phases'][label] = {'finals': finals, 'captions': captions, 'errors': errors,
                                    'asr_calls': pipeline.asr_calls - calls[0],
                                    'mt_calls': pipeline.captions.metrics().calls - calls[1],
                                    'queued_seconds_before_drain': pending}
            print(f'{"overload" if overloaded else "baseline"} {label}: {len(finals)} finals', flush=True)
        row.update(dropped_segments=pipeline.asr_dropped_segments, dropped_seconds=pipeline.asr_dropped_seconds)
        assert pipeline.audio_frames_rejected == 0
        assert all(s.closed is None for s in [teacher, *students.values()])
        all_finals = [f for f in teacher.frames if f['type'] == 'asr_final']
        assert [f['seq'] for f in all_finals] == list(range(1, len(all_finals) + 1))
    finally:
        await pipeline.stop()
        row['all_children_reaped'] = all(not pid_exists(pid) for pid in pids)
        assert row['all_children_reaped']
    return row


async def run(args):
    config = load_config(ROOT / 'config.yaml')
    if args.models_dir:
        config.models.dir = str(args.models_dir.resolve())
    assert (config.vad.engine, config.asr.engine, config.mt.engine, config.turn.strategy) == (
        'silero', 'haas-onnx', 'hy-mt2', 'morph')
    assert config.limits.asr_queue_seconds == config.vad.max_utterance_s == config.turn.morph.max_turn_s == 30
    paths = {key: ROOT / f'tests/fixtures/ja/{number}.wav' for key, number in (
        ('before', '03'), ('burst', '02'), ('after', '10'))}
    inputs = {}
    for label, path in paths.items():
        with wave.open(str(path)) as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
            inputs[label] = wav.readframes(wav.getnframes())
    baseline = await check_case(config, inputs, False)
    overload = await check_case(config, inputs, True)
    assert [(f['block'], f['samples'], f['sha256']) for f in baseline['offers']] == [
        (f['block'], f['samples'], f['sha256']) for f in overload['offers']]
    assert baseline['dropped_segments'] == 0
    budget = int(config.limits.asr_queue_seconds * 16000)
    admitted_samples = 0
    expected_admitted = []
    expected_rejected = []
    for segment in (f for f in baseline['offers'] if f['block'].startswith('burst-')):
        assert 0 < segment['samples'] <= budget
        if admitted_samples + segment['samples'] <= budget:
            expected_admitted.append(segment['block'])
            admitted_samples += segment['samples']
        else:
            expected_rejected.append(segment['block'])
    rejected = [f for f in overload['offers'] if f['result'] == 'dropped']
    admitted = [f for f in overload['offers'] if f['block'].startswith('burst-') and f['result'] == 'accepted']
    assert [f['block'] for f in admitted] == expected_admitted
    assert [f['block'] for f in rejected] == expected_rejected
    assert rejected and len(rejected) == overload['dropped_segments']
    assert abs(sum(f['samples'] / 16000 for f in rejected) - overload['dropped_seconds']) < 1e-9
    for label in inputs:
        control, actual = baseline['phases'][label], overload['phases'][label]
        indices = range(len(control['finals']))
        if label == 'burst':
            indices = [int(block.split('-')[1]) for block in expected_admitted]
        assert [f['ja'] for f in actual['finals']] == [control['finals'][i]['ja'] for i in indices]
        for lang in ('en', 'zh'):
            assert [f['text'] for f in actual['captions'][lang]] == [control['captions'][lang][i]['text'] for i in indices]
        if label != 'burst':
            assert actual['mt_calls'] == 2 * len(actual['finals']), 'fresh pre/post audio must invoke native MT'
    assert 1 <= len(overload['phases']['burst']['errors']) <= len(rejected)
    return {'status': 'PASS', 'scope': 'real local Pipeline/process models and ClientSender; deterministic unpaced input, no browser/network',
            'models_dir': str(config.models.resolved_dir), 'asr_queue_seconds': config.limits.asr_queue_seconds,
            'expected_admitted_blocks': expected_admitted, 'expected_dropped_blocks': expected_rejected,
            'dependencies': {n: importlib.metadata.version(n) for n in ('onnxruntime', 'llama-cpp-python', 'faster-whisper')},
            'inputs': {l: {'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for l, p in paths.items()},
            'baseline': baseline, 'overload': overload}


if __name__ == '__main__':
    if not __debug__:
        sys.exit('Run without Python optimization so all fixture checks are active.')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models-dir', type=Path)
    parser.add_argument('--json', type=Path)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': result['status'], 'dropped_segments': result['overload']['dropped_segments'],
                      'dropped_seconds': result['overload']['dropped_seconds']}, ensure_ascii=False))
