from docx import Document
from pypdf import PdfReader

from app.core.config import Settings
from app.schemas.action_item import ActionItem
from app.schemas.analysis import MeetingAnalysis
from app.schemas.summary import MeetingSummary
from app.services.export import LocalExportService


def test_export_summary_preserves_condition_deadline_and_milestones(tmp_path):
    item = ActionItem(description="Расторгнуть договор", responsible="Ерлан", deadline_raw=None,
        decision_type="conditional", condition="Если подрядчик продолжит нарушать", source_segment_ids=[0],
        confidence=0.9, evidence=[dict(segment_id=0, quote="Если подрядчик продолжит нарушать, Ерлан расторгнет договор; проект за неделю.")],
        milestones=[dict(description="Подготовить проект", deadline_raw="за неделю", source_segment_ids=[0])])
    summary = MeetingSummary(topics=[], key_discussions=[], decisions=[], problems_and_risks=[], main_action_items=[item])
    analysis = MeetingAnalysis(action_items=[item], summary=summary, source_segments=[], mode="single",
                               generation_requests=1, completed_generations=1, chunks=0)
    files = LocalExportService(Settings(_env_file=None, data_dir=tmp_path)).export("test", "Кеңес", analysis, [])
    docx_text = "\n".join(p.text for p in Document(files["docx"]).paragraphs)
    pdf_text = "\n".join(p.extract_text() for p in PdfReader(files["pdf"]).pages)
    for text in (docx_text, pdf_text):
        brief = " ".join(text.split("Основные поручения", 1)[1].split("КЛЮЧЕВЫЕ РЕШЕНИЯ", 1)[0].split())
        assert "Условное поручение" in brief
        assert "Если подрядчик продолжит нарушать" in brief
        assert "Ерлан" in brief and "срок: не указан" in brief
        assert "Подготовить проект; срок: за неделю" in brief
        assert "[сегменты: 0]" in brief
