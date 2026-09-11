"""Configuration settings for the payment network emulator."""

import math
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class EmulatorSettings(BaseSettings):
    """Runtime settings for the payment network emulator service."""

    model_config = SettingsConfigDict(
        env_prefix="EMULATOR_",
        case_sensitive=False,
    )

    timeout_delay_seconds: float = Field(default=0.01, ge=0.0, le=30.0)

    @field_validator("timeout_delay_seconds", mode="before")
    @classmethod
    def validate_finite_delay(cls, v: Any) -> Any:
        if isinstance(v, (int, float)):
            if math.isnan(v) or math.isinf(v):
                raise ValueError("timeout_delay_seconds must be a finite number")
        return v
