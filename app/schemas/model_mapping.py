from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class ModelMappingTarget(BaseModel):
    model_name: str = Field(..., min_length=1)
    enabled: bool = True

    @field_validator("model_name")
    @classmethod
    def normalize_model_name(cls, value: str) -> str:
        return value.strip()


class ModelMappingBase(BaseModel):
    source_model_name: str = Field(..., min_length=1)
    enabled: bool = True
    targets: list[ModelMappingTarget] = Field(default_factory=list)
    remark: str | None = None

    @field_validator("source_model_name")
    @classmethod
    def normalize_source_model_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("remark")
    @classmethod
    def normalize_remark(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_targets(self):
        if not self.targets:
            raise ValueError("至少需要配置一个目标模型")
        target_names = [item.model_name for item in self.targets]
        if len(target_names) != len(set(target_names)):
            raise ValueError("目标模型不能重复")
        if self.source_model_name in set(target_names):
            raise ValueError("目标模型不能与源模型相同")
        return self


class ModelMappingCreate(ModelMappingBase):
    pass


class ModelMappingUpdate(BaseModel):
    enabled: bool | None = None
    targets: list[ModelMappingTarget] | None = None
    remark: str | None = None

    @field_validator("remark")
    @classmethod
    def normalize_remark(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_targets(self):
        if self.targets is not None:
            if not self.targets:
                raise ValueError("至少需要配置一个目标模型")
            target_names = [item.model_name for item in self.targets]
            if len(target_names) != len(set(target_names)):
                raise ValueError("目标模型不能重复")
        return self


class ModelMappingOut(ModelMappingBase):
    id: int
    created_at: datetime
    updated_at: datetime


class ModelMappingSelectionProbe(BaseModel):
    source_model_name: str = Field(..., min_length=1)
    require_vision: bool = False
    require_stream: bool = False
    require_tools: bool = False
    require_image_generation: bool = False
    require_chat_completions: bool = False
    require_responses: bool = False

    @field_validator("source_model_name")
    @classmethod
    def normalize_source_model_name(cls, value: str) -> str:
        return value.strip()


class ModelMappingSelectionOut(BaseModel):
    mapped: bool = False
    source_model_name: str | None = None
    selected_model_name: str | None = None
    trace: dict[str, Any] | None = None
