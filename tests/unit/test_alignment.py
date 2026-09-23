from app.schemas.diarization import DiarizationSegment
from app.schemas.transcript import TranscriptRaw, TranscriptSegment
from app.services.alignment import AlignmentService


def test_alignment_preserves_ids_text_and_uncertainty():
    transcript = TranscriptRaw(model="fixture", device="cpu", compute_type="int8", text="", duration_seconds=6, options={},
        segments=[TranscriptSegment(id=i, start=i * 2, end=i * 2 + 2, text=f"Реплика {i}") for i in range(3)])
    turns = [DiarizationSegment(speaker_id="SPEAKER_00", start=0, end=3),
             DiarizationSegment(speaker_id="SPEAKER_01", start=2.5, end=4)]
    result = AlignmentService().align(transcript, turns, {"SPEAKER_00": "Асхат"})
    assert result[0].speaker == "Асхат" and not result[0].ambiguous
    assert result[1].speaker is None and result[1].ambiguous
    assert result[1].speaker_ids == ["SPEAKER_00", "SPEAKER_01"]
    assert result[2].speaker is None and result[2].speaker_ids == []
    assert [(s.id, s.text) for s in result] == [(s.id, s.text) for s in transcript.segments]
