from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ModelMappingTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_name: str = Field(..., min_length=1)
    enabled: bool = True

    @field_validator("model_name")
    @classmethod
    def normalize_model_name(cls, value: str) -> str:
        return value.strip()


class ModelMappingBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    model_config = ConfigDict(extra="forbid")

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

    @field_validator("source_model_name")
    @classmethod
    def normalize_source_model_name(cls, value: str) -> str:
        return value.strip()


class ModelMappingSelectionOut(BaseModel):
    mapped: bool = False
    source_model_name: str | None = None
    selected_model_name: str | None = None
    trace: dict[str, Any] | None = None
