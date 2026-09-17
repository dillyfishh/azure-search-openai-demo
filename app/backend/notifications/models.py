"""Human-authored notification contract. Never serialize these models to the browser."""

from datetime import date, datetime, time, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Schedule(StrictModel):
    """One continuous, inclusive window expressed in local dates and times."""

    startDate: date
    endDate: date
    startTime: time
    endTime: time
    timezone: str = Field(default="Australia/Sydney", min_length=1, max_length=100)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use an IANA timezone such as Australia/Sydney") from exc
        return value

    def interval(self) -> tuple[datetime, datetime]:
        zone = ZoneInfo(self.timezone)
        local_start = datetime.combine(self.startDate, self.startTime)
        local_end = datetime.combine(self.endDate, self.endTime)
        # Include both occurrences when a boundary falls in the repeated DST hour.
        start = local_start.replace(tzinfo=zone, fold=0).astimezone(timezone.utc)
        end = local_end.replace(tzinfo=zone, fold=1).astimezone(timezone.utc)
        for local, utc in ((local_start, start), (local_end, end)):
            if utc.astimezone(zone).replace(tzinfo=None) != local:
                raise ValueError("Schedule time does not exist in this timezone due to a daylight-saving change")
        return start, end

    @model_validator(mode="after")
    def check_window(self):
        if self.startTime.tzinfo or self.endTime.tzinfo:
            raise ValueError("Times must omit offsets; use the timezone field")
        if self.startDate.year < 2 or self.endDate.year >= 9999:
            raise ValueError("Schedule years must be between 2 and 9998")
        start, end = self.interval()
        if end < start:
            raise ValueError("Schedule end must be on or after its start")
        return self


class Notification(StrictModel):
    enabled: bool = True
    type: Literal["info", "success", "warning", "error"] = "info"
    title: Optional[str] = Field(default=None, min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=40000)
    priority: int = Field(default=0, ge=0, le=100)
    dismissible: bool = False
    schedule: Optional[Schedule] = None

    @field_validator("message", "title")
    @classmethod
    def not_blank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Text must not be blank")
        return value


class NotificationDocument(StrictModel):
    schemaVersion: Literal[1]
    notifications: list[Notification] = Field(max_length=1000)
