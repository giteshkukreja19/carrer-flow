from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class EligibilityCriterion(BaseModel):
    label: str
    value: str
    source: str = "Haveloc"


class ApplicationQuestion(BaseModel):
    id: str
    prompt: str
    kind: str = "unknown"
    options: list[str] = Field(default_factory=list)
    answer: str | None = None
    needs_review: bool = True


class Job(BaseModel):
    id: str
    external_id: str
    company: str
    role: str
    location: str | None = None
    campus_mode: str | None = None
    gender: str | None = None
    batch: str | None = None
    applicants: str | None = None
    salary_ctc: str | None = None
    stipend: str | None = None
    apply_by: str | None = None
    visit_date: str | None = None
    job_type: str | None = None
    application_status: str | None = None
    haveloc_status: str
    source_category: str
    our_decision: str = "Review"
    last_scanned: datetime | None = None
    eligibility: list[EligibilityCriterion] = Field(default_factory=list)
    questions: list[ApplicationQuestion] = Field(default_factory=list)
    hiring_process: list[str] = Field(default_factory=list)
    job_description: str | None = None
    skills: list[str] = Field(default_factory=list)
    attachments: list[str] = Field(default_factory=list)
    raw_details: dict[str, Any] = Field(default_factory=dict)


class Application(BaseModel):
    id: str
    job_id: str | None = None
    company: str
    role: str
    applied_date: str | None = None
    application_status: str
    hiring_round_status: str | None = None
    round_progression: str | None = None
    last_scanned: datetime | None = None


class Round(BaseModel):
    id: str
    job_id: str | None = None
    company: str
    round_name: str
    round_date: str | None = None
    round_time: str | None = None
    round_end_time: str | None = None
    round_status: str
    confirmation_status: str
    confirmation_deadline: str | None = None
    last_scanned: datetime | None = None


class Attendance(BaseModel):
    id: str
    round_id: str | None = None
    company: str
    round_name: str
    start_time: str | None = None
    attendance_method: str | None = None
    attendance_status: str
    action_required: str | None = None


class ActivityLog(BaseModel):
    id: str
    event_type: str
    message: str
    severity: str = "info"
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class Reminder(BaseModel):
    id: str
    kind: str
    title: str
    description: str
    due_at: str | None = None
    status: str
    severity: str
    source_id: str
    source_type: str = "placement"
    created_at: datetime | None = None


class PlacementAlert(BaseModel):
    id: str
    gmail_message_id: str
    source_message_id: str | None = None
    source_sender: str | None = None
    company: str | None = None
    role: str | None = None
    event_type: str
    round: str | None = None
    date: str | None = None
    time: str | None = None
    date_time: str | None = None
    deadline: str | None = None
    location_mode: str | None = None
    required_action: str | None = None
    application_status: str | None = None
    original_subject: str
    received_at: datetime
    created_at: datetime | None = None
    notification_status: str = "prepared"
    triage_status: Literal["new", "review", "acknowledged"] = "new"


class AlertTriageUpdate(BaseModel):
    status: Literal["review", "acknowledged"]


class GmailIntegrationStatus(BaseModel):
    oauth_configured: bool
    # Retained while the frontend migrates to oauth_configured.
    gmail_oauth_configured: bool
    gmail_connected: bool
    gemini_configured: bool
    sender_configured: bool
    polling_enabled: bool
    last_polled_at: datetime | None = None


class SchedulerSettings(BaseModel):
    scan_interval_minutes: int = 30
    notifications_enabled: bool = True
    auto_apply_enabled: bool = False
    attendance_automation_enabled: bool = False
    email_notifications_enabled: bool = False
    haveloc_notification_sender: str = ""
    gmail_polling_enabled: bool = False


class AnswerProfile(BaseModel):
    full_name: str = ""
    github_url: str = ""
    linkedin_url: str = ""
    portfolio_url: str = ""
    internship_experience: str = ""
    preferred_role: str = ""
    relocation_willingness: str = ""
    physical_attendance_willingness: str = ""


class WhatsAppNotificationStatus(BaseModel):
    configured: bool = False
    enabled: bool = False
    provider: str | None = None
    last_notification_at: datetime | None = None
    last_notification_status: Literal["pending", "sending", "sent", "failed"] | None = None


class DashboardResponse(BaseModel):
    jobs: list[Job] = Field(default_factory=list)
    applications: list[Application] = Field(default_factory=list)
    rounds: list[Round] = Field(default_factory=list)
    attendance: list[Attendance] = Field(default_factory=list)
    activity: list[ActivityLog] = Field(default_factory=list)
    reminders: list[Reminder] = Field(default_factory=list)
    alerts: list[PlacementAlert] = Field(default_factory=list)
    whatsapp_status: WhatsAppNotificationStatus = Field(default_factory=WhatsAppNotificationStatus)
    integration_status: GmailIntegrationStatus
    settings: SchedulerSettings
    answer_profile: AnswerProfile
    database_available: bool
    database_configured: bool
    scanner_configured: bool
    last_scan_at: datetime | None = None


class ScanResponse(BaseModel):
    status: str
    message: str
    jobs_found: int = 0
    applications_found: int = 0
    rounds_found: int = 0
    attention_required: bool = False
    last_scan_at: datetime | None = None
