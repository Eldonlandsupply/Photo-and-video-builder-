#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, re, shutil, subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
from kokoro import KPipeline

SR = 24000


def run(cmd, check=True):
    p = subprocess.run(cmd, text=True, capture_output=True)
    if check and p.returncode:
        raise RuntimeError(
            'Command failed:\n' + ' '.join(map(str, cmd)) +
            '\nSTDOUT:\n' + p.stdout[-3000:] +
            '\nSTDERR:\n' + p.stderr[-6000:]
        )
    return p


def ffprobe(path: Path):
    p = run([
        'ffprobe', '-v', 'error',
        '-show_entries', 'format=duration',
        '-show_entries', 'stream=codec_type,codec_name,sample_rate,channels',
        '-of', 'json', str(path)
    ])
    return json.loads(p.stdout)


def duration(path: Path) -> float:
    return float(ffprobe(path).get('format', {}).get('duration') or 0)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def synthesize(pipeline, text: str, voice: str, speed: float) -> np.ndarray:
    chunks = []
    for _gs, _ps, audio in pipeline(text, voice=voice, speed=speed):
        arr = np.asarray(audio, dtype=np.float32).reshape(-1)
        if arr.size:
            if chunks:
                chunks.append(np.zeros(int(SR * 0.055), dtype=np.float32))
            chunks.append(arr)
    if not chunks:
        raise RuntimeError(f'Kokoro returned no audio for: {text[:80]}')
    return np.concatenate(chunks)


def atempo_chain(rate: float) -> str:
    factors = []
    while rate > 2.0:
        factors.append(2.0)
        rate /= 2.0
    while rate < 0.5:
        factors.append(0.5)
        rate /= 0.5
    factors.append(rate)
    return ','.join(f'atempo={x:.8f}' for x in factors)


def fit_clip(raw: Path, target: float, out: Path):
    raw_dur = duration(raw)
    tempo = raw_dur / target
    # Preserve natural rhythm. Use silence for modest underruns and reject only extreme compression.
    if tempo < 0.95:
        tempo = 0.95
    if tempo > 1.28:
        raise RuntimeError(f'Clip {raw.name} needs excessive speed-up ({tempo:.3f}x).')
    fade_out = max(0.0, target - 0.08)
    filt = (
        f'{atempo_chain(tempo)},'
        f'apad=pad_dur={target:.3f},atrim=0:{target:.3f},'
        f'afade=t=in:st=0:d=0.045,afade=t=out:st={fade_out:.3f}:d=0.075'
    )
    run([
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-i', str(raw),
        '-af', filt, '-ar', str(SR), '-ac', '1', '-c:a', 'pcm_s24le', str(out)
    ])
    return {'raw_duration': raw_dur, 'tempo': tempo, 'target': target}


def concat_wavs(wavs: list[Path], out: Path):
    listfile = out.with_suffix('.ffconcat')
    def q(p):
        return str(p.resolve()).replace("'", "'\\''")
    listfile.write_text(
        'ffconcat version 1.0\n' + ''.join(f"file '{q(p)}'\n" for p in wavs),
        encoding='utf-8'
    )
    run([
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
        '-f', 'concat', '-safe', '0', '-i', str(listfile),
        '-c:a', 'pcm_s24le', '-ar', '48000', '-ac', '2', str(out)
    ])
    listfile.unlink(missing_ok=True)


def normalize_voice(src: Path, target: float, out: Path):
    first = run([
        'ffmpeg', '-hide_banner', '-nostats', '-i', str(src),
        '-af', 'loudnorm=I=-16:LRA=7:TP=-1.5:print_format=json',
        '-f', 'null', '-'
    ], check=False)
    matches = re.findall(r'\{\s*"input_i".*?\}', first.stderr, flags=re.S)
    if matches:
        st = json.loads(matches[-1])
        filt = (
            'highpass=f=65,lowpass=f=15000,'
            'loudnorm=I=-16:LRA=7:TP=-1.5:'
            f'measured_I={st["input_i"]}:measured_LRA={st["input_lra"]}:'
            f'measured_TP={st["input_tp"]}:measured_thresh={st["input_thresh"]}:'
            f'offset={st["target_offset"]}:linear=true:print_format=summary'
        )
    else:
        filt = 'highpass=f=65,lowpass=f=15000,loudnorm=I=-16:LRA=7:TP=-1.5'
    run([
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-i', str(src),
        '-af', f'{filt},apad=pad_dur={target:.3f},atrim=0:{target:.3f}',
        '-ar', '48000', '-ac', '2', '-c:a', 'flac', '-compression_level', '8', str(out)
    ])
    return filt


def loudness(path: Path):
    p = run([
        'ffmpeg', '-hide_banner', '-nostats', '-i', str(path),
        '-af', 'loudnorm=I=-16:LRA=7:TP=-1.5:print_format=json',
        '-f', 'null', '-'
    ], check=False)
    matches = re.findall(r'\{\s*"input_i".*?\}', p.stderr, flags=re.S)
    return json.loads(matches[-1]) if matches else None


def build_voice(pipeline, voice: str, label: str, data: dict, outdir: Path):
    work = outdir / f'work_{label}'
    work.mkdir(parents=True, exist_ok=True)
    fitted, segment_report = [], []
    base_speed = 0.98 if voice.startswith('af_') else 1.00
    for seg in data['segments']:
        raw = work / f'{seg["id"]}_raw.wav'
        fit = work / f'{seg["id"]}_fit.wav'
        audio = synthesize(pipeline, seg['tts_text'], voice, base_speed)
        sf.write(raw, audio, SR, subtype='PCM_24')
        stats = fit_clip(raw, float(seg['duration']), fit)
        stats.update({'id': seg['id'], 'text': seg['text']})
        segment_report.append(stats)
        fitted.append(fit)
    combined = work / 'combined_24bit.wav'
    narration = outdir / f'TMM001_Did_Jesus_Exist_{label}_Narration_48k.flac'
    concat_wavs(fitted, combined)
    norm_filter = normalize_voice(combined, float(data['runtime']), narration)
    preview = outdir / f'TMM001_Did_Jesus_Exist_{label}_Voice_Preview_45s.wav'
    run([
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-i', str(narration),
        '-t', '45', '-ar', '48000', '-ac', '2', '-c:a', 'pcm_s24le', str(preview)
    ])
    return {
        'voice': voice,
        'label': label,
        'narration': narration.name,
        'preview': preview.name,
        'segments': segment_report,
        'normalization_filter': norm_filter,
        'probe': ffprobe(narration),
        'loudness': loudness(narration),
        'sha256': sha256(narration)
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--segments', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    data = json.loads(args.segments.read_text(encoding='utf-8'))
    if int(data['runtime']) != 305 or len(data['segments']) != 19:
        raise RuntimeError('Unexpected locked segment map.')
    pipeline = KPipeline(lang_code='a')
    builds = []
    for voice, label in [('af_heart', 'Natural_Warm'), ('am_michael', 'Natural_Male')]:
        builds.append(build_voice(pipeline, voice, label, data, args.output))
    report = {
        'episode': 'TMM-001',
        'runtime': data['runtime'],
        'editorial_lock': 'unchanged',
        'selected_narration': 'TMM001_Did_Jesus_Exist_Natural_Warm_Narration_48k.flac',
        'builds': builds
    }
    (args.output / 'TMM001_Natural_Voice_Technical_Report.json').write_text(
        json.dumps(report, indent=2), encoding='utf-8'
    )
    readme = (
        'TMM-001 NATURAL VOICE REPLACEMENT AUDIO\n\n'
        'SELECTED\nTMM001_Did_Jesus_Exist_Natural_Warm_Narration_48k.flac\n\n'
        'ALTERNATE\nTMM001_Did_Jesus_Exist_Natural_Male_Narration_48k.flac\n\n'
        'Both preserve the locked narration and exact 5:05 section timing. '
        'No real person\'s voice was cloned.\n'
    )
    (args.output / 'README_SELECT_VOICE.txt').write_text(readme, encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
