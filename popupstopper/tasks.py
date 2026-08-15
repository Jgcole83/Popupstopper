"""Link popups back to the scheduled task or update service that caused them.

Task Scheduler writes event 129 whenever it launches a process, including the
new process's PID and the executable path. Matching that PID against a popup's
process ancestry gives a definitive answer to "what scheduled this?", which is
the whole point of the tool.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000

TASK_LOG = "Microsoft-Windows-TaskScheduler/Operational"
UPDATE_LOG = "Microsoft-Windows-WindowsUpdateClient/Operational"

_EVENT_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"

# Executables that are always Windows Update machinery, with the task family
# they belong to. Used when the event log cannot supply an exact match.
UPDATE_EXECUTABLES = {
    "musnotification.exe": "\\Microsoft\\Windows\\UpdateOrchestrator\\Reboot",
    "musnotificationux.exe": "\\Microsoft\\Windows\\UpdateOrchestrator\\Reboot",
    "usoclient.exe": "\\Microsoft\\Windows\\UpdateOrchestrator\\Schedule Scan",
    "mousocoreworker.exe": "\\Microsoft\\Windows\\UpdateOrchestrator",
    "wuauclt.exe": "\\Microsoft\\Windows\\WindowsUpdate",
}


@dataclass
class TaskLaunch:
    task_name: str
    exe: str
    pid: int
    ts: float
    event_id: int


def _run(args: list[str], timeout: int = 30) -> str:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        return completed.stdout or ""
    except (subprocess.SubprocessError, OSError) as exc:
        log.debug("Command failed %s: %s", args[:2], exc)
        return ""


def _parse_event_time(value: str) -> float:
    """Windows writes 7-digit fractional seconds, which fromisoformat rejects."""
    if not value:
        return 0.0
    cleaned = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
    try:
        return datetime.fromisoformat(cleaned).timestamp()
    except ValueError:
        return 0.0


def _parse_events(xml_text: str) -> list[dict[str, Any]]:
    """wevtutil emits a bare sequence of <Event> elements with no root."""
    if not xml_text.strip():
        return []
    try:
        root = ET.fromstring(f"<Events>{xml_text}</Events>")
    except ET.ParseError:
        return []

    events: list[dict[str, Any]] = []
    for node in root.findall(f"{_EVENT_NS}Event"):
        system = node.find(f"{_EVENT_NS}System")
        if system is None:
            continue
        event_id_node = system.find(f"{_EVENT_NS}EventID")
        time_node = system.find(f"{_EVENT_NS}TimeCreated")
        data: dict[str, str] = {}
        event_data = node.find(f"{_EVENT_NS}EventData")
        if event_data is not None:
            for item in event_data.findall(f"{_EVENT_NS}Data"):
                name = item.get("Name") or f"arg{len(data)}"
                data[name] = (item.text or "").strip()
        events.append(
            {
                "event_id": int(event_id_node.text or 0) if event_id_node is not None else 0,
                "ts": _parse_event_time(time_node.get("SystemTime", "") if time_node is not None else ""),
                "data": data,
            }
        )
    return events


# -- event log availability -----------------------------------------------


def is_task_log_enabled() -> bool:
    output = _run(["wevtutil", "gl", TASK_LOG])
    return bool(re.search(r"^\s*enabled:\s*true", output, re.IGNORECASE | re.MULTILINE))


def enable_task_log() -> tuple[bool, str]:
    """Turn on the Task Scheduler operational log. Needs administrator rights."""
    if is_task_log_enabled():
        return True, "already enabled"
    try:
        completed = subprocess.run(
            ["wevtutil", "sl", TASK_LOG, "/e:true"],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, str(exc)
    if completed.returncode == 0 and is_task_log_enabled():
        log.info("Enabled %s", TASK_LOG)
        return True, "enabled"
    message = (completed.stderr or completed.stdout or "unknown error").strip()
    return False, message


# -- scheduled task definitions -------------------------------------------


def task_definition(task_name: str) -> dict[str, Any]:
    """The executable and arguments a task runs, read from its XML definition."""
    xml_text = _run(["schtasks", "/query", "/tn", task_name, "/xml", "ONE"])
    result: dict[str, Any] = {"task_name": task_name, "actions": [], "triggers": [], "enabled": None}
    if not xml_text.strip():
        return result
    # schtasks emits a UTF-16 BOM that ElementTree will not accept in a str.
    xml_text = xml_text.lstrip("\ufeff")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return result

    def strip_ns(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    for node in root.iter():
        tag = strip_ns(node.tag)
        if tag == "Exec":
            command = ""
            arguments = ""
            for child in node:
                child_tag = strip_ns(child.tag)
                if child_tag == "Command":
                    command = (child.text or "").strip()
                elif child_tag == "Arguments":
                    arguments = (child.text or "").strip()
            if command:
                result["actions"].append({"command": command, "arguments": arguments})
        elif tag == "Settings":
            # Triggers carry their own Enabled flag; only the one directly
            # under Settings describes the task as a whole.
            for child in node:
                if strip_ns(child.tag) == "Enabled":
                    result["enabled"] = (child.text or "").strip().lower() == "true"
        elif tag.endswith("Trigger") and tag != "Triggers":
            result["triggers"].append(tag)

    # Task Scheduler leaves <Enabled> out entirely when a task is enabled,
    # because true is the default, and only writes it when disabled. Absent
    # therefore means enabled, not unknown.
    if result["enabled"] is None:
        result["enabled"] = True
    return result


_TASK_STATES = {0: "Unknown", 1: "Disabled", 2: "Queued", 3: "Ready", 4: "Running"}


def list_tasks() -> list[dict[str, Any]]:
    """All scheduled tasks with their state, for browsing in the UI."""
    output = _run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-ScheduledTask | Select-Object TaskName,TaskPath,State"
            " | ConvertTo-Json -Compress -Depth 3",
        ],
        timeout=60,
    )
    if not output.strip():
        return []
    import json

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    tasks = []
    for item in data:
        if not isinstance(item, dict):
            continue
        path = str(item.get("TaskPath") or "\\")
        name = str(item.get("TaskName") or "")
        raw_state = item.get("State")
        try:
            state = _TASK_STATES.get(int(raw_state), str(raw_state))
        except (TypeError, ValueError):
            state = str(raw_state or "")
        tasks.append(
            {
                "task_name": f"{path}{name}",
                "name": name,
                "path": path,
                "state": state,
                "enabled": state != "Disabled",
            }
        )
    return tasks


# -- live correlation ------------------------------------------------------


class TaskCorrelator:
    """Keeps a rolling window of recent task launches to match popups against."""

    def __init__(self, poll_seconds: float = 15.0, window_seconds: float = 900.0) -> None:
        self._poll_seconds = poll_seconds
        self._window_seconds = window_seconds
        self._launches: list[TaskLaunch] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.log_enabled = False
        self.log_status = "unknown"

    # -- lifecycle ---------------------------------------------------------

    def start(self, auto_enable_log: bool = True) -> None:
        self.log_enabled = is_task_log_enabled()
        if not self.log_enabled and auto_enable_log:
            ok, message = enable_task_log()
            self.log_enabled = ok
            self.log_status = message
        else:
            self.log_status = "enabled" if self.log_enabled else "disabled"

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="task-correlator", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - keep polling
                log.exception("Task correlation refresh failed")
            self._stop.wait(self._poll_seconds)

    # -- data --------------------------------------------------------------

    def refresh(self) -> int:
        if not self.log_enabled:
            self.log_enabled = is_task_log_enabled()
            if not self.log_enabled:
                return 0

        xml_text = _run(
            [
                "wevtutil",
                "qe",
                TASK_LOG,
                "/q:*[System[(EventID=129 or EventID=200)]]",
                "/c:200",
                "/rd:true",
                "/f:xml",
            ]
        )
        events = _parse_events(xml_text)
        launches: list[TaskLaunch] = []
        for event in events:
            data = event["data"]
            task_name = data.get("TaskName", "")
            if not task_name:
                continue
            if event["event_id"] == 129:
                pid_text = data.get("ProcessID", "0")
                exe = data.get("Path", "")
            else:
                pid_text = "0"
                exe = data.get("ActionName", "")
            try:
                pid = int(pid_text or 0)
            except ValueError:
                pid = 0
            launches.append(
                TaskLaunch(
                    task_name=task_name,
                    exe=exe,
                    pid=pid,
                    ts=event["ts"],
                    event_id=event["event_id"],
                )
            )

        cutoff = time.time() - self._window_seconds
        with self._lock:
            self._launches = [item for item in launches if item.ts >= cutoff]
        return len(self._launches)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(self._launches, key=lambda item: item.ts, reverse=True)[:limit]
        return [item.__dict__ for item in items]

    def find(
        self,
        pids: Iterable[int],
        when: float,
        slack_seconds: float = 45.0,
    ) -> dict[str, Any] | None:
        """Match a popup to a task, preferring an exact PID hit over timing."""
        pid_set = {pid for pid in pids if pid and pid > 0}
        with self._lock:
            launches = list(self._launches)

        for launch in sorted(launches, key=lambda item: item.ts, reverse=True):
            if launch.pid and launch.pid in pid_set:
                return {
                    "task_name": launch.task_name,
                    "task_exe": launch.exe,
                    "confidence": "exact",
                    "launched_at": launch.ts,
                }

        best: TaskLaunch | None = None
        for launch in launches:
            delta = when - launch.ts
            if 0 <= delta <= slack_seconds and (best is None or launch.ts > best.ts):
                best = launch
        if best is not None:
            return {
                "task_name": best.task_name,
                "task_exe": best.exe,
                "confidence": "timing",
                "launched_at": best.ts,
            }
        return None

    def attribute(self, exe_name: str, pids: Iterable[int], when: float) -> dict[str, Any] | None:
        """Task attribution, falling back to known Windows Update executables."""
        match = self.find(pids, when)
        if match:
            return match
        known = UPDATE_EXECUTABLES.get((exe_name or "").lower())
        if known:
            return {
                "task_name": known,
                "task_exe": exe_name,
                "confidence": "known-component",
                "launched_at": None,
            }
        return None


def recent_update_activity(limit: int = 20) -> list[dict[str, Any]]:
    """Latest Windows Update client events, shown as context in the UI."""
    xml_text = _run(["wevtutil", "qe", UPDATE_LOG, f"/c:{limit}", "/rd:true", "/f:xml"])
    out = []
    for event in _parse_events(xml_text):
        data = event["data"]
        out.append(
            {
                "event_id": event["event_id"],
                "ts": event["ts"],
                "title": data.get("updateTitle") or data.get("updateGuid") or "",
                "data": data,
            }
        )
    return out
