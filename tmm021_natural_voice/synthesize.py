#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
from kokoro import KPipeline

SYNTH_SR = 24000
DELIVERY_SR = 48000
VOICE = "af_heart"


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if check and proc.returncode:
        raise RuntimeError(
            "Command failed:\n" + " ".join(map(str, cmd))
            + "\nSTDOUT:\n" + proc.stdout[-3000:]
            + "\nSTDERR:\n" + proc.stderr[-6000:]
        )
    return proc


def probe(path: Path) -> dict:
    proc = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-show_entries", "stream=codec_type,codec_name,sample_rate,channels",
        "-of", "json", str(path),
    ])
    return json.loads(proc.stdout)


def duration(path: Path) -> float:
    return float(probe(path).get("format", {}).get("duration") or 0.0)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def synthesize(pipeline: KPipeline, text: str, speed: float) -> np.ndarray:
    chunks: list[np.ndarray] = []
    for _graphemes, _phonemes, audio in pipeline(text, voice=VOICE, speed=speed):
        wave = np.asarray(audio, dtype=np.float32).reshape(-1)
        if wave.size:
            if chunks:
                chunks.append(np.zeros(int(SYNTH_SR * 0.055), dtype=np.float32))
            chunks.append(wave)
    if not chunks:
        raise RuntimeError(f"No speech returned for: {text[:100]}")
    return np.concatenate(chunks)


def speak_to_window(pipeline: KPipeline, segment: dict, raw_path: Path, base_speed: float) -> dict:
    target = float(segment["duration"])
    speed = base_speed
    attempts: list[dict] = []
    for attempt in range(1, 5):
        wave = synthesize(pipeline, segment["tts_text"], speed)
        sf.write(raw_path, wave, SYNTH_SR, subtype="PCM_24")
        raw_duration = duration(raw_path)
        ratio = raw_duration / target
        attempts.append({
            "attempt": attempt,
            "generation_speed": round(speed, 5),
            "raw_duration": round(raw_duration, 5),
            "raw_to_window_ratio": round(ratio, 5),
        })
        if 0.91 <= ratio <= 1.09:
            break
        desired_ratio = 1.018 if ratio > 1.09 else 0.970
        speed = max(0.80, min(1.45, speed * ratio / desired_ratio))
    return {"attempts": attempts, "final_generation_speed": speed, "raw_duration": duration(raw_path)}


def atempo_chain(rate: float) -> str:
    factors: list[float] = []
    while rate > 2.0:
        factors.append(2.0)
        rate /= 2.0
    while rate < 0.5:
        factors.append(0.5)
        rate /= 0.5
    factors.append(rate)
    return ",".join(f"atempo={factor:.8f}" for factor in factors)


def fit_segment(raw_path: Path, target: float, fitted_path: Path) -> dict:
    raw_duration = duration(raw_path)
    tempo = raw_duration / target
    if tempo < 0.94:
        tempo = 0.94
    if tempo > 1.16:
        raise RuntimeError(f"{raw_path.name} still needs excessive post-render correction ({tempo:.3f}x).")
    fade_out_start = max(0.0, target - 0.075)
    audio_filter = (
        f"{atempo_chain(tempo)},"
        f"apad=pad_dur={target:.3f},atrim=0:{target:.3f},"
        f"afade=t=in:st=0:d=0.035,"
        f"afade=t=out:st={fade_out_start:.3f}:d=0.065"
    )
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw_path),
        "-af", audio_filter, "-ar", str(SYNTH_SR), "-ac", "1", "-c:a", "pcm_s24le", str(fitted_path),
    ])
    return {"raw_duration": raw_duration, "post_render_tempo": tempo, "target_duration": target, "fitted_duration": duration(fitted_path)}


def concatenate(wavs: list[Path], output: Path) -> None:
    concat_file = output.with_suffix(".ffconcat")
    def quote(path: Path) -> str:
        return str(path.resolve()).replace("'", "'\\''")
    concat_file.write_text("ffconcat version 1.0\n" + "".join(f"file '{quote(path)}'\n" for path in wavs), encoding="utf-8")
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-ar", str(DELIVERY_SR), "-ac", "2", "-c:a", "pcm_s24le", str(output),
    ])
    concat_file.unlink(missing_ok=True)


def normalize(source: Path, target: float, output: Path) -> str:
    first = run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(source),
        "-af", "loudnorm=I=-16:LRA=7:TP=-1.5:print_format=json", "-f", "null", "-",
    ], check=False)
    matches = re.findall(r'\{\s*"input_i".*?\}', first.stderr, flags=re.S)
    if matches:
        stats = json.loads(matches[-1])
        loudnorm = (
            "loudnorm=I=-16:LRA=7:TP=-1.5:"
            f"measured_I={stats['input_i']}:"
            f"measured_LRA={stats['input_lra']}:"
            f"measured_TP={stats['input_tp']}:"
            f"measured_thresh={stats['input_thresh']}:"
            f"offset={stats['target_offset']}:linear=true:print_format=summary"
        )
    else:
        loudnorm = "loudnorm=I=-16:LRA=7:TP=-1.5"
    audio_filter = "highpass=f=65,lowpass=f=15000," + loudnorm + f",apad=pad_dur={target:.3f},atrim=0:{target:.3f}"
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-af", audio_filter, "-ar", str(DELIVERY_SR), "-ac", "2",
        "-c:a", "flac", "-compression_level", "8", str(output),
    ])
    return audio_filter


def analyze_loudness(path: Path) -> dict | None:
    proc = run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
        "-af", "loudnorm=I=-16:LRA=7:TP=-1.5:print_format=json", "-f", "null", "-",
    ], check=False)
    matches = re.findall(r'\{\s*"input_i".*?\}', proc.stderr, flags=re.S)
    return json.loads(matches[-1]) if matches else None


def build_master(pipeline: KPipeline, segment_file: Path, label: str, output_dir: Path) -> dict:
    data = json.loads(segment_file.read_text(encoding="utf-8"))
    target = float(data["runtime"])
    segments = data["segments"]
    work_dir = output_dir / f"work_{label}"
    work_dir.mkdir(parents=True, exist_ok=True)
    fitted_paths: list[Path] = []
    segment_report: list[dict] = []
    for segment in segments:
        raw_path = work_dir / f"{segment['id']}_raw.wav"
        fitted_path = work_dir / f"{segment['id']}_fit.wav"
        speech = speak_to_window(pipeline, segment, raw_path, 0.98)
        fitting = fit_segment(raw_path, float(segment["duration"]), fitted_path)
        segment_report.append({
            "id": segment["id"], "start": segment["start"], "end": segment["end"],
            "text": segment["text"], **speech, **fitting,
        })
        fitted_paths.append(fitted_path)
    combined = work_dir / f"TMM021_{label}_combined.wav"
    delivery = output_dir / f"TMM021_Who_Was_Judas_{label}_Natural_Narration_48k.flac"
    preview = output_dir / f"TMM021_Who_Was_Judas_{label}_Voice_Preview_45s.wav"
    concatenate(fitted_paths, combined)
    norm_filter = normalize(combined, target, delivery)
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(delivery),
        "-t", "45", "-ar", str(DELIVERY_SR), "-ac", "2", "-c:a", "pcm_s24le", str(preview),
    ])
    final_duration = duration(delivery)
    if abs(final_duration - target) > 0.05:
        raise RuntimeError(f"{label} narration is {final_duration:.3f}s, expected {target:.3f}s.")
    return {
        "label": label, "voice": VOICE, "runtime_seconds": target,
        "narration": delivery.name, "preview": preview.name,
        "segment_count": len(segments), "segments": segment_report,
        "normalization_filter": norm_filter, "probe": probe(delivery),
        "loudness": analyze_loudness(delivery), "sha256": sha256(delivery),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--standard", type=Path, required=True)
    parser.add_argument("--vertical", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    pipeline = KPipeline(lang_code="a")
    builds = [
        build_master(pipeline, args.standard, "Standard_05m05s", args.output),
        build_master(pipeline, args.vertical, "Vertical_02m15s", args.output),
    ]
    report = {
        "episode_id": "TMM-021",
        "title": "Who Was Judas Iscariot? What the Sources Actually Say",
        "voice": VOICE, "editorial_lock": "v1.0 unchanged", "builds": builds,
    }
    (args.output / "TMM021_Natural_Voice_Technical_Report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (args.output / "README_NATURAL_VOICE.txt").write_text(
        "TMM-021 NATURAL NARRATION\n\n"
        "The standard and vertical narration use the same approved af_heart channel voice as TMM-001.\n"
        "No real person was cloned. The locked narration, evidence levels, antisemitism guardrails, and novel firewall are unchanged.\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
