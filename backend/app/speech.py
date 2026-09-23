"""Local multilingual ASR and speaker embeddings. No runtime downloads."""
import gc
import subprocess
import wave
from pathlib import Path
import numpy as np
from .config import settings
from .db import uid


def transcribe(path: Path):
    if not Path(settings.asr_model).is_dir() or not Path(settings.speaker_model).is_file():
        raise RuntimeError('speech_models_not_prepared')
    duration = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(path)], check=True, capture_output=True, text=True, timeout=30)
    if float(duration.stdout.strip()) > 10800:
        raise RuntimeError('recording_exceeds_three_hours')
    wav = path.parent / 'audio.wav'
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-protocol_whitelist', 'file,pipe',
                    '-i', str(path), '-vn', '-ac', '1', '-ar', '16000', str(wav)], check=True, timeout=300, capture_output=True)
    from faster_whisper import WhisperModel
    model = WhisperModel(settings.asr_model, device='cpu', compute_type='int8', cpu_threads=4, local_files_only=True)
    iterator, info = model.transcribe(str(wav), task='transcribe', beam_size=3, vad_filter=True, word_timestamps=True, condition_on_previous_text=False)
    segments = [{'id': uid(), 'start_ms': round(s.start*1000), 'end_ms': round(s.end*1000), 'text': s.text.strip(),
                 'speaker': 'SPEAKER_UNKNOWN', 'language': info.language, 'timing_source': 'asr'} for s in iterator if s.text.strip()]
    del model
    gc.collect()
    if not segments:
        raise RuntimeError('no_speech_detected')
    with wave.open(str(wav), 'rb') as audio:
        samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    import sherpa_onnx
    from sklearn.cluster import AgglomerativeClustering
    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=settings.speaker_model, num_threads=2, provider='cpu')
    if not config.validate():
        raise RuntimeError('speaker_model_invalid')
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
    embeddings, indices = [], []
    for i, segment in enumerate(segments):
        clip = samples[segment['start_ms']*16:segment['end_ms']*16]
        if len(clip) < 16000:
            continue
        stream = extractor.create_stream()
        stream.accept_waveform(sample_rate=16000, waveform=clip)
        stream.input_finished()
        if extractor.is_ready(stream):
            vector = np.asarray(extractor.compute(stream))
            embeddings.append(vector / max(np.linalg.norm(vector), 1e-8))
            indices.append(i)
    if embeddings:
        labels = AgglomerativeClustering(n_clusters=None, distance_threshold=0.45, metric='cosine', linkage='average').fit_predict(np.array(embeddings)) if len(embeddings) > 1 else [0]
        remap = {}
        for index, label in zip(indices, labels):
            remap.setdefault(int(label), len(remap)+1)
            segments[index]['speaker'] = f'SPEAKER_{remap[int(label)]:02d}'
    return {'segments': segments, 'language': info.language, 'asr_model': settings.asr_model,
            'diarization': 'local_wespeaker_clustering', 'speaker_review_required': True,
            'warnings': ['Проверьте короткие и перекрывающиеся реплики. Кластеры голосов не являются именами людей.']}
