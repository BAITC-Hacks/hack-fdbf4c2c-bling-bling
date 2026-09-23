from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Action(Strict):
    title: str = Field(min_length=1, max_length=500)
    assignee_mention: str | None = Field(default=None, max_length=200)
    due_raw: str | None = Field(default=None, max_length=200)
    source_segment_ids: list[str] = Field(min_length=1, max_length=30)


class Extraction(Strict):
    actions: list[Action] = Field(max_length=50)


class Group(Action):
    candidate_ids: list[str] = Field(min_length=1, max_length=30)


class Reconciliation(Strict):
    groups: list[Group] = Field(max_length=50)


class Fact(Strict):
    text: str = Field(min_length=1, max_length=900)
    kind: Literal['fact', 'decision', 'risk', 'question'] = 'fact'
    source_segment_ids: list[str] = Field(min_length=1, max_length=30)


class Summary(Strict):
    items: list[Fact] = Field(max_length=20)


class Review(Strict):
    warnings: list[str] = Field(max_length=30)


class QA(Strict):
    status: Literal['answered', 'insufficient_evidence']
    answer: str = Field(max_length=4000)
    source_segment_ids: list[str] = Field(max_length=30)


class ToolArgs(Strict):
    query: str | None = Field(default=None, max_length=1000)
    limit: int = Field(default=6, ge=1, le=6)
    mentioned_name: str | None = Field(default=None, max_length=200)
    due_raw: str | None = Field(default=None, max_length=200)
    segment_ids: list[str] = Field(default_factory=list, max_length=20)
    source_segment_ids: list[str] = Field(default_factory=list, max_length=20)
    context_segment_ids: list[str] = Field(default_factory=list, max_length=20)
    candidate_ids: list[str] = Field(default_factory=list, max_length=20)
    status_filter: Literal['all', 'open', 'in_progress', 'done'] = 'all'
    assignee_name: str | None = Field(default=None, max_length=200)


SCHEMAS = {'extract': Extraction, 'reconcile': Reconciliation, 'summary': Summary, 'verify': Review, 'qa': QA}
ROLE_TOOLS = {
    'extract': ['get_evidence', 'resolve_participant', 'normalize_deadline', 'search_reference'],
    'reconcile': ['get_evidence', 'normalize_deadline'],
    'summary': ['get_evidence', 'search_reference'],
    'verify': ['get_evidence', 'get_action_candidates'],
    'qa': ['search_meetings', 'get_evidence', 'get_confirmed_actions', 'search_reference'],
}
