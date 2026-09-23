"""Conservative segment-level alignment; no text splitting or invented identity."""
from app.schemas.alignment import AlignedSegment
from app.schemas.diarization import DiarizationSegment
from app.schemas.transcript import TranscriptRaw


class AlignmentService:
    def align(self, transcript: TranscriptRaw, turns: list[DiarizationSegment], names: dict[str, str]) -> list[AlignedSegment]:
        result = []
        for segment in transcript.segments:
            if not segment.text.strip():
                continue
            # Multiple speakers in one STT segment remain explicitly ambiguous,
            # whether speech overlaps or speakers change within the segment.
            matching = [t for t in turns if min(segment.end, t.end) > max(segment.start, t.start)]
            speakers = sorted({t.speaker_id for t in matching})
            duration = segment.end - segment.start
            spans = sorted((max(segment.start, t.start), min(segment.end, t.end)) for t in matching)
            covered = 0.0
            previous_end = segment.start
            for start, end in spans:
                covered += max(0.0, end - max(start, previous_end))
                previous_end = max(previous_end, end)
            certain = len(speakers) == 1 and duration > 0 and covered / duration >= 0.5
            result.append(AlignedSegment(id=segment.id, text=segment.text, start=segment.start, end=segment.end,
                                         speaker_ids=speakers, speaker=names.get(speakers[0]) if certain else None,
                                         ambiguous=not certain))
        return result
