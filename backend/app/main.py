from __future__ import annotations

import os
import json
import logging
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import zipfile
import urllib.error
import urllib.request
import uuid
import secrets
import sqlite3
import hashlib
from contextlib import contextmanager
from copy import deepcopy
from urllib.parse import urlparse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from collections.abc import Iterator

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

app = FastAPI(title="TRACEFORGE API", version="2.0.0")
logger = logging.getLogger("traceforge.ai_review")
APPROVAL_TOKEN_TTL_SECONDS = int(os.getenv("TRACEFORGE_APPROVAL_TOKEN_TTL_SECONDS", "3600"))
CONFIGURED_DATABASE_PATH = os.getenv("TRACEFORGE_DATABASE_PATH", "").strip()
if CONFIGURED_DATABASE_PATH and not Path(CONFIGURED_DATABASE_PATH).is_absolute():
    raise RuntimeError("TRACEFORGE_DATABASE_PATH must be an absolute path.")
DATABASE_PATH = Path(CONFIGURED_DATABASE_PATH or (
    Path(__file__).resolve().parents[1] / "traceforge.sqlite3"
))


@contextmanager
def _database_connection() -> Iterator[sqlite3.Connection]:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=30, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        yield connection
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _initialize_database() -> None:
    with _database_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS verifications (
                verification_id TEXT PRIMARY KEY,
                passport_json TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                token_expires_at REAL NOT NULL,
                decision TEXT NOT NULL CHECK (decision IN ('PENDING', 'APPROVED', 'REJECTED')),
                decided_at TEXT,
                created_at TEXT NOT NULL
            )
        """)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_initialize_database()


def _store_verification(passport: dict[str, Any], token: str, expires_at: float) -> None:
    verification_id = passport["verification_id"]
    with _database_connection() as connection:
        connection.execute(
            """
            INSERT INTO verifications
              (verification_id, passport_json, token_hash, token_expires_at, decision, created_at)
            VALUES (?, ?, ?, ?, 'PENDING', ?)
            """,
            (
                verification_id,
                json.dumps(passport, sort_keys=True),
                _token_hash(token),
                expires_at,
                passport.get("created_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            ),
        )


def _passport_row(verification_id: str) -> sqlite3.Row | None:
    with _database_connection() as connection:
        return connection.execute(
            "SELECT * FROM verifications WHERE verification_id = ?",
            (verification_id,),
        ).fetchone()


def _passport_from_row(row: sqlite3.Row) -> dict[str, Any]:
    passport = json.loads(row["passport_json"])
    passport["human_approval"].update({
        "status": row["decision"],
        "decision": row["decision"],
        "verification_id": row["verification_id"],
        "decided_at": row["decided_at"],
    })
    return passport


def _authorize_token(row: sqlite3.Row, token: str) -> bool:
    return bool(token) and time.time() <= row["token_expires_at"] and secrets.compare_digest(
        _token_hash(token),
        row["token_hash"],
    )


TRACEFORGE_ENV = os.getenv("TRACEFORGE_ENV", "development").strip().lower()
TRACEFORGE_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("TRACEFORGE_ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]
if TRACEFORGE_ENV == "production":
    if not TRACEFORGE_ALLOWED_ORIGINS or "*" in TRACEFORGE_ALLOWED_ORIGINS:
        raise RuntimeError("TRACEFORGE_ALLOWED_ORIGINS must explicitly list origins in production.")
    configured_api_url = os.getenv("TRACEFORGE_API_URL", "").strip()
    if not configured_api_url or urlparse(configured_api_url).scheme != "https":
        raise RuntimeError("TRACEFORGE_API_URL must use HTTPS in production.")
    if any(urlparse(origin).scheme != "https" for origin in TRACEFORGE_ALLOWED_ORIGINS):
        raise RuntimeError("TRACEFORGE_ALLOWED_ORIGINS must use HTTPS in production.")


@app.get("/")
def root():
    return {
        "name": "TRACEFORGE API",
        "version": "2.0.0",
        "status": "running",
        "docs": "/docs",
        "health": "/health",
        "endpoints": {
            "verify": "POST /api/verify",
            "approval": "POST /api/approval"
        }
    }
app.add_middleware(
    CORSMiddleware,
    allow_origins=TRACEFORGE_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Traceforge-Approval-Token"],
    expose_headers=["X-Traceforge-Approval-Token"],
)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_FILES = 10_000
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
INSTALL_TIMEOUT_SECONDS = int(os.getenv("TRACEFORGE_INSTALL_TIMEOUT_SECONDS", "120"))
TEST_TIMEOUT_SECONDS = int(os.getenv("TRACEFORGE_TEST_TIMEOUT_SECONDS", "120"))
PATCH_TIMEOUT_SECONDS = int(os.getenv("TRACEFORGE_PATCH_TIMEOUT_SECONDS", "30"))
DEFAULT_NPM_REGISTRY = "https://registry.npmjs.org/"
DEFAULT_PYPI_INDEX = "https://pypi.org/simple"
DEFAULT_AI_MODEL = "meta-models/muse-spark-open-1026"
GROQ_AI_REVIEW_URL = "https://api.groq.com/openai/v1/chat/completions"
AI_REVIEW_TIMEOUT_SECONDS = int(os.getenv("TRACEFORGE_AI_REVIEW_TIMEOUT_SECONDS", "30"))
PYTHON_TEST_IMAGE = os.getenv("TRACEFORGE_PYTHON_IMAGE", "traceforge-python:3.12-pytest")
NODE_TEST_IMAGE = os.getenv("TRACEFORGE_NODE_IMAGE", "traceforge-node:22")
NODE_PROXY_IMAGE = os.getenv("TRACEFORGE_NODE_PROXY_IMAGE", "traceforge-node-proxy:1")
NODE_PLACEHOLDER_TEST = 'echo "Error: no test specified" && exit 1'
SEARCH_EXCLUDED_DIRS = {"node_modules", ".git", ".next", "dist", "build", "coverage"}


@dataclass(frozen=True)
class ProjectProfile:
    language: str
    image: str
    test_command: str
    install_command: str
    project_root: Path
    relative_root: str
    reason: str = ""
    ambiguous_roots: tuple[str, ...] = ()


def _safe_zip_entry(info: zipfile.ZipInfo) -> bool:
    name = info.filename.replace("\\", "/")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\x00" in name:
        return False
    mode = (info.external_attr >> 16) & 0o170000
    return mode not in (0o120000, 0o010000)


def _extract_project(zip_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES:
            raise HTTPException(400, "The ZIP contains too many entries.")
        total_size = sum(info.file_size for info in infos)
        if total_size > MAX_UNCOMPRESSED_BYTES:
            raise HTTPException(400, "The uncompressed project is too large.")
        for info in infos:
            if not _safe_zip_entry(info):
                raise HTTPException(400, f"Unsafe ZIP entry: {info.filename}")
        archive.extractall(destination)


def _project_root(directory: Path) -> Path:
    entries = [entry for entry in directory.iterdir() if entry.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return directory


def _python_test_files(root: Path) -> list[Path]:
    matches: list[Path] = []
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if any(part in SEARCH_EXCLUDED_DIRS for part in relative.parts):
            continue
        if (
            (relative.parts[0] == "tests" and (path.name.startswith("test_") or path.name.endswith("_test.py")))
            or path.name.startswith("test_")
            or path.name.endswith("_test.py")
        ):
            matches.append(path)
    return sorted(matches)


def _python_project_root(archive_root: Path, python_files: list[Path]) -> Path:
    test_files = _python_test_files(archive_root)
    if not test_files:
        return archive_root
    paths = [path.resolve() for path in [*python_files, *test_files]]
    common = Path(os.path.commonpath([str(path) for path in paths]))
    return common if common.is_dir() else archive_root


def _bounded_files(root: Path, pattern: str) -> list[Path]:
    matches: list[Path] = []
    for current, directories, files in os.walk(root):
        directories[:] = sorted(directory for directory in directories if directory not in SEARCH_EXCLUDED_DIRS)
        if pattern in files:
            matches.append(Path(current) / pattern)
    return matches


def _relative_root(archive_root: Path, project_root: Path) -> str:
    relative = project_root.relative_to(archive_root).as_posix()
    return relative or "."


def _node_test_script(root: Path) -> str:
    package_json = root / "package.json"
    try:
        package = json.loads(package_json.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    scripts = package.get("scripts")
    test_script = scripts.get("test") if isinstance(scripts, dict) else None
    if not isinstance(test_script, str):
        return ""
    normalized = " ".join(test_script.split()).strip()
    if not normalized or normalized.lower() == NODE_PLACEHOLDER_TEST or "no test specified" in normalized.lower():
        return ""
    return normalized


def _node_install_command(root: Path) -> str:
    return "npm ci" if (root / "package-lock.json").is_file() or (root / "npm-shrinkwrap.json").is_file() else "npm install"


def _node_profiles(archive_root: Path) -> list[ProjectProfile]:
    profiles: list[ProjectProfile] = []
    for package_json in _bounded_files(archive_root, "package.json"):
        project_root = package_json.parent
        test_script = _node_test_script(project_root)
        if not test_script:
            continue
        try:
            package = json.loads(package_json.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        dependencies: dict[str, Any] = {}
        if isinstance(package, dict):
            for key in ("dependencies", "devDependencies"):
                if isinstance(package.get(key), dict):
                    dependencies.update(package[key])
        language = "Node.js / Express" if "express" in dependencies else "Node.js"
        profiles.append(ProjectProfile(
            language, NODE_TEST_IMAGE, "npm test", _node_install_command(project_root),
            project_root, _relative_root(archive_root, project_root),
        ))
    return profiles


def _detect_project(root: Path) -> ProjectProfile:
    node_profiles = _node_profiles(root)
    if len(node_profiles) == 1:
        return node_profiles[0]
    if len(node_profiles) > 1:
        return ProjectProfile(
            "Unknown", "", "", "", root, ".",
            "Multiple testable Node.js projects were found; select one explicitly.",
            tuple(profile.relative_root for profile in node_profiles),
        )
    python_files = [
        path for path in root.rglob("*.py")
        if not any(part in SEARCH_EXCLUDED_DIRS for part in path.relative_to(root).parts)
    ]
    if python_files:
        test_command = "python -m pytest -q" if _python_test_files(root) else ""
        project_root = _python_project_root(root, python_files)
        return ProjectProfile(
            "Python",
            PYTHON_TEST_IMAGE,
            test_command,
            "",
            project_root,
            _relative_root(root, project_root),
        )
    if (root / "Cargo.toml").is_file():
        return ProjectProfile("Rust", "rust:1.82-slim", "cargo test", "", root, ".")
    return ProjectProfile("Unknown", "", "", "", root, ".", "No supported project with a usable test command was found.")


def _python_requirements_path(profile: ProjectProfile) -> Path:
    return profile.project_root / "requirements.txt"


def _python_registry() -> tuple[str, set[str]]:
    index = os.getenv("TRACEFORGE_PYPI_INDEX", DEFAULT_PYPI_INDEX)
    parsed = urlparse(index)
    allowlist = {
        host.strip().lower()
        for host in os.getenv(
            "TRACEFORGE_PYPI_ALLOWLIST",
            "pypi.org,files.pythonhosted.org",
        ).split(",")
        if host.strip()
    }
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in allowlist:
        raise ValueError("TRACEFORGE_PYPI_INDEX must be an HTTPS URL on the configured package index allowlist.")
    return index, allowlist


def _normalize_registry_host(host: str) -> str:
    return host.rstrip(".").encode("idna").decode("ascii").lower()


def _validate_python_requirements(requirements: Path) -> tuple[bool, bool, str]:
    try:
        lines = requirements.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return False, False, f"Could not read requirements.txt: {exc}"
    pinned = True
    has_requirement = False
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if (
            line.startswith(("-r", "--", "-e", "-c", "-f", "-i", "git+", "hg+", "svn+", "bzr+", "http://", "https://"))
            or "://" in line
            or re.search(r"\s+@\s*", line)
        ):
            return False, False, f"Unsupported requirements option on line {line_number}."
        has_requirement = True
        if not re.match(r"^[A-Za-z0-9_.-]+(?:\[[^\]]+\])?\s*(?:==|===)\s*[^;\s]+$", line):
            pinned = False
    return True, pinned, ""


def _ensure_docker_image(image: str) -> tuple[bool, str]:
    dockerfiles = {
        PYTHON_TEST_IMAGE: "python.Dockerfile",
        NODE_TEST_IMAGE: "node.Dockerfile",
        NODE_PROXY_IMAGE: "proxy.Dockerfile",
    }
    if image not in dockerfiles:
        return True, ""
    inspect = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect.returncode == 0:
        return True, ""
    dockerfile = Path(__file__).resolve().parents[1] / "sandbox" / dockerfiles[image]
    build = subprocess.run(
        ["docker", "build", "-t", image, "-f", str(dockerfile), str(dockerfile.parent)],
        capture_output=True,
        text=True,
        timeout=TEST_TIMEOUT_SECONDS,
        check=False,
    )
    if build.returncode != 0:
        output = (build.stdout + "\n" + build.stderr).strip()[-20_000:]
        return False, f"Could not prepare Docker test image: {output}"
    return True, ""


def _run_in_docker(
    project: Path,
    image: str,
    command: str,
    install_command: str = "",
    working_directory: str = ".",
    pythonpath: str = "/workspace",
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    if not image or not command:
        return {"status": "UNKNOWN", "exit_code": None, "duration_ms": 0, "output": "No runnable tests detected."}
    if not shutil.which("docker"):
        return {"status": "UNKNOWN", "exit_code": None, "duration_ms": 0, "output": "Docker is not installed or is not on PATH."}
    try:
        image_ready, image_error = _ensure_docker_image(image)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "UNKNOWN", "exit_code": None, "duration_ms": 0, "output": f"Docker image preparation failed: {exc}"}
    if not image_ready:
        return {"status": "UNKNOWN", "exit_code": None, "duration_ms": 0, "output": image_error}
    started = time.monotonic()
    container_workdir = "/workspace" if working_directory == "." else f"/workspace/{working_directory}"
    if ".." in PurePosixPath(working_directory).parts or PurePosixPath(working_directory).is_absolute():
        return {"status": "UNKNOWN", "exit_code": None, "duration_ms": 0, "output": "Invalid project working directory."}
    docker_command = [
        "docker", "run", "--rm", "--network", "none", "--cpus", "1", "--memory", "512m",
        "--pids-limit", "128", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m",
    ]
    execution_timeout = timeout_seconds or TEST_TIMEOUT_SECONDS
    if install_command:
        install_directory = shlex.quote(container_workdir)
        docker_command.extend([
            "--env", f"PYTHONPATH={pythonpath}",
            "-v", f"{project.resolve()}:/input:ro",
            "--tmpfs", "/workspace:rw,nosuid,size=512m",
            "-w", "/workspace", image, "sh", "-lc",
            f"cp -a /input/. /workspace/ && cd {install_directory} && {install_command} && {command}",
        ])
    else:
        docker_command.extend([
            "--env", f"PYTHONPATH={pythonpath}",
            "-v", f"{project.resolve()}:/workspace:ro",
            "-w", container_workdir, image, "sh", "-lc", command,
        ])
    try:
        result = subprocess.run(
            docker_command, capture_output=True, text=True, timeout=execution_timeout,
            check=False,
        )
        output = (result.stdout + "\n" + result.stderr).strip()[-20_000:]
        return {
            "status": "VERIFIED" if result.returncode == 0 else "FAILED",
            "exit_code": result.returncode,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "output": output,
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "FAILED", "exit_code": None,
            "duration_ms": execution_timeout * 1000,
            "output": f"Test command exceeded the {execution_timeout}-second timeout.",
        }
    except OSError as exc:
        return {"status": "FAILED", "exit_code": None, "duration_ms": 0, "output": f"Docker execution failed: {exc}"}


def _run_python_preparation(
    project: Path,
    image: str,
    requirements_relative: str,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        index, allowlist = _python_registry()
    except ValueError as exc:
        return {
            "status": "FAILED",
            "package_manager": "pip",
            "registry": "",
            "requirements_file_detected": True,
            "installation_command": "",
            "exit_code": None,
            "duration_ms": 0,
            "dependencies_prepared": False,
            "errors": [str(exc)],
            "stdout": "",
            "stderr": str(exc),
            "output": str(exc),
        }
    container_requirements = f"/workspace/{requirements_relative}"
    command = (
        "python -m pip install --disable-pip-version-check --no-input --no-cache-dir "
        "--only-binary=:all: --target /workspace/.traceforge-python-packages "
        f"--index-url {shlex.quote(index)} --requirement {shlex.quote(container_requirements)}"
    )
    network = None
    proxy = None
    try:
        proxy_ready, proxy_error = _ensure_docker_image(NODE_PROXY_IMAGE)
        if not proxy_ready:
            raise RuntimeError(proxy_error)
        network, proxy = _start_node_preparation_network(allowlist)
        docker_command = [
            "docker", "run", "--rm", "--network", network, "--cpus", "1",
            "--memory", "512m", "--pids-limit", "128", "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
            "--env", f"PIP_INDEX_URL={index}",
            "--env", "PIP_NO_INPUT=1",
            "--env", "PIP_DISABLE_PIP_VERSION_CHECK=1",
            "--env", "HTTP_PROXY=http://traceforge-proxy:3128",
            "--env", "HTTPS_PROXY=http://traceforge-proxy:3128",
            "--env", "ALL_PROXY=http://traceforge-proxy:3128",
            "--env", "NO_PROXY=",
            "--env", "http_proxy=http://traceforge-proxy:3128",
            "--env", "https_proxy=http://traceforge-proxy:3128",
            "--env", "all_proxy=http://traceforge-proxy:3128",
            "--env", "no_proxy=",
            "-v", f"{project.resolve()}:/workspace:rw",
            "-w", "/workspace", image, "sh", "-lc", command,
        ]
        result = subprocess.run(
            docker_command,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT_SECONDS,
            check=False,
        )
        output = (result.stdout + "\n" + result.stderr).strip()[-20_000:]
        return {
            "status": "VERIFIED" if result.returncode == 0 else "FAILED",
            "package_manager": "pip",
            "registry": index,
            "requirements_file_detected": True,
            "installation_command": command,
            "exit_code": result.returncode,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "dependencies_prepared": result.returncode == 0,
            "errors": [] if result.returncode == 0 else [result.stderr.strip()[-2_000:] or "pip installation failed."],
            "stdout": result.stdout[-20_000:],
            "stderr": result.stderr[-20_000:],
            "output": output,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return {
            "status": "FAILED",
            "package_manager": "pip",
            "registry": index,
            "requirements_file_detected": True,
            "installation_command": command,
            "exit_code": None,
            "duration_ms": INSTALL_TIMEOUT_SECONDS * 1000,
            "dependencies_prepared": False,
            "errors": [f"Dependency installation exceeded the {INSTALL_TIMEOUT_SECONDS}-second timeout."],
            "stdout": stdout[-20_000:],
            "stderr": stderr[-20_000:],
            "output": f"{stdout}\n{stderr}".strip(),
        }
    except OSError as exc:
        return {
            "status": "FAILED",
            "package_manager": "pip",
            "registry": index,
            "requirements_file_detected": True,
            "installation_command": command,
            "exit_code": None,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "dependencies_prepared": False,
            "errors": [str(exc)],
            "stdout": "",
            "stderr": str(exc),
            "output": f"Docker execution failed: {exc}",
        }
    except (RuntimeError, subprocess.SubprocessError) as exc:
        return {
            "status": "FAILED",
            "package_manager": "pip",
            "registry": index,
            "requirements_file_detected": True,
            "installation_command": command,
            "exit_code": None,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "dependencies_prepared": False,
            "errors": [str(exc)],
            "stdout": "",
            "stderr": str(exc),
            "output": f"Dependency network preparation failed: {exc}",
        }
    finally:
        if network and proxy:
            _stop_node_preparation_network(network, proxy)


def _python_not_required() -> dict[str, Any]:
    return {
        "status": "NOT_REQUIRED",
        "package_manager": "pip",
        "registry": os.getenv("TRACEFORGE_PYPI_INDEX", DEFAULT_PYPI_INDEX),
        "requirements_file_detected": False,
        "installation_command": "",
        "exit_code": None,
        "duration_ms": 0,
        "dependencies_prepared": True,
        "errors": [],
        "stdout": "",
        "stderr": "",
    }


def _run_prepared_python_verification(
    original: Path,
    patched: Path,
    profile: ProjectProfile,
) -> tuple[dict[str, Any], dict[str, Any]]:
    requirements = _python_requirements_path(profile)
    valid, pinned, validation_error = _validate_python_requirements(requirements)
    relative_requirements = requirements.relative_to(original).as_posix()
    if not valid:
        preparation = {
            **_python_not_required(),
            "status": "FAILED",
            "requirements_file_detected": True,
            "dependencies_prepared": False,
            "errors": [validation_error],
        }
        unknown = {
            "status": "UNKNOWN",
            "exit_code": None,
            "duration_ms": 0,
            "output": "Tests were not started because dependency preparation failed.",
        }
        return (
            {"status": "UNKNOWN", "exit_code": None, "duration_ms": 0, "output": validation_error, "installation": preparation, "dependency_preparation": preparation, "tests": unknown},
            {"status": "UNKNOWN", "exit_code": None, "duration_ms": 0, "output": validation_error, "installation": preparation, "dependency_preparation": preparation, "tests": unknown},
        )
    with tempfile.TemporaryDirectory(prefix="traceforge-python-prepare-") as execution:
        prepared = Path(execution) / "prepared"
        shutil.copytree(original, prepared, symlinks=False)
        preparation = _run_python_preparation(prepared, profile.image, relative_requirements)
        preparation["reproducible"] = pinned
        preparation["requirements_pinned"] = pinned
        if preparation["status"] != "VERIFIED":
            unknown = {
                "status": "UNKNOWN",
                "exit_code": None,
                "duration_ms": 0,
                "output": "Tests were not started because dependency preparation failed.",
            }
            return (
                {"status": "UNKNOWN", "exit_code": None, "duration_ms": 0, "output": preparation["output"], "installation": preparation, "dependency_preparation": preparation, "tests": unknown},
                {"status": "UNKNOWN", "exit_code": None, "duration_ms": 0, "output": preparation["output"], "installation": preparation, "dependency_preparation": preparation, "tests": unknown},
            )
        baseline_root = Path(execution) / "baseline"
        patched_root = Path(execution) / "patched"
        shutil.copytree(prepared, baseline_root, symlinks=False)
        shutil.copytree(patched, patched_root, symlinks=False)
        dependency_source = prepared / ".traceforge-python-packages"
        for target_root in (baseline_root, patched_root):
            target = target_root / ".traceforge-python-packages"
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(dependency_source, target, symlinks=False)
        pythonpath = "/workspace/.traceforge-python-packages:/workspace"
        baseline_tests = _run_in_docker(
            baseline_root, profile.image, profile.test_command, "", profile.relative_root, pythonpath,
        )
        patched_tests = _run_in_docker(
            patched_root, profile.image, profile.test_command, "", profile.relative_root, pythonpath,
        )
        return (
            {**baseline_tests, "installation": preparation, "dependency_preparation": preparation, "tests": baseline_tests},
            {**patched_tests, "installation": preparation, "dependency_preparation": preparation, "tests": patched_tests},
        )


def _node_docker_environment() -> list[str]:
    registry = os.getenv("TRACEFORGE_NPM_REGISTRY", DEFAULT_NPM_REGISTRY)
    parsed = urlparse(registry)
    allowlist = {
        host.strip().lower()
        for host in os.getenv("TRACEFORGE_NPM_REGISTRY_ALLOWLIST", "registry.npmjs.org").split(",")
        if host.strip()
    }
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in allowlist:
        raise ValueError("TRACEFORGE_NPM_REGISTRY must be an HTTPS URL on the configured registry allowlist.")
    return [
        "--env", "HOME=/tmp/npm-home",
        "--env", "NPM_CONFIG_CACHE=/tmp/npm-cache",
        "--env", "NPM_CONFIG_LOGS_DIR=/tmp/npm-logs",
        "--env", f"NPM_CONFIG_REGISTRY={registry}",
        "--env", "NPM_CONFIG_OFFLINE=true",
        "--env", "NPM_CONFIG_AUDIT=false",
        "--env", "NPM_CONFIG_FUND=false",
        "--env", "NPM_CONFIG_IGNORE_SCRIPTS=true",
        "--env", "NPM_CONFIG_INSTALL_LINKS=true",
    ]


def _node_preparation_environment() -> list[str]:
    environment = _node_docker_environment()
    return [
        "NPM_CONFIG_OFFLINE=false" if value == "NPM_CONFIG_OFFLINE=true" else value
        for value in environment
    ]


def _validate_node_lockfile(project: Path, registry: str) -> tuple[bool, str]:
    lockfile = project / "package-lock.json"
    if not lockfile.is_file():
        try:
            package = json.loads((project / "package.json").read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            package = {}
        dependencies = package.get("dependencies", {}) if isinstance(package, dict) else {}
        dev_dependencies = package.get("devDependencies", {}) if isinstance(package, dict) else {}
        if dependencies or dev_dependencies:
            return False, "A package-lock.json with integrity data is required for external dependencies."
        return True, ""
    try:
        lock = json.loads(lockfile.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, f"Invalid package-lock.json: {exc}"
    if not isinstance(lock, dict) or not isinstance(lock.get("lockfileVersion"), int):
        return False, "Invalid package-lock.json: lockfileVersion is missing."
    packages = lock.get("packages")
    if packages is not None and not isinstance(packages, dict):
        return False, "Invalid package-lock.json: packages must be an object."
    registry_host = urlparse(registry).hostname
    try:
        registry_host = _normalize_registry_host(registry_host or "")
    except UnicodeError:
        return False, "TRACEFORGE_NPM_REGISTRY has an invalid hostname."
    for package_path, metadata in (packages or {}).items():
        if not package_path or package_path == "" or not isinstance(metadata, dict):
            continue
        resolved = metadata.get("resolved")
        if not resolved:
            continue
        parsed = urlparse(resolved)
        try:
            resolved_host = _normalize_registry_host(parsed.hostname or "")
            resolved_port = parsed.port
        except UnicodeError:
            return False, f"package-lock.json contains a malformed resolved URL for {package_path}."
        except ValueError:
            return False, f"package-lock.json contains a malformed resolved URL for {package_path}."
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or resolved_port not in (None, 443)
            or resolved_host != registry_host
        ):
            return False, f"package-lock.json contains an unapproved resolved URL for {package_path}."
        if not isinstance(metadata.get("integrity"), str) or not metadata["integrity"].strip():
            return False, f"package-lock.json is missing integrity data for {package_path}."
    return True, ""


def _npm_diagnostic_script() -> str:
    return r"""
printf '\n__TRACEFORGE_NPM_DIAGNOSTICS__\n'
printf 'node_version='; node --version
printf 'npm_version='; npm --version
printf 'package_manager=npm\n'
printf 'cache='; npm config get cache
printf 'logs='; npm config get logs-dir
printf 'registry='; npm config get registry
printf 'offline='; npm config get offline
printf 'ignore_scripts='; npm config get ignore-scripts
for file in package.json package-lock.json npm-shrinkwrap.json; do
  if [ -f "$file" ]; then
    size=$(wc -c < "$file")
    printf 'file=%s size=%s\n' "$file" "$size"
  else
    printf 'file=%s missing\n' "$file"
  fi
done
for log in /tmp/npm-logs/*-debug-0.log; do
  if [ -f "$log" ]; then
    if grep -q 'verbose title npm install\|verbose title npm ci' "$log"; then
      printf '\n__TRACEFORGE_NPM_LOG__ %s\n' "$log"
      tail -n 150 "$log"
    fi
  fi
done
"""


def _extract_debug_summary(output: str) -> str:
    marker = "__TRACEFORGE_NPM_DIAGNOSTICS__"
    return output[output.find(marker):].strip() if marker in output else ""


def _extract_npm_version(output: str) -> str:
    match = re.search(r"npm_version=([^\s]+)", output)
    return match.group(1) if match else ""


def _start_node_preparation_network(approved_hosts: set[str] | None = None) -> tuple[str, str]:
    suffix = f"{os.getpid()}-{time.monotonic_ns()}"
    network = f"traceforge-node-{suffix}"
    proxy = f"traceforge-node-proxy-{suffix}"
    created = subprocess.run(
        ["docker", "network", "create", "--internal", network],
        capture_output=True, text=True, check=False,
    )
    if created.returncode != 0:
        raise RuntimeError((created.stderr or created.stdout).strip() or "Could not create the internal preparation network.")
    started = subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", proxy,
            "--network", "bridge",
            "--env", (
                "TRACEFORGE_PROXY_HOSTS="
                + ",".join(sorted(approved_hosts or {
                    urlparse(os.getenv("TRACEFORGE_NPM_REGISTRY", DEFAULT_NPM_REGISTRY)).hostname or "",
                }))
            ),
            NODE_PROXY_IMAGE,
        ],
        capture_output=True, text=True, check=False,
    )
    if started.returncode != 0:
        subprocess.run(["docker", "network", "rm", network], capture_output=True, text=True, check=False)
        raise RuntimeError((started.stderr or started.stdout).strip() or "Could not start the registry proxy.")
    connected = subprocess.run(
        ["docker", "network", "connect", "--alias", "traceforge-proxy", network, proxy],
        capture_output=True, text=True, check=False,
    )
    if connected.returncode != 0:
        subprocess.run(["docker", "rm", "-f", proxy], capture_output=True, text=True, check=False)
        subprocess.run(["docker", "network", "rm", network], capture_output=True, text=True, check=False)
        raise RuntimeError((connected.stderr or connected.stdout).strip() or "Could not connect the registry proxy.")
    return network, proxy


def _stop_node_preparation_network(network: str, proxy: str) -> None:
    for command in (
        ["docker", "rm", "-f", proxy],
        ["docker", "network", "rm", network],
    ):
        try:
            subprocess.run(command, capture_output=True, text=True, check=False)
        except (OSError, subprocess.SubprocessError):
            continue


def _run_node_phase(
    project: Path,
    image: str,
    command: str,
    working_directory: str,
    timeout_seconds: int,
    writable: bool,
    network: str = "none",
    preparation: bool = False,
) -> dict[str, Any]:
    started = time.monotonic()
    container_workdir = "/workspace" if working_directory == "." else f"/workspace/{working_directory}"
    try:
        npm_environment = _node_preparation_environment() if preparation else _node_docker_environment()
    except ValueError as exc:
        return {
            "status": "FAILED" if preparation else "UNKNOWN", "exit_code": None, "duration_ms": 0,
            "stdout": "", "stderr": str(exc), "output": str(exc),
            "debug_log_summary": "", "failure_reason": str(exc),
        }
    controlled_network = None
    proxy_container = None
    if preparation:
        try:
            proxy_ready, proxy_error = _ensure_docker_image(NODE_PROXY_IMAGE)
            if not proxy_ready:
                raise RuntimeError(proxy_error)
            controlled_network, proxy_container = _start_node_preparation_network()
            network = controlled_network
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return {
                "status": "FAILED", "exit_code": None, "duration_ms": 0,
                "stdout": "", "stderr": str(exc), "output": str(exc),
                "debug_log_summary": "", "failure_reason": str(exc),
            }
    docker_command = [
        "docker", "run", "--rm", "--network", network, "--cpus", "1", "--memory", "512m",
        "--pids-limit", "128", "--read-only", "--tmpfs", "/tmp:rw,nosuid,size=256m",
        *npm_environment,
        *(["--env", "HTTP_PROXY=http://traceforge-proxy:3128",
           "--env", "HTTPS_PROXY=http://traceforge-proxy:3128",
           "--env", "ALL_PROXY=http://traceforge-proxy:3128",
           "--env", "NO_PROXY=",
           "--env", "http_proxy=http://traceforge-proxy:3128",
           "--env", "https_proxy=http://traceforge-proxy:3128",
           "--env", "all_proxy=http://traceforge-proxy:3128",
           "--env", "no_proxy=",
           "--env", "NPM_CONFIG_PROXY=http://traceforge-proxy:3128",
           "--env", "NPM_CONFIG_HTTPS_PROXY=http://traceforge-proxy:3128"] if preparation else []),
        "-v", f"{project.resolve()}:/workspace:{'rw' if writable else 'ro'}",
        "-w", container_workdir, image, "sh", "-lc", command,
    ]
    try:
        result = subprocess.run(
            docker_command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        combined = result.stdout + "\n" + result.stderr
        return {
            "status": "VERIFIED" if result.returncode == 0 else "FAILED",
            "exit_code": result.returncode,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "stdout": result.stdout[-20_000:],
            "stderr": result.stderr[-20_000:],
            "output": combined.strip()[-20_000:],
            "debug_log_summary": _extract_debug_summary(combined),
            "failure_reason": "" if result.returncode == 0 else (
                result.stderr.strip()[-2_000:] or "npm command exited with a non-zero status."
            ),
        }
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "")[-20_000:] if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "")[-20_000:] if isinstance(exc.stderr, str) else ""
        combined = f"{stdout}\n{stderr}"
        return {
            "status": "TIMEOUT",
            "exit_code": None,
            "duration_ms": timeout_seconds * 1000,
            "stdout": stdout,
            "stderr": stderr,
            "output": combined.strip(),
            "debug_log_summary": _extract_debug_summary(combined),
            "failure_reason": f"npm command exceeded the {timeout_seconds}-second timeout.",
        }
    except OSError as exc:
        return {
            "status": "UNKNOWN",
            "exit_code": None,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "stdout": "",
            "stderr": str(exc),
            "output": f"Docker execution failed: {exc}",
            "debug_log_summary": "",
            "failure_reason": str(exc),
        }
    finally:
        if proxy_container and controlled_network:
            _stop_node_preparation_network(controlled_network, proxy_container)


def _run_node_in_docker(
    project: Path,
    image: str,
    test_command: str,
    install_command: str,
    working_directory: str,
) -> dict[str, Any]:
    if not shutil.which("docker"):
        unknown = {"status": "UNKNOWN", "exit_code": None, "duration_ms": 0, "stdout": "", "stderr": "", "output": "Docker is not installed or is not on PATH."}
        return {"installation": unknown, "tests": unknown, **unknown}
    try:
        image_ready, image_error = _ensure_docker_image(image)
    except (OSError, subprocess.SubprocessError) as exc:
        image_ready, image_error = False, f"Docker image preparation failed: {exc}"
    if not image_ready:
        unknown = {"status": "UNKNOWN", "exit_code": None, "duration_ms": 0, "stdout": "", "stderr": "", "output": image_error}
        return {"installation": unknown, "tests": unknown, **unknown}
    with tempfile.TemporaryDirectory(prefix="traceforge-node-") as execution:
        execution_root = Path(execution) / "project"
        shutil.copytree(project, execution_root, symlinks=False)
        install_script = (
            f"{install_command} --no-audit --no-fund --ignore-scripts --install-links; "
            f"install_exit=$?; {_npm_diagnostic_script()}\nexit $install_exit"
        )
        install = _run_node_phase(
            execution_root,
            image,
            install_script,
            working_directory,
            INSTALL_TIMEOUT_SECONDS,
            writable=True,
            network="bridge",
            preparation=True,
        )
        if install["status"] not in {"VERIFIED"}:
            unknown_tests = {
                "status": "UNKNOWN", "exit_code": None, "duration_ms": 0,
                "stdout": "", "stderr": "",
                "output": "Tests were not started because dependency installation did not complete.",
                "debug_log_summary": "",
                "failure_reason": "Dependency installation did not complete.",
            }
            return {
                "status": "UNKNOWN",
                "exit_code": None,
                "duration_ms": install["duration_ms"],
                "stdout": install["stdout"],
                "stderr": install["stderr"],
                "output": install["output"],
                "installation": install,
                "tests": unknown_tests,
            }
        tests = _run_node_phase(
            execution_root, image, test_command, working_directory, TEST_TIMEOUT_SECONDS, writable=False,
        )
        return {
            "status": tests["status"],
            "exit_code": tests["exit_code"],
            "duration_ms": tests["duration_ms"],
            "stdout": tests["stdout"],
            "stderr": tests["stderr"],
            "output": tests["output"],
            "installation": install,
            "tests": tests,
        }


def _node_unknown_tests(reason: str) -> dict[str, Any]:
    return {
        "status": "UNKNOWN",
        "exit_code": None,
        "duration_ms": 0,
        "stdout": "",
        "stderr": "",
        "output": reason,
        "debug_log_summary": "",
        "failure_reason": reason,
    }


def _run_prepared_node_verification(
    original: Path,
    patched: Path,
    image: str,
    test_command: str,
    install_command: str,
    working_directory: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    registry = os.getenv("TRACEFORGE_NPM_REGISTRY", DEFAULT_NPM_REGISTRY)
    lock_valid, lock_error = _validate_node_lockfile(
        original / working_directory if working_directory != "." else original,
        registry,
    )
    lockfile = (original / working_directory if working_directory != "." else original) / "package-lock.json"
    preparation_metadata = {
        "registry": registry,
        "package_manager": "npm",
        "lockfile_detected": lockfile.is_file(),
        "lockfile_validated": lock_valid,
        "lifecycle_scripts_disabled": True,
        "dependencies_prepared": False,
        "package_manager_version": "",
        "errors": [lock_error] if not lock_valid else [],
    }
    if not lock_valid:
        preparation = {
            "status": "FAILED",
            "exit_code": None,
            "duration_ms": 0,
            "stdout": "",
            "stderr": lock_error,
            "output": lock_error,
            "failure_reason": lock_error,
            **preparation_metadata,
        }
        unknown = _node_unknown_tests("Tests were not started because dependency preparation failed.")
        return {**unknown, "installation": preparation, "dependency_preparation": preparation, "tests": unknown}, {**unknown, "installation": preparation, "dependency_preparation": preparation, "tests": unknown}

    with tempfile.TemporaryDirectory(prefix="traceforge-node-prepare-") as execution:
        prepared = Path(execution) / "prepared"
        shutil.copytree(original, prepared, symlinks=False)
        install_script = (
            f"{install_command} --no-audit --no-fund --ignore-scripts --install-links; "
            f"install_exit=$?; {_npm_diagnostic_script()}\nexit $install_exit"
        )
        preparation = _run_node_phase(
            prepared,
            image,
            install_script,
            working_directory,
            INSTALL_TIMEOUT_SECONDS,
            writable=True,
            network="bridge",
            preparation=True,
        )
        preparation.update(preparation_metadata)
        preparation["package_manager_version"] = _extract_npm_version(preparation.get("output", ""))
        preparation["lockfile_validated"] = True
        preparation["dependencies_prepared"] = preparation["status"] == "VERIFIED"
        preparation["errors"] = [] if preparation["status"] == "VERIFIED" else [preparation.get("failure_reason", "Dependency preparation failed.")]
        if preparation["status"] != "VERIFIED":
            unknown = _node_unknown_tests("Tests were not started because dependency preparation failed.")
            return {**unknown, "installation": preparation, "dependency_preparation": preparation, "tests": unknown}, {**unknown, "installation": preparation, "dependency_preparation": preparation, "tests": unknown}

        baseline_root = Path(execution) / "baseline"
        patched_root = Path(execution) / "patched"
        shutil.copytree(prepared, baseline_root, symlinks=False)
        shutil.copytree(patched, patched_root, symlinks=False)
        dependency_source = prepared / working_directory / "node_modules" if working_directory != "." else prepared / "node_modules"
        dependency_targets = [
            baseline_root / working_directory / "node_modules" if working_directory != "." else baseline_root / "node_modules",
            patched_root / working_directory / "node_modules" if working_directory != "." else patched_root / "node_modules",
        ]
        for dependency_target in dependency_targets:
            if dependency_target.exists():
                shutil.rmtree(dependency_target)
            if dependency_source.is_dir():
                shutil.copytree(dependency_source, dependency_target, symlinks=False)
            else:
                dependency_target.mkdir(parents=True, exist_ok=True)
        baseline_tests = _run_node_phase(
            baseline_root, image, test_command, working_directory, TEST_TIMEOUT_SECONDS, writable=False,
        )
        patched_tests = _run_node_phase(
            patched_root, image, test_command, working_directory, TEST_TIMEOUT_SECONDS, writable=False,
        )
        baseline_result = {
            **baseline_tests,
            "installation": preparation,
            "dependency_preparation": preparation,
            "tests": baseline_tests,
        }
        patched_result = {
            **patched_tests,
            "installation": preparation,
            "dependency_preparation": preparation,
            "tests": patched_tests,
        }
        return baseline_result, patched_result


def _normalize_patch(patch_data: bytes) -> bytes:
    text = patch_data.decode("utf-8-sig")
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _patch_path_from_header(value: str) -> str:
    path = value.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return path
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    return path


def _validate_patch_targets(patch_data: bytes, archive_root: Path) -> tuple[bool, str]:
    try:
        lines = patch_data.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        return False, f"Patch is not valid UTF-8: {exc}"
    headers = [line[4:] for line in lines if line.startswith("--- ") or line.startswith("+++ ")]
    for raw_path in headers:
        target = _patch_path_from_header(raw_path)
        if target == "/dev/null":
            continue
        pure = PurePosixPath(target.replace("\\", "/"))
        if pure.is_absolute() or ".." in pure.parts or "\x00" in target:
            return False, f"Patch target is unsafe: {raw_path.strip()}"
        resolved = (archive_root / Path(*pure.parts)).resolve()
        if archive_root.resolve() not in resolved.parents and resolved != archive_root.resolve():
            return False, f"Patch target escapes the archive root: {target}"
        if not resolved.is_file():
            return False, f"Patch target not found: {target} (expected at {pure.as_posix()})"
    return True, ""


def _apply_patch(patch: Path, project: Path) -> tuple[bool, str]:
    try:
        check = subprocess.run(
            ["git", "apply", "--check", str(patch)],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=PATCH_TIMEOUT_SECONDS,
        )
        if check.returncode != 0:
            return False, (check.stderr or check.stdout).strip()
        applied = subprocess.run(
            ["git", "apply", str(patch)],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=PATCH_TIMEOUT_SECONDS,
        )
        return applied.returncode == 0, (applied.stderr or applied.stdout).strip()
    except subprocess.TimeoutExpired:
        return False, f"Patch application exceeded the {PATCH_TIMEOUT_SECONDS}-second timeout."


def _verification_outcome(patch_applied: bool, patched_status: str) -> str:
    if not patch_applied:
        return "PATCH_APPLICATION_FAILED"
    if patched_status in {"FAILED", "TIMEOUT"}:
        return "TESTS_FAILED"
    if patched_status == "VERIFIED":
        return "TESTS_PASSED"
    return "UNKNOWN"


def _test_counts(result: dict[str, Any]) -> dict[str, int | None]:
    output = str(result.get("output", ""))
    patterns = (
        (r"(\d+)\s+passed", "passed"),
        (r"(\d+)\s+failed", "failed"),
        (r"(\d+)\s+skipped", "skipped"),
        (r"(\d+)\s+tests?\s+passed", "passed"),
        (r"(\d+)\s+tests?\s+failed", "failed"),
        (r"#\s+pass\s+(\d+)", "passed"),
        (r"#\s+fail\s+(\d+)", "failed"),
        (r"#\s+skipped\s+(\d+)", "skipped"),
        (r"(\d+)\s+passing", "passed"),
        (r"(\d+)\s+failing", "failed"),
    )
    counts: dict[str, int | None] = {
        "passed": None,
        "failed": None,
        "skipped": None,
        "total": None,
    }
    for pattern, key in patterns:
        match = re.search(pattern, output, re.IGNORECASE)
        if match:
            counts[key] = int(match.group(1))
    known = [counts[key] for key in ("passed", "failed", "skipped") if counts[key] is not None]
    if known:
        counts["total"] = sum(known)
    return counts


def _passport_test_evidence(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result.get("status", "UNKNOWN"),
        "counts": _test_counts(result),
        "exit_code": result.get("exit_code"),
        "duration_ms": result.get("duration_ms", 0),
        "output": str(result.get("output", ""))[-20_000:],
        "installation": result.get("installation"),
        "dependency_preparation": result.get("dependency_preparation"),
    }


def _tool_versions(
    profile: ProjectProfile,
    baseline: dict[str, Any],
    patched: dict[str, Any],
) -> dict[str, str]:
    versions: dict[str, str] = {"traceforge": app.version}
    for result in (baseline, patched):
        installation = result.get("installation") or {}
        package_manager_version = installation.get("package_manager_version")
        if package_manager_version:
            versions["package_manager"] = str(package_manager_version)
        diagnostic = installation.get("debug_log_summary", "")
        for key, label in (("node_version=", "node"), ("python_version=", "python")):
            match = re.search(rf"{re.escape(key)}([^\s]+)", str(diagnostic))
            if match:
                versions[label] = match.group(1)
    if profile.image:
        versions["sandbox_image"] = profile.image
    return versions


def _patch_review_context(patch_data: bytes, changed_files: list[str]) -> str:
    text = patch_data.decode("utf-8", errors="replace")
    return text[:30_000] + ("\n[patch context truncated]" if len(text) > 30_000 else "")


def _validate_ai_review(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("AI response must be a JSON object.")
    findings = value.get("findings", [])
    recommendations = value.get("recommendations", [])
    if not isinstance(findings, list) or not isinstance(recommendations, list):
        raise ValueError("AI response findings and recommendations must be arrays.")
    validated_findings = []
    for finding in findings[:30]:
        if not isinstance(finding, dict):
            raise ValueError("AI finding must be an object.")
        validated_findings.append({
            "status": "SUGGESTED",
            "severity": str(finding.get("severity", "UNKNOWN"))[:32],
            "title": str(finding.get("title", ""))[:300],
            "explanation": str(finding.get("explanation", ""))[:2_000],
            "files": [str(item)[:300] for item in finding.get("files", [])[:20]]
            if isinstance(finding.get("files", []), list) else [],
            "confidence": str(finding.get("confidence", "UNKNOWN"))[:32],
        })
    validated_recommendations = [
        {"status": "SUGGESTED", "text": str(item)[:1_000]}
        for item in recommendations[:30]
        if isinstance(item, str)
    ]
    return {
        "status": "SUGGESTED",
        "findings": validated_findings,
        "recommendations": validated_recommendations,
        "limitations": [
            "AI suggestions are not execution evidence.",
            "Passing tests does not prove complete correctness.",
        ],
    }



def _run_ai_review(
    patch_data: bytes,
    changed_files: list[str],
    profile: ProjectProfile,
    baseline: dict[str, Any],
    patched: dict[str, Any],
    patch_applied: bool,
) -> dict[str, Any]:
    provider = os.getenv("TRACEFORGE_AI_REVIEW_PROVIDER", "meta").strip().lower()

    if provider not in {"meta", "groq", "baseten", "modal"}:
        return {
            "status": "UNKNOWN",
            "provider": provider,
            "findings": [],
            "recommendations": [],
            "message": "AI review unavailable: unsupported provider.",
        }

    if provider == "meta":
        endpoint = "https://api.meta.ai/v1/chat/completions"
        api_key = os.getenv("MODEL_API_KEY", "").strip()
        model = "muse-spark-1.3"
    else:
        endpoint = (
            GROQ_AI_REVIEW_URL
            if provider == "groq"
            else os.getenv("TRACEFORGE_AI_REVIEW_URL", "").strip()
        )
        api_key = os.getenv("TRACEFORGE_AI_REVIEW_API_KEY", "").strip()
        model = os.getenv("TRACEFORGE_AI_REVIEW_MODEL", "").strip()

        if provider == "baseten" and not model:
            model = DEFAULT_AI_MODEL

    missing = []

    if not api_key:
        missing.append(
            "MODEL_API_KEY" if provider == "meta"
            else "TRACEFORGE_AI_REVIEW_API_KEY"
        )

    if provider in {"baseten", "modal"} and not endpoint:
        missing.append("TRACEFORGE_AI_REVIEW_URL")

    if provider in {"groq", "modal"} and not model:
        missing.append("TRACEFORGE_AI_REVIEW_MODEL")

    if missing:
        return {
            "status": "UNKNOWN",
            "provider": provider,
            "findings": [],
            "recommendations": [],
            "message": f"AI review unavailable: configure {', '.join(missing)}.",
        }
    if not endpoint.lower().startswith("https://"):
        return {
            "status": "UNKNOWN",
            "provider": provider,
            "findings": [],
            "recommendations": [],
            "message": "AI review unavailable: TRACEFORGE_AI_REVIEW_URL must use HTTPS.",
        }

    evidence = {
        "language": profile.language,
        "project_root": profile.relative_root,
        "test_command": profile.test_command,
        "patch_applied": patch_applied,
        "baseline": {
            "status": baseline.get("status", "UNKNOWN"),
            "exit_code": baseline.get("exit_code"),
            "duration_ms": baseline.get("duration_ms", 0),
        },
        "patched": {
            "status": patched.get("status", "UNKNOWN"),
            "exit_code": patched.get("exit_code"),
            "duration_ms": patched.get("duration_ms", 0),
        },
    }

    prompt = (
        "Review this code patch as an independent advisory reviewer. "
        "Identify likely bugs, regressions, security concerns, affected behavior, "
        "and missing tests. Compare the patch with the measured evidence. "
        "Never claim tests or correctness are verified. Return only JSON with this shape: "
        '{"findings":[{"severity":"LOW|MEDIUM|HIGH|CRITICAL","title":"",'
        '"explanation":"","files":[],"confidence":"LOW|MEDIUM|HIGH"}],'
        '"recommendations":[""]}. '
        f"Changed files: {json.dumps(changed_files)}\n"
        f"Measured evidence: {json.dumps(evidence)}\n"
        f"Patch:\n{_patch_review_context(patch_data, changed_files)}"
    )

    request_body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }).encode("utf-8")

    request = urllib.request.Request(
        endpoint,
        data=request_body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "TRACEFORGE/2.0 (AI Patch Review)",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=AI_REVIEW_TIMEOUT_SECONDS,
        ) as response:
            response_data = json.loads(
                response.read(1_000_000).decode("utf-8")
            )

        content = (
            response_data.get("choices", [{}])[0]
            .get("message", {})
            .get("content")
        )

        if isinstance(content, str):
            content = json.loads(content)

        review = _validate_ai_review(content)
        review["provider"] = provider
        return review

    except urllib.error.HTTPError as exc:
        try:
            provider_error = exc.read(16_384).decode(
                "utf-8",
                errors="replace",
            )
        except OSError:
            provider_error = ""

        sanitized_error = re.sub(
            r"(?i)(authorization|api[_-]?key|token)\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            provider_error,
        )[:1_000]

        logger.warning(
            "AI review provider=%s model=%s http_status=%s error=%s",
            provider,
            model,
            exc.code,
            sanitized_error or "provider returned an HTTP error",
        )

        return {
            "status": "UNKNOWN",
            "provider": provider,
            "findings": [],
            "recommendations": [],
            "message": f"AI review unavailable: provider returned HTTP {exc.code}.",
        }

    except (
        OSError,
        urllib.error.URLError,
        json.JSONDecodeError,
        ValueError,
        KeyError,
        IndexError,
    ) as exc:
        logger.warning(
            "AI review provider=%s model=%s error=%s",
            provider,
            model,
            str(exc)[:500],
        )

        return {
            "status": "UNKNOWN",
            "provider": provider,
            "findings": [],
            "recommendations": [],
            "message": f"AI review unavailable: {str(exc)[:500]}",
        }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/verify")
async def verify(project_zip: UploadFile = File(...), patch_file: UploadFile = File(...)) -> JSONResponse:
    if not project_zip.filename or not project_zip.filename.lower().endswith(".zip"):
        raise HTTPException(400, "Upload a .zip project archive.")
    if not patch_file.filename or not patch_file.filename.lower().endswith((".patch", ".diff")):
        raise HTTPException(400, "Upload a .patch or .diff file.")
    zip_data = await project_zip.read(MAX_UPLOAD_BYTES + 1)
    patch_data = await patch_file.read(MAX_UPLOAD_BYTES + 1)
    if len(zip_data) > MAX_UPLOAD_BYTES or len(patch_data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Each upload must be 50 MB or smaller.")
    if not zip_data or not patch_data:
        raise HTTPException(400, "Both uploads must contain data.")

    with tempfile.TemporaryDirectory(prefix="traceforge-") as work:
        workspace = Path(work)
        zip_path = workspace / "project.zip"
        patch_path = workspace / "change.patch"
        zip_path.write_bytes(zip_data)
        try:
            normalized_patch = _normalize_patch(patch_data)
        except UnicodeDecodeError as exc:
            normalized_patch = b""
            patch_decode_error = f"Patch is not valid UTF-8: {exc}"
        else:
            patch_decode_error = ""
        patch_path.write_bytes(normalized_patch)
        baseline_parent = workspace / "baseline"
        patched_parent = workspace / "patched"
        baseline_parent.mkdir()
        patched_parent.mkdir()
        _extract_project(zip_path, baseline_parent)
        archive_root = baseline_parent
        shutil.copytree(archive_root, patched_parent, dirs_exist_ok=True, symlinks=False)
        profile = _detect_project(archive_root)
        python_requirements = (
            profile.image == PYTHON_TEST_IMAGE
            and _python_requirements_path(profile).is_file()
        )
        if profile.test_command and profile.image != NODE_TEST_IMAGE and not python_requirements:
            baseline = (
                _run_in_docker(
                    archive_root, profile.image, profile.test_command,
                    profile.install_command, profile.relative_root,
                )
            )
        else:
            baseline = {
                "status": "UNKNOWN", "exit_code": None, "duration_ms": 0,
                "output": profile.reason or "No runnable tests detected.",
            }
        targets_valid, target_error = (
            (False, patch_decode_error)
            if patch_decode_error
            else _validate_patch_targets(normalized_patch, archive_root)
        )
        patch_ok, patch_message = (
            (False, target_error)
            if not targets_valid
            else _apply_patch(patch_path, patched_parent)
        )
        patched = (
            _run_in_docker(
                patched_parent, profile.image, profile.test_command,
                profile.install_command, profile.relative_root,
            )
            if patch_ok and profile.test_command and profile.image != NODE_TEST_IMAGE
            else ({
            "status": "UNKNOWN", "exit_code": None, "duration_ms": 0,
            "output": profile.reason or "No runnable tests detected.",
            } if patch_ok else {
            "status": "UNKNOWN", "exit_code": None, "duration_ms": 0,
            "output": patch_message or "Patch could not be applied.",
            })
        )
        if profile.image == PYTHON_TEST_IMAGE and profile.test_command and python_requirements and patch_ok:
            baseline, patched = _run_prepared_python_verification(
                archive_root,
                patched_parent,
                profile,
            )
        if profile.image == NODE_TEST_IMAGE and profile.test_command:
            if patch_ok:
                baseline, patched = _run_prepared_node_verification(
                    archive_root,
                    patched_parent,
                    profile.image,
                    profile.test_command,
                    profile.install_command,
                    profile.relative_root,
                )
            else:
                baseline = _run_node_in_docker(
                    archive_root, profile.image, profile.test_command,
                    profile.install_command, profile.relative_root,
                )
                patched = {
                    "status": "UNKNOWN",
                    "exit_code": None,
                    "duration_ms": 0,
                    "output": patch_message or "Patch could not be applied.",
                }
        changed_files = sorted(
            _patch_path_from_header(line[4:])
            for line in patch_data.decode("utf-8", errors="replace").splitlines()
            if line.startswith("+++ ") and _patch_path_from_header(line[4:]) != "/dev/null"
        )
        baseline_passed = baseline["status"] == "VERIFIED"
        patched_passed = patched["status"] == "VERIFIED"
        outcome = (
            "UNKNOWN"
            if profile.ambiguous_roots
            else _verification_outcome(patch_ok, patched["status"])
        )
        verification_id = str(uuid.uuid4())
        approval_token = secrets.token_urlsafe(32)
        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ai_review = _run_ai_review(
            normalized_patch,
            changed_files,
            profile,
            baseline,
            patched,
            patch_ok,
        )
        passport = {
            "schema_version": "2.1",
            "product": "TRACEFORGE",
            "verification_id": verification_id,
            "created_at": created_at,
            "tool_versions": _tool_versions(profile, baseline, patched),
            "verification": {
                "status": outcome,
                "tests_passed": patched_passed,
                "complete_correctness": "UNKNOWN",
            },
            "project": {
                "language": profile.language,
                "root": profile.relative_root,
                "test_command": profile.test_command,
                "install_command": profile.install_command or None,
                "detection_reason": profile.reason or None,
                "ambiguous_roots": list(profile.ambiguous_roots),
            },
            "patch": {
                "filename": patch_file.filename,
                "applied": patch_ok,
                "status": "VERIFIED" if patch_ok else "FAILED",
                "message": patch_message,
                "changed_files": changed_files,
            },
            "baseline": _passport_test_evidence(baseline),
            "patched": _passport_test_evidence(patched),
            "installation": {
                "baseline": baseline.get("installation"),
                "patched": patched.get("installation"),
            },
            "dependency_preparation": (
                baseline.get("dependency_preparation")
                or baseline.get("installation")
                or (_python_not_required() if profile.image == PYTHON_TEST_IMAGE else None)
            ),
            "regressions": {
                "status": "VERIFIED" if patch_ok and baseline_passed and not patched_passed else "UNKNOWN",
                "detected": patch_ok and baseline_passed and not patched_passed,
            },
            "ai_review": ai_review,
            "recommendations": ai_review.get("recommendations") or [
                {"status": "SUGGESTED", "text": "Review the technical evidence and add focused tests for the changed behavior."}
            ],
            "limitations": ["Passing tests does not prove complete correctness.", "Static analysis and AI review are not enabled.", "This MVP uses temporary local storage and is not production-hardened."],
            "human_approval": {
                "status": "PENDING",
                "verification_id": verification_id,
                "decision": "PENDING",
                "decided_at": None,
                "ai_review_status": ai_review.get("status", "UNKNOWN"),
            },
        }
        _store_verification(
            passport,
            approval_token,
            time.time() + APPROVAL_TOKEN_TTL_SECONDS,
        )
        return JSONResponse(
            passport,
            headers={"X-Traceforge-Approval-Token": approval_token},
        )


@app.get("/api/verification/{verification_id}")
async def verification_passport(
    verification_id: str,
    token: str = Header(default="", alias="X-Traceforge-Approval-Token"),
) -> JSONResponse:
    row = _passport_row(verification_id)
    if row is None:
        raise HTTPException(404, "Verification ID was not found.")
    if not _authorize_token(row, token):
        raise HTTPException(403, "Verification authorization failed.")
    return JSONResponse(_passport_from_row(row))


@app.post("/api/approval")
async def approval(
    payload: dict[str, str],
    token: str = Header(default="", alias="X-Traceforge-Approval-Token"),
) -> JSONResponse:
    verification_id = payload.get("verification_id", "").strip()
    decision = payload.get("decision", "").strip().upper()
    if not verification_id:
        raise HTTPException(400, "verification_id is required.")
    if decision not in {"APPROVED", "REJECTED", "PENDING"}:
        raise HTTPException(400, "decision must be APPROVED, REJECTED, or PENDING.")
    row = _passport_row(verification_id)
    if row is None:
        raise HTTPException(404, "Verification ID was not found.")
    if row["decision"] != "PENDING":
        if decision == row["decision"]:
            raise HTTPException(409, "A final human decision has already been recorded.")
        raise HTTPException(409, "A human decision cannot be changed.")
    if not _authorize_token(row, token):
        raise HTTPException(403, "Approval authorization failed.")

    if decision == "PENDING":
        return JSONResponse(_passport_from_row(row))

    decided_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _database_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE verifications
            SET decision = ?, decided_at = ?
            WHERE verification_id = ?
              AND decision = 'PENDING'
              AND token_hash = ?
              AND token_expires_at >= ?
            """,
            (decision, decided_at, verification_id, row["token_hash"], time.time()),
        )
        if updated.rowcount != 1:
            connection.rollback()
            raise HTTPException(409, "A human decision has already been recorded or authorization expired.")
        connection.commit()
        final_row = connection.execute(
            "SELECT * FROM verifications WHERE verification_id = ?",
            (verification_id,),
        ).fetchone()
    return JSONResponse(_passport_from_row(final_row))
