"""Utility to swap Axis camera image profiles for day and night use.

The script uses the Axis VAPIX `param.cgi` endpoint to push a set of
parameters defined in a YAML configuration file. Cameras, credentials, profiles,
and schedules are all loaded from YAML so deployments can be driven entirely by
configuration rather than command-line flags.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

import requests
from requests.auth import HTTPDigestAuth
import yaml


@dataclass
class ImageProfile:
    """Collection of Axis image parameters for a given lighting scenario."""

    name: str
    parameters: Mapping[str, str]
    description: Optional[str] = None

    def normalized_parameters(self) -> Dict[str, str]:
        """Return a copy with all parameter values converted to strings."""

        return {key: str(value) for key, value in self.parameters.items()}


@dataclass
class CameraConfig:
    """Axis camera connection details and credential sources."""

    name: str
    host: str
    protocol: str = "http"
    supports_profiles: bool = True
    username: Optional[str] = None
    password: Optional[str] = None
    username_env: Optional[str] = None
    password_env: Optional[str] = None

    def resolve_credentials(self) -> tuple[str, str]:
        username = self.username
        password = self.password

        if username is None and self.username_env:
            username = os.environ.get(self.username_env)
        if password is None and self.password_env:
            password = os.environ.get(self.password_env)

        missing = []
        if not username:
            missing.append("username")
        if not password:
            missing.append("password")

        if missing:
            env_notes = []
            if self.username_env:
                env_notes.append(f"username env var '{self.username_env}'")
            if self.password_env:
                env_notes.append(f"password env var '{self.password_env}'")
            env_hint = ", ".join(env_notes) or "provide credentials"
            raise ValueError(
                f"Camera '{self.name}' is missing {', '.join(missing)}; set them in config or via {env_hint}."
            )

        return username, password


@dataclass
class ScheduleEntry:
    profile: str
    start: time
    end: time

    @classmethod
    def from_strings(cls, *, profile: str, start: str, end: str) -> "ScheduleEntry":
        return cls(profile=profile, start=parse_hhmm(start), end=parse_hhmm(end))


@dataclass
class Schedule:
    name: str
    entries: list[ScheduleEntry]
    timezone: Optional[str] = None

    def resolve_profile(self, *, now: Optional[datetime] = None) -> str:
        if now is None:
            if self.timezone:
                now = datetime.now(ZoneInfo(self.timezone))
            else:
                now = datetime.now()

        current_time = now.time()

        for entry in self.entries:
            if is_time_in_range(current_time, entry.start, entry.end):
                return entry.profile

        available = ", ".join(sorted(entry.profile for entry in self.entries))
        raise RuntimeError(
            f"No schedule entry matched current time {current_time.isoformat(timespec='minutes')}."
            f" Available profiles in schedule '{self.name}': {available}."
        )


@dataclass
class AxisConfig:
    cameras: Dict[str, CameraConfig]
    profiles: Dict[str, ImageProfile]
    schedules: Dict[str, Schedule]


class AxisCameraClient:
    """Minimal client for the Axis `param.cgi` API."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        protocol: str = "http",
        timeout: float = 5.0,
        max_retries: int = 3,
    ) -> None:
        self.base_url = f"{protocol}://{host}".rstrip("/")
        self.auth = HTTPDigestAuth(username, password)
        self.timeout = timeout

        retry_strategy = requests.adapters.Retry(
            total=max_retries,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST"),
        )
        adapter = requests.adapters.HTTPAdapter(max_retries=retry_strategy)

        session = requests.Session()
        session.auth = self.auth
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self.session = session

    def update_parameters(self, parameters: Mapping[str, str]) -> requests.Response:
        payload: Dict[str, str] = {"action": "update"}
        payload.update(parameters)

        response = self.session.post(
            f"{self.base_url}/axis-cgi/param.cgi",
            data=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()

        if "Error:" in response.text:
            raise RuntimeError(f"Camera rejected parameters: {response.text}")

        return response

    def fetch_parameters(self, parameter_names: Iterable[str]) -> Dict[str, str]:
        """Best-effort fetch of current parameter values for idempotency checks."""

        results: Dict[str, str] = {}
        grouped: Dict[str, list[str]] = {}
        for key in parameter_names:
            group = key.rsplit(".", 1)[0] if "." in key else key
            grouped.setdefault(group, []).append(key)

        for group, names in grouped.items():
            try:
                response = self.session.get(
                    f"{self.base_url}/axis-cgi/param.cgi",
                    params={"action": "list", "group": group},
                    timeout=self.timeout,
                )
                response.raise_for_status()
            except requests.RequestException:
                continue

            for line in response.text.splitlines():
                if "=" not in line:
                    continue
                param_name, value = line.split("=", 1)
                if param_name in names:
                    results[param_name] = value

        return results

    def is_profile_applied(self, profile: ImageProfile) -> bool:
        desired = profile.normalized_parameters()
        current = self.fetch_parameters(desired.keys())
        if not current:
            return False
        return all(current.get(key) == value for key, value in desired.items())

    def apply_profile(self, profile: ImageProfile) -> Optional[requests.Response]:
        if self.is_profile_applied(profile):
            return None
        return self.update_parameters(profile.normalized_parameters())


def parse_hhmm(value: str) -> time:
    try:
        hour, minute = value.split(":", maxsplit=1)
        return time(hour=int(hour), minute=int(minute))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Invalid HH:MM time value '{value}'.") from exc


def is_time_in_range(current: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def load_profiles(raw_profiles: object) -> Dict[str, ImageProfile]:
    if not isinstance(raw_profiles, dict):
        raise ValueError("The 'profiles' entry must be a mapping from profile names to parameter sets.")

    profiles: Dict[str, ImageProfile] = {}
    for name, entry in raw_profiles.items():
        if not isinstance(entry, dict) or "parameters" not in entry:
            raise ValueError(f"Profile '{name}' must define a 'parameters' mapping.")

        parameters = entry["parameters"]
        if not isinstance(parameters, dict):
            raise ValueError(f"Parameters for profile '{name}' must be a mapping.")

        description = entry.get("description") if isinstance(entry.get("description"), str) else None
        profiles[name] = ImageProfile(name=name, parameters=parameters, description=description)

    return profiles


def load_cameras(raw_cameras: object) -> Dict[str, CameraConfig]:
    if not isinstance(raw_cameras, dict):
        raise ValueError("The 'cameras' entry must be a mapping of names to camera configs.")

    cameras: Dict[str, CameraConfig] = {}
    for name, entry in raw_cameras.items():
        if not isinstance(entry, dict) or "host" not in entry:
            raise ValueError(f"Camera '{name}' must define a 'host'.")

        protocol = entry.get("protocol", "http")
        if protocol not in {"http", "https"}:
            raise ValueError(f"Camera '{name}' has unsupported protocol '{protocol}'.")

        supports_profiles = entry.get("supports_profiles", True)
        if not isinstance(supports_profiles, bool):
            raise ValueError(f"Camera '{name}' has non-boolean 'supports_profiles' value.")

        cameras[name] = CameraConfig(
            name=name,
            host=str(entry["host"]),
            protocol=str(protocol),
            supports_profiles=supports_profiles,
            username=entry.get("username"),
            password=entry.get("password"),
            username_env=entry.get("username_env"),
            password_env=entry.get("password_env"),
        )

    return cameras


def load_schedules(raw_schedules: object) -> Dict[str, Schedule]:
    if raw_schedules is None:
        return {}
    if not isinstance(raw_schedules, dict):
        raise ValueError("The 'schedules' entry must be a mapping of names to schedules.")

    schedules: Dict[str, Schedule] = {}
    for name, entry in raw_schedules.items():
        if not isinstance(entry, dict) or "entries" not in entry:
            raise ValueError(f"Schedule '{name}' must contain an 'entries' list.")

        raw_entries = entry.get("entries")
        if not isinstance(raw_entries, list) or not raw_entries:
            raise ValueError(f"Schedule '{name}' must define at least one entry.")

        entries: list[ScheduleEntry] = []
        for item in raw_entries:
            if not isinstance(item, dict) or "profile" not in item or "start" not in item or "end" not in item:
                raise ValueError(
                    f"Each schedule entry in '{name}' must include 'profile', 'start', and 'end' fields."
                )
            entries.append(
                ScheduleEntry.from_strings(profile=str(item["profile"]), start=str(item["start"]), end=str(item["end"]))
            )

        timezone = entry.get("timezone") if isinstance(entry.get("timezone"), str) else None
        schedules[name] = Schedule(name=name, entries=entries, timezone=timezone)

    return schedules


def load_config(config_path: Path) -> AxisConfig:
    data = yaml.safe_load(config_path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"Config file {config_path} must contain mappings for cameras and optional profiles.")

    if "cameras" not in data:
        raise ValueError("Config must define 'cameras'.")

    raw_profiles = data.get("profiles", {})
    profiles = load_profiles(raw_profiles) if raw_profiles is not None else {}
    cameras = load_cameras(data["cameras"])
    schedules = load_schedules(data.get("schedules"))

    return AxisConfig(cameras=cameras, profiles=profiles, schedules=schedules)


def load_parameter_file(path: Path) -> Dict[str, str]:
    """Load a simple YAML mapping of parameter names to values."""

    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"Parameter file {path} must contain a mapping of VAPIX parameters to values.")
    return {str(key): str(value) for key, value in data.items()}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push Axis camera image parameters from YAML or a schedule.")
    parser.add_argument("--camera", required=True, help="Camera name defined in the config file")
    parser.add_argument("--profile", help="Profile name to apply (e.g., 'day' or 'night')")
    parser.add_argument("--schedule", help="Schedule name to use for picking a profile")
    parser.add_argument(
        "--parameters-file",
        help=(
            "Path to a YAML mapping of parameters to apply directly (bypasses profiles and schedules). "
            "Useful when the camera lacks profile support."
        ),
    )
    parser.add_argument(
        "--config",
        default="axis_config.yaml",
        help="Path to YAML config containing cameras, profiles, and schedules (default: axis_config.yaml)",
    )
    parser.add_argument("--timeout", type=float, default=5.0, help="Request timeout in seconds (default: 5)")
    parser.add_argument("--retries", type=int, default=3, help="Number of retries for transient failures (default: 3)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(Path(args.config))

    if args.camera not in config.cameras:
        available = ", ".join(sorted(config.cameras))
        sys.exit(f"Camera '{args.camera}' not found. Available cameras: {available}.")

    camera = config.cameras[args.camera]
    try:
        username, password = camera.resolve_credentials()
    except ValueError as exc:
        sys.exit(str(exc))

    chosen_parameters: Optional[Dict[str, str]] = None
    summary: str = ""

    if args.parameters_file:
        chosen_parameters = load_parameter_file(Path(args.parameters_file))
        summary = f"parameters from {args.parameters_file}"
    else:
        if not camera.supports_profiles:
            sys.exit(
                f"Camera '{camera.name}' is marked as not supporting profiles. "
                "Use --parameters-file to apply raw settings."
            )
        if not args.profile and not args.schedule:
            sys.exit("Provide --parameters-file, --profile, or --schedule to choose what to apply.")
        if args.profile and args.schedule:
            sys.exit("Specify only one of --profile or --schedule.")

        if args.profile:
            profile_name = args.profile
        else:
            schedule_name = args.schedule
            assert schedule_name is not None
            schedule = config.schedules.get(schedule_name)
            if schedule is None:
                available = ", ".join(sorted(config.schedules)) or "none"
                sys.exit(f"Schedule '{schedule_name}' not found. Available schedules: {available}.")
            profile_name = schedule.resolve_profile()

        if profile_name not in config.profiles:
            available = ", ".join(sorted(config.profiles)) or "none"
            sys.exit(f"Profile '{profile_name}' not found. Available profiles: {available}.")

        profile = config.profiles[profile_name]
        chosen_parameters = profile.normalized_parameters()
        summary = profile.description or ""
        if summary:
            summary = f" ({summary})"
        summary = f"profile '{profile.name}'{summary}"

    assert chosen_parameters is not None
    client = AxisCameraClient(
        host=camera.host,
        username=username,
        password=password,
        protocol=camera.protocol,
        timeout=args.timeout,
        max_retries=args.retries,
    )

    try:
        if args.parameters_file:
            response = client.update_parameters(chosen_parameters)
        else:
            profile_obj = config.profiles[profile_name]
            response = client.apply_profile(profile_obj)
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        if status in {401, 403}:
            sys.exit("Camera authentication failed.")
        sys.exit(f"Camera returned HTTP {status} while applying profile: {exc}")
    except requests.exceptions.ConnectTimeout:
        sys.exit(f"Camera unreachable at host {camera.host} (timed out after {args.timeout}s).")
    except requests.exceptions.Timeout:
        sys.exit(f"Camera unreachable at host {camera.host} (timed out after {args.timeout}s).")
    except requests.exceptions.ConnectionError as exc:
        sys.exit(f"Camera unreachable at host {camera.host}: {exc}")
    except requests.RequestException as exc:
        sys.exit(f"Failed to communicate with camera: {exc}")
    except RuntimeError as exc:
        sys.exit(str(exc))

    if args.parameters_file:
        print(f"Applied {summary} to camera '{camera.name}'. HTTP status: {response.status_code}.")
    elif response is None:
        print(f"Profile '{profile_obj.name}'{summary} is already applied on camera '{camera.name}'. No changes made.")
    else:
        print(
            f"Applied profile '{profile_obj.name}'{summary} to camera '{camera.name}'. HTTP status: {response.status_code}."
        )


if __name__ == "__main__":
    main()
