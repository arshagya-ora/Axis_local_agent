"""Small, explicit document commands issued by the planner."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkflowItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_id: int = Field(ge=1)
    instruction: str = Field(min_length=1, max_length=1600)
    expected_result: str = Field(min_length=1, max_length=600)


class DocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["outline", "read", "search", "plan", "skip", "workflow"]
    attachment_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    section_ids: list[int] = Field(default_factory=list, max_length=16)
    query: str | None = Field(default=None, max_length=500)
    sheet: str | None = Field(default=None, max_length=200)
    cell_range: str | None = Field(default=None, max_length=40)
    offset: int = Field(default=0, ge=0)
    steps: list[WorkflowItem] = Field(default_factory=list, max_length=24)
    reason: str | None = Field(default=None, max_length=600)
