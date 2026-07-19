"""
Agent settings and credential loading.

Behavioral parameters come from agent_config.json (schema locked in
docs/config.md). Credentials come only from the global .env.json, loaded via
agent_shared.infra.load_env_config and navigated by the exact nested paths
documented in docs/config.md (trello.api_key / trello.token,
notion.integration_token, gmail_sender / gmail_password, ollama_endpoint, and
anthropic_api_keys.work-todo-trello-grooming-agent — never a flat field).

All validation happens here at load time; the run fails fast with a message
naming the offending field. No thresholds, list names, label names, or caps are
hardcoded elsewhere in the codebase — everything lives in agent_config.json.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from agent_shared.infra import load_env_config

logger = logging.getLogger(__name__)

AGENT_NAME = "work-todo-trello-grooming-agent"
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


_MODE_TRUE = {"automatic", "auto", "true", "yes", "1", "on"}
_MODE_FALSE = {"proposed", "propose", "false", "no", "0", "off"}


def _mode_to_bool(v, default=None):
    """Coerce a mode value ('automatic'/'proposed' or bool) to a bool.

    True == automatic (auto-execute), False == proposed (flag only). Returns
    `default` when the value is unrecognized (None signals invalid to callers).
    """
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in _MODE_TRUE:
        return True
    if s in _MODE_FALSE:
        return False
    return default


class SettingsError(ValueError):
    """Raised when agent_config.json is missing or has an invalid field."""


class CredentialsError(ValueError):
    """Raised when a required credential is missing from .env.json."""


@dataclass
class AgentSettings:
    """Typed view of agent_config.json (see docs/config.md)."""

    board_shortlink: str
    edit_scope_lists: list[str]
    comparison_extra_lists: list[str]
    recovery_include_pattern: str
    recovery_exclude_pattern: str
    archive_list_name: str
    report_list: str
    label_auto_updated: str
    label_proposed: str
    recovery_batch_size: int
    recovery_today_max: int
    archive_list_days: int
    proposal_timeout_days: int
    no_touch_hours: int
    dead_due_days: int
    optimistic_label_days: int
    max_merges_per_run: int
    max_renames_per_run: int
    max_recoveries_per_run: int
    max_inscope_archives_per_run: int
    max_proposals_open: int
    # Automatic-mode toggles. Internally booleans (True == "automatic",
    # False == "proposed"). The former tier1_* names remain silent aliases via
    # the properties below. Config/Notion accept "automatic"/"proposed" or bools.
    time_label_fix_mode: bool          # was tier1_stale_label_removal
    archive_mode: bool                 # was tier1_recovery_archive
    due_date_fix_mode: bool            # was tier1_due_date_clear
    automatic_action_confidence: int   # was auto_min_confidence
    # Reprioritization pass (design §5.4 / spine Problem 5).
    reprioritization_mode: bool        # True == "automatic"
    time_reprioritization_confidence: int
    today_list_target: int
    next_few_days_target: int
    max_reprioritization_moves_per_run: int
    demotion_exempt_hours: int
    priority_labels: dict              # {label_name: role} — P0/P1 promotion signal
    reprioritization_due_days: dict    # {role: days} — due-in-window bands
    wide_block_jaccard: float
    narrow_hint_jaccard: float
    name_min_length: int
    name_max_length: int
    model: str
    ollama_model: str
    weekly_sweep_day: str
    spine_review_day: str
    dry_run: bool
    auto_pause_after_failures: int
    spine_page_id: str
    entity_keywords_seed: list[str]
    tz_standard_offset: int
    tz_daylight_offset: int
    log_level: str

    # Operational (not in config schema; sensible defaults, overridable in JSON)
    db_path: str = "state.db"
    report_file: str = "logs/grooming_report.txt"

    @property
    def comparison_scope_lists(self) -> list[str]:
        """Edit-scope lists plus the extra comparison lists (dedup detection)."""
        return list(self.edit_scope_lists) + list(self.comparison_extra_lists)

    # -- Silent aliases for the pre-rename config key names ------------------
    # Old names still resolve (read and write) so existing callers, tests, and
    # Notion Rules lines keep working. Modes proxy the boolean fields; the
    # confidence alias proxies the renamed int.
    @property
    def tier1_stale_label_removal(self) -> bool:
        return self.time_label_fix_mode

    @tier1_stale_label_removal.setter
    def tier1_stale_label_removal(self, v) -> None:
        self.time_label_fix_mode = _mode_to_bool(v, default=True)

    @property
    def tier1_recovery_archive(self) -> bool:
        return self.archive_mode

    @tier1_recovery_archive.setter
    def tier1_recovery_archive(self, v) -> None:
        self.archive_mode = _mode_to_bool(v, default=True)

    @property
    def tier1_due_date_clear(self) -> bool:
        return self.due_date_fix_mode

    @tier1_due_date_clear.setter
    def tier1_due_date_clear(self, v) -> None:
        self.due_date_fix_mode = _mode_to_bool(v, default=True)

    @property
    def auto_min_confidence(self) -> int:
        return self.automatic_action_confidence

    @auto_min_confidence.setter
    def auto_min_confidence(self, v) -> None:
        self.automatic_action_confidence = int(v)

    @staticmethod
    def mode_word(flag: bool) -> str:
        """Render a mode boolean as its config word (for the report)."""
        return "automatic" if flag else "proposed"


@dataclass
class Credentials:
    """Secrets read from the global .env.json (never logged)."""

    trello_api_key: str
    trello_token: str
    notion_token: str
    gmail_sender: str
    gmail_password: str
    ollama_endpoint: str
    anthropic_api_key: str
    trello_board_id: str = ""
    gmail_recipient: str = ""


# ---------------------------------------------------------------------------
# agent_config.json
# ---------------------------------------------------------------------------

def load_settings(agent_config_path: str) -> AgentSettings:
    """Load and validate agent_config.json into an AgentSettings.

    Args:
        agent_config_path: Path to agent_config.json.

    Returns:
        Validated AgentSettings.

    Raises:
        SettingsError: If the file is missing, invalid JSON, or any field is
            missing, the wrong type, or out of range (message names the field).
    """
    path = Path(agent_config_path)
    if not path.exists():
        raise SettingsError(f"agent_config.json not found at {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SettingsError(f"agent_config.json is not valid JSON: {exc}") from exc

    def req(key: str):
        if key not in data:
            raise SettingsError(f"Missing required field '{key}' in agent_config.json")
        return data[key]

    labels = req("labels")
    if not isinstance(labels, dict) or "auto_updated" not in labels or "proposed" not in labels:
        raise SettingsError("Field 'labels' must contain 'auto_updated' and 'proposed'")

    offsets = req("local_tz_offsets")
    if not isinstance(offsets, dict) or "standard" not in offsets or "daylight" not in offsets:
        raise SettingsError("Field 'local_tz_offsets' must contain 'standard' and 'daylight'")

    def mode(new_key: str, old_key: str | None, default: str = "automatic") -> bool:
        """Read an automatic/proposed mode from the new key, then the old alias."""
        if new_key in data:
            raw = data[new_key]
        elif old_key is not None and old_key in data:
            raw = data[old_key]
        else:
            raw = default
        b = _mode_to_bool(raw)
        if b is None:
            raise SettingsError(f"Field '{new_key}' must be 'automatic'/'proposed' or a boolean")
        return b

    def int_alias(new_key: str, old_key: str, default: int) -> int:
        raw = data.get(new_key, data.get(old_key, default))
        return int(raw)

    try:
        settings = AgentSettings(
            board_shortlink=str(req("board_shortlink")),
            edit_scope_lists=list(req("edit_scope_lists")),
            comparison_extra_lists=list(req("comparison_extra_lists")),
            recovery_include_pattern=str(req("recovery_include_pattern")),
            recovery_exclude_pattern=str(req("recovery_exclude_pattern")),
            archive_list_name=str(req("archive_list_name")),
            report_list=str(req("report_list")),
            label_auto_updated=str(labels["auto_updated"]),
            label_proposed=str(labels["proposed"]),
            recovery_batch_size=int(req("recovery_batch_size")),
            recovery_today_max=int(req("recovery_today_max")),
            archive_list_days=int(req("archive_list_days")),
            proposal_timeout_days=int(req("proposal_timeout_days")),
            no_touch_hours=int(req("no_touch_hours")),
            dead_due_days=int(req("dead_due_days")),
            optimistic_label_days=int(req("optimistic_label_days")),
            max_merges_per_run=int(req("max_merges_per_run")),
            max_renames_per_run=int(req("max_renames_per_run")),
            max_recoveries_per_run=int(req("max_recoveries_per_run")),
            max_inscope_archives_per_run=int(req("max_inscope_archives_per_run")),
            max_proposals_open=int(req("max_proposals_open")),
            time_label_fix_mode=mode("time_label_fix_mode", "tier1_stale_label_removal"),
            archive_mode=mode("archive_mode", "tier1_recovery_archive"),
            due_date_fix_mode=mode("due_date_fix_mode", "tier1_due_date_clear"),
            automatic_action_confidence=int_alias(
                "automatic_action_confidence", "auto_min_confidence", 70),
            reprioritization_mode=mode("reprioritization_mode", None, "automatic"),
            time_reprioritization_confidence=int(data.get("time_reprioritization_confidence", 75)),
            today_list_target=int(data.get("today_list_target", 15)),
            next_few_days_target=int(data.get("next_few_days_target", 20)),
            max_reprioritization_moves_per_run=int(data.get("max_reprioritization_moves_per_run", 10)),
            demotion_exempt_hours=int(data.get("demotion_exempt_hours", 48)),
            priority_labels=dict(data.get("priority_labels",
                                          {"P0. High": "today", "P1": "next_few_days"})),
            reprioritization_due_days=dict(data.get("reprioritization_due_days",
                                                    {"today": 1, "next_few_days": 3, "this_week": 7})),
            wide_block_jaccard=float(req("wide_block_jaccard")),
            narrow_hint_jaccard=float(req("narrow_hint_jaccard")),
            name_min_length=int(req("name_min_length")),
            name_max_length=int(req("name_max_length")),
            model=str(req("model")),
            ollama_model=str(req("ollama_model")),
            weekly_sweep_day=str(req("weekly_sweep_day")).lower(),
            spine_review_day=str(req("spine_review_day")).lower(),
            dry_run=bool(req("dry_run")),
            auto_pause_after_failures=int(req("auto_pause_after_failures")),
            spine_page_id=str(req("spine_page_id")),
            entity_keywords_seed=[str(k).lower() for k in req("entity_keywords_seed")],
            tz_standard_offset=int(offsets["standard"]),
            tz_daylight_offset=int(offsets["daylight"]),
            log_level=str(data.get("log_level", "INFO")).upper(),
            db_path=str(data.get("db_path", "state.db")),
            report_file=str(data.get("report_file", "logs/grooming_report.txt")),
        )
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"Invalid field type in agent_config.json: {exc}") from exc

    _validate_settings(settings)
    logger.info("Loaded agent settings (dry_run=%s)", settings.dry_run)
    return settings


def _validate_settings(s: AgentSettings) -> None:
    """Cross-field and range validation per docs/config.md."""
    if not s.edit_scope_lists:
        raise SettingsError("edit_scope_lists must be non-empty")
    if not s.archive_list_name:
        raise SettingsError("archive_list_name must be non-empty")
    # NOTE: recovery_today_max is intentionally NOT bounded by recovery_batch_size.
    # Per docs/design.md it may exceed the batch (harmless — a small batch simply
    # never reaches the Today cap in one run).
    if not (s.name_min_length < s.name_max_length):
        raise SettingsError("name_min_length must be < name_max_length")
    if not (0 <= s.automatic_action_confidence <= 100):
        raise SettingsError("automatic_action_confidence must satisfy 0 <= x <= 100")
    if not (0 <= s.time_reprioritization_confidence <= 100):
        raise SettingsError("time_reprioritization_confidence must satisfy 0 <= x <= 100")
    for field_name, val in (
        ("today_list_target", s.today_list_target),
        ("next_few_days_target", s.next_few_days_target),
        ("max_reprioritization_moves_per_run", s.max_reprioritization_moves_per_run),
        ("demotion_exempt_hours", s.demotion_exempt_hours),
    ):
        if val < 0:
            raise SettingsError(f"{field_name} must be >= 0")
    for field_name, val in (
        ("narrow_hint_jaccard", s.narrow_hint_jaccard),
        ("wide_block_jaccard", s.wide_block_jaccard),
    ):
        if not (0.0 < val <= 1.0):
            raise SettingsError(f"{field_name} must satisfy 0.0 < x <= 1.0")
    for field_name, val in (
        ("recovery_batch_size", s.recovery_batch_size),
        ("archive_list_days", s.archive_list_days),
        ("proposal_timeout_days", s.proposal_timeout_days),
        ("dead_due_days", s.dead_due_days),
        ("optimistic_label_days", s.optimistic_label_days),
        ("auto_pause_after_failures", s.auto_pause_after_failures),
    ):
        if val <= 0:
            raise SettingsError(f"{field_name} must be > 0")
    # no_touch_hours may be 0 (disables the no-touch window; the rejection ledger
    # and open-proposal lock still protect their cards).
    for field_name, val in (
        ("no_touch_hours", s.no_touch_hours),
        ("recovery_today_max", s.recovery_today_max),
        ("max_merges_per_run", s.max_merges_per_run),
        ("max_renames_per_run", s.max_renames_per_run),
        ("max_recoveries_per_run", s.max_recoveries_per_run),
        ("max_inscope_archives_per_run", s.max_inscope_archives_per_run),
        ("max_proposals_open", s.max_proposals_open),
    ):
        if val < 0:
            raise SettingsError(f"{field_name} must be >= 0")
    if s.weekly_sweep_day not in _WEEKDAYS:
        raise SettingsError(f"weekly_sweep_day must be one of {_WEEKDAYS}")
    if s.spine_review_day not in _WEEKDAYS and s.spine_review_day != "off":
        raise SettingsError(f"spine_review_day must be one of {_WEEKDAYS} or 'off'")
    for field_name, pattern in (
        ("recovery_include_pattern", s.recovery_include_pattern),
        ("recovery_exclude_pattern", s.recovery_exclude_pattern),
    ):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise SettingsError(f"{field_name} is not a valid regex: {exc}") from exc
    for field_name, val in (
        ("tz_standard_offset", s.tz_standard_offset),
        ("tz_daylight_offset", s.tz_daylight_offset),
    ):
        if not (-24 < val < 24):
            raise SettingsError(f"local_tz_offsets.{field_name} must satisfy -24 < x < 24")
    if s.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise SettingsError("log_level must be one of DEBUG, INFO, WARNING, ERROR")


# ---------------------------------------------------------------------------
# .env.json credentials
# ---------------------------------------------------------------------------

def load_credentials(env_config_path: str) -> Credentials:
    """Load credentials from the global .env.json via the shared loader.

    Navigates the exact nested paths in docs/config.md. Fails fast naming the
    missing path. The Anthropic key is read ONLY from
    anthropic_api_keys.work-todo-trello-grooming-agent (never a flat field).

    Args:
        env_config_path: Path to the global .env.json.

    Returns:
        Populated Credentials.

    Raises:
        CredentialsError: If any required credential path is missing/empty.
    """
    data = load_env_config(config_path=env_config_path)

    def nested(*keys: str) -> str:
        cur = data
        seen: list[str] = []
        for k in keys:
            seen.append(k)
            if not isinstance(cur, dict) or k not in cur:
                raise CredentialsError(
                    f"Missing required credential '{'.'.join(seen)}' in {env_config_path}"
                )
            cur = cur[k]
        if cur is None or cur == "":
            raise CredentialsError(
                f"Credential '{'.'.join(keys)}' is empty in {env_config_path}"
            )
        return str(cur)

    creds = Credentials(
        trello_api_key=nested("trello", "api_key"),
        trello_token=nested("trello", "token"),
        notion_token=nested("notion", "integration_token"),
        gmail_sender=nested("gmail_sender"),
        gmail_password=nested("gmail_password"),
        ollama_endpoint=nested("ollama_endpoint"),
        anthropic_api_key=nested("anthropic_api_keys", AGENT_NAME),
        trello_board_id=str(data.get("trello", {}).get("fira_todo_board_id", "")),
        gmail_recipient=str(data.get("gmail_recipient", "")),
    )
    logger.info("Loaded credentials from %s", env_config_path)
    return creds


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    cfg = sys.argv[1] if len(sys.argv) > 1 else "agent_config.json"
    s = load_settings(cfg)
    print("Settings OK:")
    print(f"  board_shortlink   = {s.board_shortlink}")
    print(f"  edit_scope_lists  = {s.edit_scope_lists}")
    print(f"  comparison_scope  = {s.comparison_scope_lists}")
    print(f"  dry_run           = {s.dry_run}")
    print(f"  tz offsets        = std {s.tz_standard_offset} / dst {s.tz_daylight_offset}")
