from pydantic import BaseModel, Field


class SessionCreate(BaseModel):
    goal: str = Field(min_length=1, max_length=1000)
    concept_ids: list[str] = Field(default_factory=list, max_length=12)
    course_id: str = Field(default="mechanics", min_length=1, max_length=60)
    student_id: str = Field(
        default="local", pattern=r"^(local|evaluation_[a-f0-9]{12})$"
    )


