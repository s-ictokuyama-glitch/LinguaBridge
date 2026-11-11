from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import sys
import traceback
import wave

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.config import load_config
from server.main import build_asr_engine, build_mt_engine
from server.pipeline import Pipeline
from server.roster import Roster


class Socket:
    def __init__(self):
        self.frames = []
        self.closed = None

    async def send_json(self, frame):
        self.frames.append(frame)

    async def close(self, code=1000):
        self.closed = code


def model_inventory(config):
    asr_paths = {
        'haas-onnx': config.asr.haas_onnx.model,
        'haas-sherpa': config.asr.haas_sherpa.model,
        'sherpa': config.asr.sherpa.model,
        'faster-whisper': config.asr.model,
    }
    mt_paths = {
        'hy-mt2': [config.mt.hy_mt2.gguf_path],
        'nllb': [config.mt.nllb.model_dir, config.mt.nllb.tokenizer_dir],
    }
    selected = [asr_paths[config.asr.engine], *mt_paths[config.mt.engine]]
    inventory = []
    for relative in selected:
        target = config.models.resolve(relative)
        files = sorted(p for p in target.rglob('*') if p.is_file()) if target.is_dir() else [target]
        for path in files:
            stat = path.stat()
            inventory.append({
                'path': str(path.relative_to(config.models.resolved_dir)),
                'bytes': stat.st_size,
                'mtime_ns': stat.st_mtime_ns,
            })
    return inventory


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


async def run(args):
    config = load_config(ROOT / 'config.yaml')
    configured_models = str(config.models.resolved_dir)
    if args.models_dir is not None:
        config.models.dir = str(args.models_dir.resolve())
    if config.asr.engine == 'fake' or config.mt.engine == 'fake':
        raise ValueError('real-model witness requires real ASR and MT engines')
    if not {'en', 'zh'}.issubset(config.language_codes):
        raise ValueError('real-model witness requires en and zh in config.languages')
    config.mt.cache_size = 0
    wav_path = args.wav if args.wav.is_absolute() else ROOT / args.wav
    with wave.open(str(wav_path)) as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 16000):
            raise ValueError('wav must be mono, PCM16, and 16000 Hz')
        frames = source.getnframes()
        pcm = source.readframes(frames)
        if not pcm or len(pcm) != frames * 2:
            raise ValueError('wav must contain complete, nonempty PCM frames')
    report = {
        'status': 'RUNNING',
        'python': platform.python_version(),
        'dependencies': {name: importlib.metadata.version(name) for name in (
            'numpy', 'onnxruntime', 'llama-cpp-python', 'faster-whisper',
        )},
        'input': str(wav_path.resolve()),
        'input_sha256': hashlib.sha256(wav_path.read_bytes()).hexdigest(),
        'input_seconds': len(pcm) / 32000,
        'configured_model_root': configured_models,
        'model_root': str(config.models.resolved_dir),
        'model_files': model_inventory(config),
        'model_files_scope': 'inventory of selected model files or directories; includes unused variants in those directories',
        'config': config.model_dump(mode='json'),
        'iterations': [],
        'scope': 'same Pipeline, real ASR/MT/VAD/Turn, captured ClientSender socket messages; no browser or real network',
    }
    def save():
        save_json(args.json, report)

    save()
    roster = Roster(config.limits)
    pipeline = None
    try:
        print('building real engines', flush=True)
        pipeline = Pipeline(roster, config, build_asr_engine(config), build_mt_engine(config))
        teacher = Socket()
        students = {lang: Socket() for lang in ('en', 'zh')}
        await roster.add(teacher, 'teacher')
        for lang, socket in students.items():
            await roster.add(socket, 'student', lang)
        await pipeline.start()
        await asyncio.wait_for(pipeline._warmup_task, timeout=180)
        assert pipeline.ready, 'warmup did not become ready'
        await pipeline.set_accepting_audio(True)
        print('warmup complete', flush=True)
        for iteration in range(1, args.repeats + 1):
            teacher_start = len(teacher.frames)
            starts = {lang: len(socket.frames) for lang, socket in students.items()}
            for offset in range(0, len(pcm), 3200):
                await pipeline.feed_audio(pcm[offset:offset + 3200])
                await asyncio.sleep(0)
            for _ in range(12):
                await pipeline.feed_audio(bytes(3200))
                await asyncio.sleep(0)
            await pipeline.flush_audio()
            await pipeline.drain(timeout=120)
            await asyncio.wait_for(asyncio.gather(*(
                sender._queue.join() for sender in roster._senders.values()
            )), timeout=10)
            finals = [f for f in teacher.frames[teacher_start:] if f['type'] == 'asr_final']
            captions = {lang: [f for f in socket.frames[starts[lang]:] if f['type'] == 'caption']
                        for lang, socket in students.items()}
            actual = {
                'iteration': iteration, 'asr_final': finals, 'captions': captions,
                'asr_calls': pipeline.asr_calls, 'mt_calls': pipeline.captions.metrics().calls,
                'asr_dropped_segments': pipeline.asr_dropped_segments,
                'queue_depth_after_drain': pipeline.queue_depth,
                'closed': {lang: socket.closed for lang, socket in students.items()},
                'teacher_errors': [f for f in teacher.frames[teacher_start:] if f['type'] == 'error'],
            }
            report['iterations'].append(actual)
            save()
            assert finals, f'iteration {iteration}: no final ASR'
            expected = [(f['seq'], f['ja']) for f in finals]
            for lang, frames in captions.items():
                assert [(f['seq'], f['ja']) for f in frames] == expected, (iteration, lang, expected, frames)
                assert all(f['lang'] == lang and f['text'].strip() for f in frames)
                assert students[lang].closed is None
            assert pipeline.asr_dropped_segments == 0
            assert pipeline.queue_depth == 0
            assert not actual['teacher_errors'], actual['teacher_errors']
            assert teacher.closed is None
            print(f'iteration {iteration}: finals={len(finals)}, en={len(captions["en"])}, zh={len(captions["zh"])}, asr={pipeline.asr_calls}, mt={pipeline.captions.metrics().calls}', flush=True)
        all_finals = [f for f in teacher.frames if f['type'] == 'asr_final']
        assert [f['seq'] for f in all_finals] == list(range(1, len(all_finals) + 1))
        assert pipeline.captions.metrics().calls == 2 * len(all_finals)
        report['status'] = 'PASS'
        report['totals'] = {
            'iterations': args.repeats, 'asr_calls': pipeline.asr_calls,
            'mt_calls': pipeline.captions.metrics().calls,
            'asr_finals': len(all_finals),
            'captions': {lang: len([f for f in s.frames if f['type'] == 'caption']) for lang, s in students.items()},
        }
        if args.screens is not None:
            save_json(args.screens, [{
                'name': f'real-model-{args.repeats}',
                'language': lang,
                'frames': socket.frames,
                'expected': [{key: frame[key] for key in ('seq', 'ja', 'text', 'lang')}
                             for frame in socket.frames if frame['type'] == 'caption'],
            } for lang, socket in students.items()])
        save()
        print(json.dumps(report['totals']), flush=True)
    except BaseException:
        report['status'] = 'FAIL'
        report['error'] = traceback.format_exc()
        save()
        raise
    finally:
        if pipeline is not None:
            await pipeline.stop()
        else:
            await roster.aclose()


def main():
    parser = argparse.ArgumentParser(description='Repeat real PCM through the configured ASR, MT, and caption delivery.')
    parser.add_argument('--models-dir', type=Path, help='Override config.models.dir')
    parser.add_argument('--wav', type=Path, default=Path('tests/fixtures/ja/03.wav'))
    parser.add_argument('--repeats', type=int, default=32)
    parser.add_argument('--json', type=Path, required=True)
    parser.add_argument('--screens', type=Path)
    args = parser.parse_args()
    if args.repeats < 17:
        parser.error('--repeats must be at least 17')
    if args.screens is not None and args.json.resolve() == args.screens.resolve():
        parser.error('--json and --screens must be different paths')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
