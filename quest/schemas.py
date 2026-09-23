from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Level = Annotated[int, Field(ge=0, le=5, strict=True)]
Identifier = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")]
Grade = Literal["Junior", "Middle", "Senior", "Lead"]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class GoalInput(InputModel):
    target_role: str = Field(min_length=1, max_length=100)
    target_grade: Grade


class EmployeeInput(InputModel):
    employee_id: Identifier
    full_name: str = Field(min_length=1, max_length=200)
    department: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=100)
    grade: Grade
    manager_id: Identifier | None = None
    hire_date: date
    tenure_months: int = Field(ge=0)
    work_format: Literal["office", "hybrid", "remote"]
    preferred_language: Literal["ru", "kk", "en"]
    career_goal: GoalInput | None = None
    skills: dict[Identifier, Level]
    last_review_date: date


class SkillInput(InputModel):
    skill_id: Identifier
    name: str = Field(min_length=1, max_length=200)
    type: Literal["hard", "soft"]
    category: str = Field(max_length=100)
    description: str = ""


class RoleInput(InputModel):
    role: str = Field(min_length=1, max_length=100)
    grade: Grade
    required_skills: dict[Identifier, Level]
    critical_skills: list[Identifier]


class GainInput(InputModel):
    skill_id: Identifier
    gain: Level
    max_level: Level


class EventInput(InputModel):
    event_id: Identifier
    title: str = Field(min_length=1, max_length=300)
    description: str
    type: Literal["compliance", "onboarding", "course", "workshop", "mentoring", "certification", "meetup"]
    format: Literal["online", "offline", "self_paced"]
    duration_hours: float = Field(gt=0, allow_inf_nan=False)
    mandatory: bool
    target_roles: list[str]
    target_grades: list[Grade]
    develops_skills: list[GainInput]
    prerequisites: dict[Identifier, Level]
    upcoming_sessions: list[date]


class HistoryInput(InputModel):
    record_id: Identifier
    employee_id: Identifier
    event_id: Identifier
    date: date
    due_date: date | None = None
    status: Literal["completed", "in_progress", "dropped", "no_show", "declined", "overdue"]
    completion_pct: int = Field(ge=0, le=100)
    score: int | None = Field(default=None, ge=0, le=100)
    feedback_rating: int | None = Field(default=None, ge=1, le=5)
    assigned_by: Literal["self", "manager", "hr"]

    @model_validator(mode="after")
    def validate_status(self):
        if self.status == "completed" and self.completion_pct != 100:
            raise ValueError("completed требует completion_pct=100")
        if self.status in {"no_show", "declined"} and self.completion_pct != 0:
            raise ValueError("no_show / declined требует completion_pct=0")
        return self
