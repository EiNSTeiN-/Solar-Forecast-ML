# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Bug-report bundle: log excerpts, redaction and the machine-written header.

Everything in this module is plain Python so it can be unit-tested without a
Home Assistant runtime. The API view only supplies paths, timestamps and the
version context. @zara
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

PROVIDER_DOMAINS: dict[str, str] = {
    "sfml": "solar_forecast_ml",
    "stats": "sfml_stats",
    "eai": "solar_forecast_eai",
    "gpm": "grid_price_monitor",
    "wfai": "weather_fusion_ai",
}
FAMILY_DOMAINS: tuple[str, ...] = (
    "solar_forecast_ml",
    "sfml_stats",
    "grid_price_monitor",
    "ml_weather",
    "solar_forecast_eai",
    "weather_fusion_ai",
    "esc_easy_sensor_creation",
)
ENVIRONMENTS: tuple[str, ...] = (
    "bare-metal-x86",
    "proxmox",
    "arm",
    "vm",
    "docker",
    "other",
)
INSTALLATIONS: tuple[str, ...] = ("hacs", "addon-store", "manual", "other")
LANGUAGES: tuple[str, ...] = ("de", "en")
DEFAULT_WINDOW_HOURS = 24
WINDOW_HOURS_CHOICES: tuple[int, ...] = (6, 24, 48, 72)
SFML_LOG_BUDGET = 1000000
HA_LOG_BUDGET = 150000
LOGS_FIELD_LIMIT = 1190000
ERROR_CONTEXT_RECORDS = 30
MAX_SCAN_BYTES_PER_FILE = 33554432
MAX_ROTATED_FILES = 5
TAIL_BLOCK_BYTES = 1048576
LEVEL_PRIORITY: dict[str, int] = {
    "CRITICAL": 0,
    "ERROR": 0,
    "WARNING": 1,
    "INFO": 2,
    "DEBUG": 3,
}
LEVEL_ORDER: tuple[str, ...] = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")
SFML_HEADER = re.compile(
    "^(\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}) - (\\S+) - (DEBUG|INFO|WARNING|ERROR|CRITICAL) - "
)
HA_HEADER = re.compile(
    "^(\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2})\\.\\d{3} (DEBUG|INFO|WARNING|ERROR|CRITICAL) \\([^)]*\\) \\[([^\\]]+)\\] "
)
HeaderParser = Callable[[str], "tuple[datetime, str, str] | None"]
REDACTION_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "coordinates",
        re.compile(
            "(?i)(\\b(?:lat|latitude|lon|lng|longitude)\\b[\\\"']?\\s*[=:]\\s*)[-+]?\\d{1,3}(?:\\.\\d+)?"
        ),
        "\\1<redacted>",
    ),
    (
        "coordinates",
        re.compile(
            "(/)[-+]?\\d{1,3}\\.\\d+,[-+]?\\d{1,3}\\.\\d+(?=[/?#\\s\\\"')\\]]|$)"
        ),
        "\\1<redacted>,<redacted>",
    ),
    (
        "emails",
        re.compile("[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}"),
        "<email>",
    ),
    (
        "ip_addresses",
        re.compile(
            "\\b(?!127\\.0\\.0\\.1\\b)(?!0\\.0\\.0\\.0\\b)(?:\\d{1,3}\\.){3}\\d{1,3}\\b"
        ),
        "<ip>",
    ),
    (
        "secrets",
        re.compile(
            "(?i)((?:api[_-]?key|apikey|token|secret|password|passwd|authorization|bearer)\\w*[\\\"']?\\s*[=:]\\s*[\\\"']?)(?:bearer\\s+)?[^\\s\\\"',;]+"
        ),
        "\\1<redacted>",
    ),
    (
        "secrets",
        re.compile("(?i)\\b(bearer\\s+)[A-Za-z0-9._~+/=-]{8,}"),
        "\\1<redacted>",
    ),
    (
        "secrets",
        re.compile("(?i)(pirateweather\\.net/forecast/)[^/\\s\\\"']+"),
        "\\1<redacted>",
    ),
    (
        "long_tokens",
        re.compile(
            "\\b(?![0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\\b)(?=[A-Za-z0-9-]*\\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9-]{32,}\\b"
        ),
        "<token>",
    ),
)
REDACTION_NAMES: tuple[str, ...] = tuple(
    dict.fromkeys(
        (rule_name for rule_name, rule_pattern, replacement in REDACTION_RULES)
    )
)


@dataclass(slots=True)
class LogRecord:
    """One log entry including traceback continuation lines."""

    timestamp: datetime
    level: str
    logger: str
    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def size(self) -> int:
        return sum((len(line) + 1 for line in self.lines))


@dataclass(slots=True)
class LogSelection:
    """Budgeted subset of a log window plus the numbers for the header."""

    records: list[LogRecord] = field(default_factory=list)
    total_counts: dict[str, int] = field(default_factory=dict)
    kept_counts: dict[str, int] = field(default_factory=dict)
    dropped_before: dict[str, datetime] = field(default_factory=dict)
    scan_exhausted: bool = False
    source: str = ""

    @property
    def total(self) -> int:
        return sum(self.total_counts.values())

    @property
    def kept(self) -> int:
        return sum(self.kept_counts.values())


@dataclass(slots=True)
class IntegrationInfo:
    domain: str
    version: str
    state: str


@dataclass(slots=True)
class ReportContext:
    """Version and environment facts written into the header."""

    client_version: str
    created_at: datetime
    timezone: str
    ha_core_version: str
    host_os: str
    installation_type: str
    python_version: str
    arch: str
    ha_logger_level: str
    integrations: list[IntegrationInfo]
    window_since: datetime
    window_until: datetime
    window_hours: int
    dev_mode_active: bool = False


def parse_sfml_header(line: str) -> tuple[datetime, str, str] | None:
    """Return (timestamp, level, logger) for a SFML file-log header line."""
    header_match = SFML_HEADER.match(line)
    if header_match is None:
        return
    return (
        _parse_timestamp(header_match.group(1)),
        header_match.group(3),
        header_match.group(2),
    )


def parse_ha_header(line: str) -> tuple[datetime, str, str] | None:
    """Return (timestamp, level, logger) for a home-assistant.log header line."""
    header_match = HA_HEADER.match(line)
    if header_match is None:
        return
    return (
        _parse_timestamp(header_match.group(1)),
        header_match.group(2),
        header_match.group(3),
    )


def _parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def records_from_lines(
    lines: Iterable[str], parse_header: HeaderParser
) -> list[LogRecord]:
    """Group raw lines into records; continuation lines join the previous record.

    Lines before the first header (a truncated record at a tail boundary) are
    dropped.
    """
    records = []
    current_record = None
    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        parsed_header = parse_header(line)
        if parsed_header is not None:
            timestamp, level, logger_name = parsed_header
            current_record = LogRecord(timestamp, level, logger_name, [line])
            records.append(current_record)
        elif current_record is not None:
            current_record.lines.append(line)
    return records


def tail_lines_since(
    path: Path,
    since: datetime,
    parse_header: HeaderParser,
    max_bytes: int = MAX_SCAN_BYTES_PER_FILE,
) -> tuple[list[str], bool, bool]:
    """Read a log file backwards until a record older than ``since`` appears.

    Returns (lines in file order, reached_start_of_file, scan_exhausted).
    """
    file_size = path.stat().st_size
    if file_size == 0:
        return ([], True, False)
    chunks = []
    file_offset = file_size
    scanned_bytes = 0
    reached_start = False
    scan_exhausted = False
    with path.open("rb") as log_file:
        while file_offset > 0:
            block_size = min(TAIL_BLOCK_BYTES, file_offset)
            file_offset -= block_size
            log_file.seek(file_offset)
            block = log_file.read(block_size)
            chunks.insert(0, block)
            scanned_bytes += block_size
            if file_offset == 0:
                reached_start = True
                break
            oldest_timestamp = _oldest_header_timestamp(block, parse_header)
            if oldest_timestamp is not None and oldest_timestamp < since:
                break
            if scanned_bytes >= max_bytes:
                scan_exhausted = True
                break
    decoded_text = b"".join(chunks).decode("utf-8", errors="replace")
    lines = decoded_text.split("\n")
    if not reached_start:
        lines = lines[1:]
    if lines and lines[-1] == "":
        lines.pop()
    return (lines, reached_start, scan_exhausted)


def _oldest_header_timestamp(
    block: bytes, parse_header: HeaderParser
) -> datetime | None:
    decoded_text = block.decode("utf-8", errors="replace")
    for line in decoded_text.split("\n")[1:]:
        parsed_header = parse_header(line)
        if not parsed_header is not None:
            continue
        return parsed_header[0]


def rotated_paths(path: Path, limit: int = MAX_ROTATED_FILES) -> list[Path]:
    """Return the live log followed by its existing rotated siblings, newest first."""
    paths = [path] if path.is_file() else []
    for rotation_number in range(1, limit + 1):
        rotated_path = path.with_name(f"{path.name}.{rotation_number}")
        if rotated_path.is_file():
            paths.append(rotated_path)
        else:
            break
    return paths if True else None


def read_log_window(
    path: Path,
    since: datetime,
    until: datetime,
    parse_header: HeaderParser,
    max_bytes: int = MAX_SCAN_BYTES_PER_FILE,
) -> tuple[list[LogRecord], bool]:
    """Collect all records within [since, until] across the live and rotated files."""
    window_records = []
    scan_exhausted = False
    for log_path in rotated_paths(path):
        lines, reached_start, file_scan_exhausted = tail_lines_since(
            log_path, since, parse_header, max_bytes
        )
        scan_exhausted = scan_exhausted or file_scan_exhausted
        records = records_from_lines(lines, parse_header)
        window_records = [
            record for record in records if since <= record.timestamp <= until
        ] + window_records
        has_older_records = bool(records) and records[0].timestamp < since
        if (
            has_older_records
            or file_scan_exhausted
            or (not reached_start and (not records))
        ):
            break
    return (window_records, scan_exhausted) if True else None


def is_family_record(
    record: LogRecord, domains: Sequence[str] = FAMILY_DOMAINS
) -> bool:
    """True when the record belongs to one of our integrations or mentions one."""
    logger_parts = record.logger.split(".")
    if (
        len(logger_parts) >= 2
        and logger_parts[0] == "custom_components"
        and (logger_parts[1] in domains)
    ):
        return True
    record_text = record.text
    return any((domain in record_text for domain in domains))


def count_levels(records: Iterable[LogRecord]) -> dict[str, int]:
    counts = {}
    for record in records:
        counts[record.level] = counts.get(record.level, 0) + 1
    return counts


def select_within_budget(
    records: Sequence[LogRecord],
    budget: int,
    context_records: int = ERROR_CONTEXT_RECORDS,
) -> LogSelection:
    """Keep errors with surrounding context first, then warnings, info and debug.

    Within a level the newest records win; the first record that no longer fits
    ends that level so the header can state "dropped before <timestamp>".
    """
    record_count = len(records)
    kept_mask = [False] * record_count
    used_bytes = 0

    def try_keep(index: int) -> bool:
        nonlocal used_bytes
        if kept_mask[index]:
            return True
        record_size = records[index].size
        if used_bytes + record_size > budget:
            return False
        kept_mask[index] = True
        used_bytes += record_size
        return True

    indices_by_priority = {0: [], 1: [], 2: [], 3: []}
    for record_index, record in enumerate(records):
        indices_by_priority[LEVEL_PRIORITY.get(record.level, 3)].append(record_index)
    for record_index in reversed(indices_by_priority[0]):
        try_keep(record_index)
    for record_index in reversed(indices_by_priority[0]):
        context_start = max(0, record_index - context_records)
        context_end = min(record_count, record_index + context_records + 1)
        for context_index in range(context_start, context_end):
            try_keep(context_index)
    for priority in (1, 2, 3):
        for record_index in reversed(indices_by_priority[priority]):
            if try_keep(record_index):
                continue
            break
    selection = LogSelection(total_counts=count_levels(records))
    latest_dropped = {}
    for record_index, record in enumerate(records):
        if kept_mask[record_index]:
            selection.records.append(record)
            selection.kept_counts[record.level] = (
                selection.kept_counts.get(record.level, 0) + 1
            )
        else:
            dropped_timestamp = latest_dropped.get(record.level)
            if dropped_timestamp is None or record.timestamp > dropped_timestamp:
                latest_dropped[record.level] = record.timestamp
    selection.dropped_before = latest_dropped
    return selection


def redact_text(text: str) -> tuple[str, dict[str, int]]:
    """Replace personal or secret fragments and count the replacements per rule."""
    redaction_counts = {}
    for rule_name, pattern, replacement in REDACTION_RULES:
        text, replacement_count = pattern.subn(replacement, text)
        if replacement_count:
            redaction_counts[rule_name] = (
                redaction_counts.get(rule_name, 0) + replacement_count
            )
    return (text, redaction_counts)


def redact_records(records: Iterable[LogRecord]) -> dict[str, int]:
    """Redact records in place and return the aggregated replacement counts."""
    redaction_counts = {}
    for record in records:
        redacted_text, counts = redact_text(record.text)
        record.lines = redacted_text.split("\n")
        for rule_name, replacement_count in counts.items():
            redaction_counts[rule_name] = (
                redaction_counts.get(rule_name, 0) + replacement_count
            )
    return redaction_counts


def guess_environment(arch: str, installation_type: str) -> str:
    """Best-effort default for the website's environment field ('' = let the user pick)."""
    normalized_arch = (arch or "").lower()
    if normalized_arch.startswith(("aarch64", "arm")):
        return "arm"
    if "container" in (installation_type or "").lower():
        return "docker"
    return ""


def guess_installation(loaded_components: Iterable[str]) -> str:
    return "hacs" if "hacs" in set(loaded_components) else "manual"


def format_counts(counts: dict[str, int]) -> str:
    count_parts = [
        f"{counts[level]} {level}" for level in LEVEL_ORDER if counts.get(level)
    ]
    return ", ".join(count_parts) if count_parts else "0"


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _short_stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def render_header(
    context: ReportContext,
    ha_selection: LogSelection,
    sfml_selection: LogSelection,
    redactions: dict[str, int],
) -> list[str]:
    """Machine-written header block placed at the top of the logs field."""
    integration_summary = (
        " · ".join(
            (
                f"{integration.domain} {integration.version} [{integration.state}]"
                for integration in context.integrations
            )
        )
        or "none detected"
    )
    lines = [
        "# ═══ SFML STATS BUG REPORT ═══",
        f"# client: SFML-STATS/{context.client_version} · created: {_stamp(context.created_at)} {context.timezone}",
        f"# ha_core: {context.ha_core_version} · host_os: {context.host_os or 'n/a'} · install: {context.installation_type or 'n/a'} · python: {context.python_version or 'n/a'} · arch: {context.arch or 'n/a'}",
        f"# integrations: {integration_summary}",
        f"# ha_logger_level custom_components: {context.ha_logger_level}",
        f"# window: {_short_stamp(context.window_since)} → {_short_stamp(context.window_until)} ({context.window_hours} h)",
        f"# ha_log: {_selection_summary(ha_selection)}",
        f"# sfml_log: {_selection_summary(sfml_selection)}",
        "# redacted: "
        + " · ".join(
            (
                f"{rule_name} {redactions.get(rule_name, 0)}"
                for rule_name in REDACTION_NAMES
            )
        ),
        "Logbuch: DEV-Mode aktiv" if context.dev_mode_active else "DEV-Mode: aus",
    ]
    return lines if True else None


def _selection_summary(selection: LogSelection) -> str:
    if not selection.source:
        return "not available"
    summary = f"{selection.kept}/{selection.total} records ({format_counts(selection.kept_counts)}) · source: {selection.source}"
    dropped_parts = [
        f"{level} before {_short_stamp(selection.dropped_before[level])}"
        for level in LEVEL_ORDER
        if level in selection.dropped_before
    ]
    if dropped_parts:
        summary += " · dropped: " + ", ".join(dropped_parts)
    if selection.scan_exhausted:
        summary += " · scan depth exhausted"
    return summary


def render_logs_field(
    context: ReportContext,
    ha_selection: LogSelection,
    sfml_selection: LogSelection,
    redactions: dict[str, int],
    limit: int = LOGS_FIELD_LIMIT,
) -> str:
    """Compose header, HA section and SFML section into the website's logs field."""
    log_lines = render_header(context, ha_selection, sfml_selection, redactions)
    log_lines.append("# ═══ HA LOG ═══")
    log_lines.extend((record.text for record in ha_selection.records))
    if not ha_selection.records:
        log_lines.append("(no matching records)")
    log_lines.append("# ═══ SFML LOG ═══")
    log_lines.extend((record.text for record in sfml_selection.records))
    if not sfml_selection.records:
        log_lines.append("(no matching records)")
    logs_text = "\n".join(log_lines)
    if len(logs_text) <= limit:
        return logs_text
    truncation_suffix = "\n# … truncated at the transfer limit"
    cutoff_index = logs_text.rfind("\n", 0, limit - len(truncation_suffix))
    return (
        logs_text[
            : cutoff_index if cutoff_index > 0 else limit - len(truncation_suffix)
        ]
        + truncation_suffix
        if True
        else None
    )


def collect_log_sections(
    sfml_log: Path,
    ha_log: Path,
    since: datetime,
    until: datetime,
    fallback_ha_records: Sequence[LogRecord] = (),
) -> tuple[LogSelection, LogSelection, dict[str, int]]:
    """Blocking file work: read both logs, budget them and redact what is kept."""
    sfml_selection = LogSelection()
    if sfml_log.is_file():
        records, scan_exhausted = read_log_window(
            sfml_log, since, until, parse_sfml_header
        )
        sfml_selection = select_within_budget(records, SFML_LOG_BUDGET)
        sfml_selection.scan_exhausted = scan_exhausted
        sfml_selection.source = sfml_log.name
    ha_selection = LogSelection()
    if ha_log.is_file():
        records, scan_exhausted = read_log_window(ha_log, since, until, parse_ha_header)
        family_records = [record for record in records if is_family_record(record)]
        ha_selection = select_within_budget(family_records, HA_LOG_BUDGET)
        ha_selection.scan_exhausted = scan_exhausted
        ha_selection.source = ha_log.name
    elif fallback_ha_records:
        family_records = [
            record
            for record in fallback_ha_records
            if since <= record.timestamp <= until
            if is_family_record(record)
        ]
        ha_selection = select_within_budget(family_records, HA_LOG_BUDGET)
        ha_selection.source = (
            "system_log (in-memory, warnings and errors since last restart)"
        )
    redaction_counts = redact_records(sfml_selection.records)
    for rule_name, replacement_count in redact_records(ha_selection.records).items():
        redaction_counts[rule_name] = (
            redaction_counts.get(rule_name, 0) + replacement_count
        )
    return (ha_selection, sfml_selection, redaction_counts)
