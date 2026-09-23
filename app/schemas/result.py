from pydantic import BaseModel

from app.schemas.action_item import ActionItem
from app.schemas.alignment import AlignedSegment
from app.schemas.meeting import MeetingRead
from app.schemas.summary import MeetingSummary


class MeetingResult(BaseModel):
    meeting: MeetingRead
    summary: MeetingSummary
    action_items: list[ActionItem]
    transcript: list[AlignedSegment]
