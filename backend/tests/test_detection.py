import io
import asyncio
import subprocess
import tempfile
import unittest
import zipfile
import json
import urllib.error
import time
import threading
import os
import sys
import socket
from pathlib import Path
from unittest.mock import MagicMock, patch

from sandbox.registry_proxy import _connect_target, _public_addresses, allowed, normalize_host

from app.main import (
    NODE_TEST_IMAGE,
    PYTHON_TEST_IMAGE,
    ProjectProfile,
    _apply_patch,
    _detect_project,
    _extract_project,
    _normalize_patch,
    _validate_patch_targets,
    _run_node_in_docker,
    _run_in_docker,
    _extract_debug_summary,
    _node_docker_environment,
    _run_prepared_node_verification,
    _run_prepared_python_verification,
    _run_python_preparation,
    _run_ai_review,
    _passport_test_evidence,
    _tool_versions,
    _stop_node_preparation_network,
    _python_not_required,
    _validate_python_requirements,
    _validate_node_lockfile,
    _verification_outcome,
    _database_connection,
    _store_verification,
    _passport_row,
    _passport_from_row,
    approval,
    verification_passport,
)


class PythonDetectionTests(unittest.TestCase):
    def test_python_requirements_are_detected_and_unpinned_requirements_are_not_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "module.py").write_text("value = 1\n")
            (project / "test_module.py").write_text("def test_value(): assert True\n")
            (project / "requirements.txt").write_text("colorama>=0.4\n")

            profile = _detect_project(project)
            valid, pinned, error = _validate_python_requirements(project / "requirements.txt")

            self.assertEqual(profile.relative_root, ".")
            self.assertTrue(valid)
            self.assertFalse(pinned)
            self.assertEqual(error, "")

    def test_python_requirements_reject_direct_urls_vcs_and_remote_directives(self):
            rejected = (
                "package @ https://attacker.example/package.whl\n",
                "git+https://attacker.example/repository.git\n",
                "hg+https://attacker.example/repository\n",
                "-r https://attacker.example/requirements.txt\n",
                "--extra-index-url https://attacker.example/simple\n",
                "-c https://attacker.example/constraints.txt\n",
                "-f https://attacker.example/packages\n",
            )
            with tempfile.TemporaryDirectory() as directory:
                requirements = Path(directory) / "requirements.txt"
                for content in rejected:
                    requirements.write_text(content)
                    valid, pinned, error = _validate_python_requirements(requirements)
                    self.assertFalse(valid, content)
                    self.assertFalse(pinned, content)
                    self.assertIn("line 1", error)

    def test_no_requirements_uses_not_required_preparation(self):
        result = _python_not_required()

        self.assertEqual(result["status"], "NOT_REQUIRED")
        self.assertFalse(result["requirements_file_detected"])
        self.assertTrue(result["dependencies_prepared"])
    def test_detects_src_project_with_pytest_tests_without_metadata(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "src").mkdir()
            (project / "tests").mkdir()
            (project / "src" / "email_utils.py").write_text("def normalize_email(value): return value")
            (project / "src" / "__init__.py").write_text("")
            (project / "tests" / "test_email_utils.py").write_text("def test_email(): pass")

            self.assertEqual(
                (_detect_project(project).language, _detect_project(project).test_command),
                ("Python", "python -m pytest -q"),
            )

    def test_python_without_tests_is_not_reported_as_runnable(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "src").mkdir()
            (project / "src" / "email_utils.py").write_text("")

            profile = _detect_project(project)

            self.assertEqual(profile.language, "Python")
            self.assertEqual(profile.image, PYTHON_TEST_IMAGE)
            self.assertEqual(profile.test_command, "")

    def test_detects_supported_pytest_filename_patterns(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "tests").mkdir()
            (project / "src").mkdir()
            (project / "src" / "module.py").write_text("")
            for filename in ("tests/test_first.py", "tests/second_test.py", "test_third.py", "fourth_test.py"):
                path = project / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("")

            self.assertEqual(_detect_project(project).test_command, "python -m pytest -q")

    def test_detects_nested_python_project_root(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "repository" / "src").mkdir(parents=True)
            (project / "repository" / "tests").mkdir()
            (project / "repository" / "src" / "module.py").write_text("value = 1\n")
            (project / "repository" / "__init__.py").write_text("")
            (project / "repository" / "tests" / "test_module.py").write_text(
                "from repository.src.module import value\n"
                "def test_value():\n"
                "    assert value == 1\n"
            )

            profile = _detect_project(project)

            self.assertEqual(profile.relative_root, "repository")


class NodeDetectionTests(unittest.TestCase):
    def test_detects_express_project_and_valid_test_script(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "package.json").write_text(json.dumps({
                "scripts": {"test": "node --test"},
                "dependencies": {"express": "^5.0.0"},
            }))
            (project / "package-lock.json").write_text("{}")

            self.assertEqual(
                (
                    _detect_project(project).language,
                    _detect_project(project).test_command,
                    _detect_project(project).relative_root,
                ),
                ("Node.js / Express", "npm test", "."),
            )

    def test_rejects_missing_and_placeholder_test_scripts(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "package.json").write_text(json.dumps({"scripts": {}}))
            self.assertEqual(_detect_project(project).test_command, "")
            (project / "package.json").write_text(json.dumps({
                "scripts": {"test": 'echo "Error: no test specified" && exit 1'},
            }))
            self.assertEqual(_detect_project(project).test_command, "")

    def test_detects_node_projects_at_any_nested_depth_from_synthetic_zip(self):
        import json
        import tempfile

        structures = ((".",), ("my-project",), ("repository", "apps", "backend"))
        for parts in structures:
            with self.subTest(parts=parts), tempfile.TemporaryDirectory() as directory:
                zip_path = Path(directory) / "project.zip"
                with zipfile.ZipFile(zip_path, "w") as archive:
                    root = Path(*parts) if parts != (".",) else Path()
                    prefix = f"{root.as_posix()}/" if root != Path() else ""
                    archive.writestr(prefix + "package.json", json.dumps({"scripts": {"test": "node --test"}}))
                    archive.writestr(prefix + "server.js", "export default 1;\n")
                    archive.writestr(prefix + "tests/server.test.js", "test();\n")
                extracted = Path(directory) / "extracted"
                extracted.mkdir()
                _extract_project(zip_path, extracted)
                profile = _detect_project(extracted)

                self.assertEqual(profile.language, "Node.js")
                self.assertEqual(profile.test_command, "npm test")
                expected = "." if parts == (".",) else "/".join(parts)
                self.assertEqual(profile.relative_root, expected)

    def test_multiple_valid_nested_node_projects_are_ambiguous(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("one", "two"):
                project = root / name
                project.mkdir()
                (project / "package.json").write_text(json.dumps({"scripts": {"test": "node --test"}}))

            profile = _detect_project(root)

            self.assertEqual(profile.test_command, "")
            self.assertEqual(profile.ambiguous_roots, ("one", "two"))
            self.assertIn("Multiple", profile.reason)

    def test_nested_package_without_test_script_is_not_valid(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "nested"
            project.mkdir()
            (project / "package.json").write_text(json.dumps({"name": "no-tests"}))

            profile = _detect_project(Path(directory))

            self.assertEqual(profile.language, "Unknown")
            self.assertEqual(profile.test_command, "")
            self.assertIn("No supported", profile.reason)


class DockerExecutionTests(unittest.TestCase):
    def setUp(self):
        with _database_connection() as connection:
            connection.execute("DELETE FROM verifications")

    def _stored_passport(self, verification_id="test-verification"):
        passport = {
            "verification_id": verification_id,
            "verification": {
                "status": "TESTS_PASSED",
                "tests_passed": True,
                "complete_correctness": "UNKNOWN",
            },
            "ai_review": {"status": "UNKNOWN", "findings": []},
            "human_approval": {
                "status": "PENDING",
                "decision": "PENDING",
                "verification_id": verification_id,
                "decided_at": None,
                "ai_review_status": "UNKNOWN",
            },
        }
        _store_verification(passport, f"token-{verification_id}", time.time() + 3600)
        return passport

    def test_human_approval_records_approved_decision_and_preserves_verification(self):
        self._stored_passport()
        result = asyncio.run(approval({
            "verification_id": "test-verification",
            "decision": "APPROVED",
        }, token="token-test-verification"))

        body = json.loads(result.body)
        self.assertEqual(body["human_approval"]["decision"], "APPROVED")
        self.assertEqual(body["human_approval"]["ai_review_status"], "UNKNOWN")
        self.assertEqual(body["verification"]["status"], "TESTS_PASSED")
        self.assertIsNotNone(body["human_approval"]["decided_at"])

    def test_human_approval_records_rejected_decision(self):
        self._stored_passport()
        result = asyncio.run(approval({
            "verification_id": "test-verification",
            "decision": "REJECTED",
        }, token="token-test-verification"))

        self.assertEqual(json.loads(result.body)["human_approval"]["status"], "REJECTED")

    def test_human_approval_rejects_invalid_and_unknown_requests(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as invalid:
            asyncio.run(approval({"verification_id": "missing", "decision": "MAYBE"}))
        self.assertEqual(invalid.exception.status_code, 400)
        with self.assertRaises(HTTPException) as unknown:
            asyncio.run(approval({"verification_id": "missing", "decision": "APPROVED"}))
        self.assertEqual(unknown.exception.status_code, 404)

    def test_human_approval_is_final_and_rejects_duplicates_or_conflicts(self):
        self._stored_passport()
        asyncio.run(approval({"verification_id": "test-verification", "decision": "APPROVED"}, token="token-test-verification"))

        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as duplicate:
            asyncio.run(approval({"verification_id": "test-verification", "decision": "APPROVED"}, token="token-test-verification"))
        self.assertEqual(duplicate.exception.status_code, 409)
        with self.assertRaises(HTTPException) as conflict:
            asyncio.run(approval({"verification_id": "test-verification", "decision": "REJECTED"}, token="token-test-verification"))
        self.assertEqual(conflict.exception.status_code, 409)

    def test_human_approval_rejects_missing_wrong_and_cross_verification_tokens(self):
        self._stored_passport("first")
        self._stored_passport("second")

        from fastapi import HTTPException
        for token in ("", "wrong", "token-second"):
            with self.assertRaises(HTTPException) as denied:
                asyncio.run(approval({
                    "verification_id": "first",
                    "decision": "APPROVED",
                }, token=token))
            self.assertEqual(denied.exception.status_code, 403)
        self.assertEqual(
            _passport_from_row(_passport_row("first"))["human_approval"]["decision"],
            "PENDING",
        )

    def test_approval_token_is_not_in_passport(self):
        passport = self._stored_passport()
        self.assertNotIn("token-test-verification", json.dumps(passport))

    def test_pending_and_final_passport_retrieval_require_token(self):
        self._stored_passport()
        pending = asyncio.run(verification_passport(
            "test-verification",
            token="token-test-verification",
        ))
        self.assertEqual(json.loads(pending.body)["human_approval"]["decision"], "PENDING")
        asyncio.run(approval({
            "verification_id": "test-verification",
            "decision": "APPROVED",
        }, token="token-test-verification"))
        final = asyncio.run(verification_passport(
            "test-verification",
            token="token-test-verification",
        ))
        self.assertEqual(json.loads(final.body)["human_approval"]["decision"], "APPROVED")

    def test_expired_token_cannot_retrieve_or_decide(self):
        self._stored_passport()
        with _database_connection() as connection:
            connection.execute(
                "UPDATE verifications SET token_expires_at = ? WHERE verification_id = ?",
                (time.time() - 1, "test-verification"),
            )
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as retrieval:
            asyncio.run(verification_passport("test-verification", token="token-test-verification"))
        self.assertEqual(retrieval.exception.status_code, 403)
        with self.assertRaises(HTTPException) as decision:
            asyncio.run(approval({
                "verification_id": "test-verification",
                "decision": "APPROVED",
            }, token="token-test-verification"))
        self.assertEqual(decision.exception.status_code, 403)

    def test_sqlite_state_survives_new_connection(self):
        self._stored_passport()
        row = _passport_row("test-verification")
        self.assertIsNotNone(row)
        self.assertNotEqual(row["token_hash"], "token-test-verification")
        self.assertEqual(len(row["token_hash"]), 64)
        self.assertEqual(_passport_from_row(row)["verification_id"], "test-verification")
        asyncio.run(approval({
            "verification_id": "test-verification",
            "decision": "REJECTED",
        }, token="token-test-verification"))
        with _database_connection() as connection:
            persisted = connection.execute(
                "SELECT decision FROM verifications WHERE verification_id = ?",
                ("test-verification",),
            ).fetchone()
        self.assertEqual(persisted["decision"], "REJECTED")

    def test_concurrent_decisions_allow_only_one_winner(self):
        self._stored_passport()
        results = []

        def submit(decision):
            try:
                asyncio.run(approval({
                    "verification_id": "test-verification",
                    "decision": decision,
                }, token="token-test-verification"))
                results.append(("success", decision))
            except Exception as caught:
                results.append((getattr(caught, "status_code", None), decision))

        first = threading.Thread(target=submit, args=("APPROVED",))
        second = threading.Thread(target=submit, args=("REJECTED",))
        first.start()
        second.start()
        first.join()
        second.join()
        self.assertEqual(sum(result[0] == "success" for result in results), 1)
        self.assertEqual(sum(result[0] == 409 for result in results), 1)

    @patch("app.main.sqlite3.connect")
    def test_database_connection_closes_after_success(self, connect):
        connection = MagicMock()
        connect.return_value = connection
        with _database_connection() as active:
            self.assertIs(active, connection)
        connection.close.assert_called_once_with()

    @patch("app.main.sqlite3.connect")
    def test_database_connection_closes_after_exception(self, connect):
        connection = MagicMock()
        connect.return_value = connection
        with self.assertRaises(RuntimeError):
            with _database_connection():
                raise RuntimeError("database operation failed")
        connection.rollback.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_two_backend_processes_share_atomic_approval_state(self):
        passport = {
            "verification_id": "multi-worker-verification",
            "created_at": "2026-01-01T00:00:00Z",
            "verification": {
                "status": "TESTS_PASSED",
                "tests_passed": True,
                "complete_correctness": "UNKNOWN",
            },
            "ai_review": {"status": "UNKNOWN", "findings": []},
            "human_approval": {
                "status": "PENDING",
                "decision": "PENDING",
                "verification_id": "multi-worker-verification",
                "decided_at": None,
                "ai_review_status": "UNKNOWN",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "shared.sqlite3")
            environment = os.environ.copy()
            environment.update({
                "TRACEFORGE_DATABASE_PATH": database,
                "TRACEFORGE_ENV": "development",
                "TRACEFORGE_ALLOWED_ORIGINS": "http://localhost:3000",
            })
            seed = (
                "from app.main import _store_verification; "
                f"_store_verification({passport!r}, 'multi-worker-token', 4102444800)"
            )
            subprocess.run(
                [sys.executable, "-c", seed],
                cwd=Path(__file__).parents[1],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )

            def free_port():
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    return sock.getsockname()[1]

            ports = (free_port(), free_port())
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "app.main:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ],
                    cwd=Path(__file__).parents[1],
                    env=environment,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                for port in ports
            ]
            try:
                deadline = time.time() + 30
                while time.time() < deadline:
                    try:
                        if all(
                            urllib.request.urlopen(
                                f"http://127.0.0.1:{port}/health",
                                timeout=1,
                            ).status == 200
                            for port in ports
                        ):
                            break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.1)
                else:
                    self.fail("Backend worker processes did not become ready.")

                results = []

                def submit(port, decision):
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/approval",
                        data=json.dumps({
                            "verification_id": "multi-worker-verification",
                            "decision": decision,
                        }).encode(),
                        headers={
                            "Content-Type": "application/json",
                            "X-Traceforge-Approval-Token": "multi-worker-token",
                        },
                        method="POST",
                    )
                    try:
                        with urllib.request.urlopen(request, timeout=10) as response:
                            results.append(("success", json.loads(response.read())))
                    except urllib.error.HTTPError as error:
                        results.append((error.code, None))

                first = threading.Thread(target=submit, args=(ports[0], "APPROVED"))
                second = threading.Thread(target=submit, args=(ports[1], "REJECTED"))
                first.start()
                second.start()
                first.join()
                second.join()

                self.assertEqual(sum(result[0] == "success" for result in results), 1)
                self.assertEqual(sum(result[0] == 409 for result in results), 1)
                for port in ports:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/verification/multi-worker-verification",
                        headers={"X-Traceforge-Approval-Token": "multi-worker-token"},
                    )
                    with urllib.request.urlopen(request, timeout=10) as response:
                        self.assertIn(
                            json.loads(response.read())["human_approval"]["decision"],
                            {"APPROVED", "REJECTED"},
                        )
            finally:
                for process in processes:
                    process.terminate()
                for process in processes:
                    process.wait(timeout=10)

    def test_repair_passport_evidence_preserves_status_and_counts(self):
        evidence = _passport_test_evidence({
            "status": "VERIFIED",
            "exit_code": 0,
            "duration_ms": 42,
            "output": "2 passed, 1 skipped",
        })

        self.assertEqual(evidence["status"], "VERIFIED")
        self.assertEqual(evidence["counts"], {"passed": 2, "failed": None, "skipped": 1, "total": 3})
        self.assertEqual(evidence["exit_code"], 0)
        self.assertEqual(evidence["duration_ms"], 42)
        node_evidence = _passport_test_evidence({
            "status": "VERIFIED",
            "output": "# tests 2\n# pass 2\n# fail 0\n",
        })
        self.assertEqual(node_evidence["counts"]["passed"], 2)

    def test_repair_passport_tool_versions_exclude_environment_values(self):
        versions = _tool_versions(
            ProjectProfile("Python", PYTHON_TEST_IMAGE, "pytest", "", Path("."), "."),
            {"status": "UNKNOWN"},
            {"status": "UNKNOWN"},
        )

        self.assertEqual(versions["traceforge"], "2.0.0")
        self.assertEqual(versions["sandbox_image"], PYTHON_TEST_IMAGE)
        self.assertNotIn("TRACEFORGE", json.dumps(versions))

    def test_ai_review_is_unknown_without_configuration(self):
        result = _run_ai_review(
            b"diff --git a/app.py b/app.py\n",
            ["app.py"],
            ProjectProfile("Python", PYTHON_TEST_IMAGE, "pytest", "", Path("."), "."),
            {"status": "FAILED", "exit_code": 1, "duration_ms": 10},
            {"status": "VERIFIED", "exit_code": 0, "duration_ms": 20},
            True,
        )

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("configure", result["message"])

    @patch("app.main.urllib.request.urlopen")
    def test_ai_review_validates_structured_suggested_output(self, urlopen):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "findings": [{
                            "severity": "HIGH",
                            "title": "Missing validation",
                            "explanation": "Input is not checked.",
                            "files": ["app.py"],
                            "confidence": "HIGH",
                        }],
                        "recommendations": ["Add a boundary test."],
                    })
                }
            }]
        }).encode()
        urlopen.return_value = response
        with patch.dict("os.environ", {
            "TRACEFORGE_AI_REVIEW_URL": "https://ai.example/review",
            "TRACEFORGE_AI_REVIEW_API_KEY": "secret-value",
        }):
            result = _run_ai_review(
                b"diff --git a/app.py b/app.py\n",
                ["app.py"],
                ProjectProfile("Python", PYTHON_TEST_IMAGE, "pytest", "", Path("."), "."),
                {"status": "FAILED", "exit_code": 1, "duration_ms": 10},
                {"status": "VERIFIED", "exit_code": 0, "duration_ms": 20},
                True,
            )

        self.assertEqual(result["status"], "SUGGESTED")
        self.assertEqual(result["provider"], "baseten")
        self.assertEqual(result["findings"][0]["status"], "SUGGESTED")
        self.assertEqual(result["recommendations"][0]["status"], "SUGGESTED")
        request = urlopen.call_args.args[0]
        self.assertNotIn("secret-value", request.data.decode())
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-value")
        self.assertEqual(request.get_header("User-agent"), "TRACEFORGE/2.0 (AI Patch Review)")
        self.assertIn("app.py", request.data.decode())

    @patch("app.main.urllib.request.urlopen")
    def test_groq_provider_uses_official_endpoint_and_configured_model(self, urlopen):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({
            "choices": [{"message": {"content": '{"findings":[],"recommendations":["Add a regression test."]}'}}],
        }).encode()
        urlopen.return_value = response
        with patch.dict("os.environ", {
            "TRACEFORGE_AI_REVIEW_PROVIDER": "groq",
            "TRACEFORGE_AI_REVIEW_API_KEY": "groq-secret",
            "TRACEFORGE_AI_REVIEW_MODEL": "configured-groq-model",
        }, clear=False):
            result = _run_ai_review(
                b"diff --git a/app.py b/app.py\n",
                ["app.py"],
                ProjectProfile("Python", PYTHON_TEST_IMAGE, "pytest", "", Path("."), "."),
                {"status": "FAILED", "exit_code": 1, "duration_ms": 10},
                {"status": "VERIFIED", "exit_code": 0, "duration_ms": 20},
                True,
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.groq.com/openai/v1/chat/completions")
        body = json.loads(request.data)
        self.assertEqual(body["model"], "configured-groq-model")
        self.assertEqual(request.get_header("Authorization"), "Bearer groq-secret")
        self.assertEqual(request.get_header("User-agent"), "TRACEFORGE/2.0 (AI Patch Review)")
        self.assertEqual(result["provider"], "groq")
        self.assertEqual(result["status"], "SUGGESTED")
        self.assertNotIn("groq-secret", request.data.decode())

    def test_groq_provider_requires_model_and_credentials(self):
        with patch.dict("os.environ", {
            "TRACEFORGE_AI_REVIEW_PROVIDER": "groq",
            "TRACEFORGE_AI_REVIEW_API_KEY": "",
            "TRACEFORGE_AI_REVIEW_MODEL": "",
        }, clear=False):
            result = _run_ai_review(
                b"diff --git a/app.py b/app.py\n", ["app.py"],
                ProjectProfile("Python", PYTHON_TEST_IMAGE, "pytest", "", Path("."), "."),
                {"status": "UNKNOWN"}, {"status": "UNKNOWN"}, False,
            )

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["provider"], "groq")
        self.assertIn("TRACEFORGE_AI_REVIEW_MODEL", result["message"])

    @patch("app.main.urllib.request.urlopen")
    def test_groq_malformed_response_is_unknown(self, urlopen):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"choices":[{"message":{"content":"not-json"}}]}'
        urlopen.return_value = response
        with patch.dict("os.environ", {
            "TRACEFORGE_AI_REVIEW_PROVIDER": "groq",
            "TRACEFORGE_AI_REVIEW_API_KEY": "groq-secret",
            "TRACEFORGE_AI_REVIEW_MODEL": "configured-groq-model",
        }, clear=False):
            result = _run_ai_review(
                b"diff --git a/app.py b/app.py\n", ["app.py"],
                ProjectProfile("Python", PYTHON_TEST_IMAGE, "pytest", "", Path("."), "."),
                {"status": "UNKNOWN"}, {"status": "UNKNOWN"}, False,
            )

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["provider"], "groq")

    @patch("app.main.urllib.request.urlopen")
    def test_groq_http_error_is_unknown_and_sanitized(self, urlopen):
        error = urllib.error.HTTPError(
            "https://api.groq.com/openai/v1/chat/completions",
            403,
            "Forbidden",
            {},
            io.BytesIO(b'{"error":"invalid api_key=secret-value"}'),
        )
        urlopen.side_effect = error
        with patch.dict("os.environ", {
            "TRACEFORGE_AI_REVIEW_PROVIDER": "groq",
            "TRACEFORGE_AI_REVIEW_API_KEY": "secret-value",
            "TRACEFORGE_AI_REVIEW_MODEL": "configured-groq-model",
        }, clear=False):
            with self.assertLogs("traceforge.ai_review", level="WARNING") as logs:
                result = _run_ai_review(
                    b"diff --git a/app.py b/app.py\n", ["app.py"],
                    ProjectProfile("Python", PYTHON_TEST_IMAGE, "pytest", "", Path("."), "."),
                    {"status": "UNKNOWN"}, {"status": "UNKNOWN"}, False,
                )

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["message"], "AI review unavailable: provider returned HTTP 403.")
        self.assertNotIn("secret-value", result["message"])
        self.assertNotIn("secret-value", "\n".join(logs.output))

    def test_unsupported_provider_is_unknown(self):
        with patch.dict("os.environ", {"TRACEFORGE_AI_REVIEW_PROVIDER": "unknown"}, clear=False):
            result = _run_ai_review(
                b"diff --git a/app.py b/app.py\n", ["app.py"],
                ProjectProfile("Python", PYTHON_TEST_IMAGE, "pytest", "", Path("."), "."),
                {"status": "UNKNOWN"}, {"status": "UNKNOWN"}, False,
            )

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("must be groq or baseten", result["message"])

    @patch("app.main.urllib.request.urlopen")
    def test_ai_review_failure_does_not_change_verification_evidence(self, urlopen):
        urlopen.side_effect = OSError("provider unavailable")
        with patch.dict("os.environ", {
            "TRACEFORGE_AI_REVIEW_URL": "https://ai.example/review",
            "TRACEFORGE_AI_REVIEW_API_KEY": "secret-value",
        }):
            result = _run_ai_review(
                b"diff --git a/app.py b/app.py\n",
                ["app.py"],
                ProjectProfile("Python", PYTHON_TEST_IMAGE, "pytest", "", Path("."), "."),
                {"status": "FAILED", "exit_code": 1, "duration_ms": 10},
                {"status": "VERIFIED", "exit_code": 0, "duration_ms": 20},
                True,
            )

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["findings"], [])
    @patch("app.main._ensure_docker_image", return_value=(True, ""))
    @patch("app.main.shutil.which", return_value="docker")
    @patch("app.main.subprocess.run")
    def test_python_preparation_captures_installation_evidence(
        self, run, _which, _ensure_image
    ):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Successfully installed colorama", stderr=""
        )

        with patch("app.main._start_node_preparation_network", return_value=("controlled", "proxy")), \
             patch("app.main._stop_node_preparation_network"):
          with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "requirements.txt").write_text("colorama==0.4.6\n")
            result = _run_python_preparation(project, PYTHON_TEST_IMAGE, "requirements.txt")

        command = run.call_args.args[0]
        self.assertIn("--network", command)
        self.assertIn("controlled", command)
        self.assertTrue(any("traceforge-proxy:3128" in value for value in command))
        self.assertIn("--only-binary=:all:", command[-1])
        self.assertIn("--target /workspace/.traceforge-python-packages", command[-1])
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["package_manager"], "pip")
        self.assertTrue(result["requirements_file_detected"])
        self.assertEqual(result["exit_code"], 0)

    @patch("app.main._ensure_docker_image", return_value=(True, ""))
    @patch("app.main.shutil.which", return_value="docker")
    @patch("app.main.subprocess.run")
    def test_python_preparation_failure_leaves_tests_unknown(
        self, run, _which, _ensure_image
    ):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="No matching distribution found"
        )

        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original"
            patched = Path(directory) / "patched"
            original.mkdir()
            patched.mkdir()
            (original / "requirements.txt").write_text("package-that-does-not-exist==0.0.0\n")
            (original / "test_module.py").write_text("def test_value(): assert True\n")
            result, patched_result = _run_prepared_python_verification(
                original,
                patched,
                ProjectProfile("Python", PYTHON_TEST_IMAGE, "python -m pytest -q", "", original, "."),
            )

        self.assertEqual(result["installation"]["status"], "FAILED")
        self.assertEqual(result["tests"]["status"], "UNKNOWN")
        self.assertEqual(patched_result["tests"]["status"], "UNKNOWN")
    def test_invalid_lockfile_and_missing_integrity_are_rejected(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "package.json").write_text(json.dumps({"dependencies": {"left-pad": "1.3.0"}}))
            valid, reason = _validate_node_lockfile(project, "https://registry.npmjs.org/")
            self.assertFalse(valid)
            self.assertIn("package-lock.json", reason)

            (project / "package-lock.json").write_text(json.dumps({
                "lockfileVersion": 3,
                "packages": {
                    "": {},
                    "node_modules/left-pad": {
                        "resolved": "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",
                    },
                },
            }))
            valid, reason = _validate_node_lockfile(project, "https://registry.npmjs.org/")
            self.assertFalse(valid)
            self.assertIn("integrity", reason)

            for resolved in (
                "http://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",
                "ftp://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",
                "not a url",
            ):
                lock = {
                    "lockfileVersion": 3,
                    "packages": {
                        "": {},
                        "node_modules/left-pad": {
                            "resolved": resolved,
                            "integrity": "sha512-test",
                        },
                    },
                }
                (project / "package-lock.json").write_text(json.dumps(lock))
                valid, reason = _validate_node_lockfile(project, "https://registry.npmjs.org/")
                self.assertFalse(valid)
                self.assertIn("resolved URL", reason)

            (project / "package-lock.json").write_text(json.dumps({
                "lockfileVersion": 3,
                "packages": {
                    "": {},
                    "node_modules/left-pad": {
                        "resolved": "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",
                        "integrity": "sha512-test",
                    },
                },
            }))
            valid, reason = _validate_node_lockfile(project, "https://registry.npmjs.org/")
            self.assertTrue(valid, reason)

    @patch("app.main._ensure_docker_image", return_value=(True, ""))
    @patch("app.main.shutil.which", return_value="docker")
    @patch("app.main.subprocess.run")
    def test_preparation_uses_network_and_tests_use_none(
        self, run, _which, _ensure_image
    ):
        run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="prepared", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="baseline", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="patched", stderr=""),
        ]
        with patch("app.main._start_node_preparation_network", return_value=("controlled", "proxy")), \
             patch("app.main._stop_node_preparation_network"):
            with tempfile.TemporaryDirectory() as directory:
                original = Path(directory) / "original"
                patched = Path(directory) / "patched"
                original.mkdir()
                patched.mkdir()
                (original / "package.json").write_text('{"scripts":{"test":"node --test"}}')
                (patched / "package.json").write_text('{"scripts":{"test":"node --test"}}')
                (original / "node_modules").mkdir()
                (original / "node_modules" / ".prepared").write_text("ok")
                baseline, patched_result = _run_prepared_node_verification(
                    original, patched, NODE_TEST_IMAGE, "npm test", "npm install", "."
                )

        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn("--network", commands[0])
        self.assertIn("controlled", commands[0])
        self.assertTrue(any("traceforge-proxy:3128" in argument for argument in commands[0]))
        self.assertIn("none", commands[1])
        self.assertIn("none", commands[2])
        self.assertEqual(baseline["dependency_preparation"]["status"], "VERIFIED")
        self.assertEqual(patched_result["tests"]["status"], "VERIFIED")

    def test_npm_debug_diagnostics_are_preserved(self):
        output = (
            "__TRACEFORGE_NPM_DIAGNOSTICS__\n"
            "node_version=v22.0.0\n"
            "npm_version=10.0.0\n"
            "cache=/tmp/npm-cache\n"
            "__TRACEFORGE_NPM_LOG__ /tmp/npm-logs/install.log\n"
            "npm error code ENOTCACHED\n"
        )

        summary = _extract_debug_summary(output)

        self.assertIn("node_version=v22.0.0", summary)
        self.assertIn("npm error code ENOTCACHED", summary)

    def test_registry_must_be_https_and_allowlisted(self):
        with patch.dict(
            "os.environ",
            {
                "TRACEFORGE_NPM_REGISTRY": "http://untrusted.example",
                "TRACEFORGE_NPM_REGISTRY_ALLOWLIST": "untrusted.example",
            },
            clear=False,
        ):
            with self.assertRaises(ValueError):
                _node_docker_environment()

    def test_registry_proxy_allows_only_approved_registry_host(self):
        with patch.dict("os.environ", {"TRACEFORGE_PROXY_HOST": "registry.npmjs.org"}):
            self.assertTrue(allowed("registry.npmjs.org", 443))
            self.assertTrue(allowed("registry.npmjs.org", 80))
            self.assertTrue(allowed("REGISTRY.NPMJS.ORG.", 443))
            self.assertFalse(allowed("example.com", 443))
            self.assertFalse(allowed("registry.npmjs.org", 8080))

    def test_registry_proxy_supports_only_explicit_multiple_approved_hosts(self):
        with patch.dict("os.environ", {
            "TRACEFORGE_PROXY_HOSTS": "pypi.org,files.pythonhosted.org",
            "TRACEFORGE_PROXY_HOST": "",
        }):
            self.assertFalse(allowed("example.com", 443))
            self.assertTrue(allowed("pypi.org", 443))

    def test_registry_proxy_rejects_ip_literals_and_private_resolution(self):
        with patch.dict("os.environ", {"TRACEFORGE_PROXY_HOST": "registry.npmjs.org"}):
            with patch("sandbox.registry_proxy.socket.getaddrinfo", return_value=[
                (2, 1, 6, "", ("127.0.0.1", 443)),
                (10, 1, 6, "", ("::1", 443, 0, 0)),
            ]):
                self.assertFalse(allowed("registry.npmjs.org", 443))
            self.assertFalse(allowed("127.0.0.1", 443))
            self.assertFalse(allowed("[::1]", 443))
            self.assertFalse(allowed("169.254.169.254", 443))

    def test_registry_proxy_uses_validated_address_without_second_dns_lookup(self):
        address = (2, 1, 6, "", ("8.8.8.8", 443))
        with patch("sandbox.registry_proxy.socket.getaddrinfo", return_value=[address]) as resolve:
            with patch("sandbox.registry_proxy.socket.socket") as socket_factory:
                self.assertEqual(_public_addresses("registry.npmjs.org", 443), [(2, 1, 6, ("8.8.8.8", 443))])
                self.assertEqual(resolve.call_count, 1)
                self.assertFalse(socket_factory.called)

    def test_registry_proxy_rejects_malformed_and_unbracketed_ipv6_connect_targets(self):
        with self.assertRaises(ValueError):
            _connect_target("[::1")
        with self.assertRaises(ValueError):
            _connect_target("::1:443")
        with self.assertRaises(ValueError):
            _connect_target("registry.npmjs.org:not-a-port")

    @patch("app.main.subprocess.run")
    def test_node_preparation_cleanup_attempts_network_removal_after_proxy_cleanup_error(self, run):
        run.side_effect = [OSError("docker unavailable"), subprocess.CompletedProcess(args=[], returncode=0)]

        _stop_node_preparation_network("traceforge-network", "traceforge-proxy")

        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0], ["docker", "network", "rm", "traceforge-network"])

    @patch("app.main._ensure_docker_image", return_value=(True, ""))
    @patch("app.main.shutil.which", return_value="docker")
    @patch("app.main.subprocess.run")
    def test_runs_with_pythonpath_and_returns_execution_evidence(
        self, run, _which, _ensure_image
    ):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=3, stdout="test output", stderr="failure"
        )

        result = _run_in_docker(
            Path("project"),
            PYTHON_TEST_IMAGE,
            "python -m pytest -q",
        )

        docker_command = run.call_args.args[0]
        self.assertIn("--network", docker_command)
        self.assertIn("none", docker_command)
        self.assertIn("--env", docker_command)
        self.assertIn("PYTHONPATH=/workspace", docker_command)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["exit_code"], 3)
        self.assertIn("test output", result["output"])
        self.assertGreaterEqual(result["duration_ms"], 0)

    def test_empty_test_command_is_unknown(self):
        result = _run_in_docker(Path("project"), PYTHON_TEST_IMAGE, "")

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["exit_code"])
        self.assertIn("No runnable tests", result["output"])

    @patch("app.main._ensure_docker_image", return_value=(True, ""))
    @patch("app.main.shutil.which", return_value="docker")
    @patch("app.main.subprocess.run")
    def test_genuine_execution_failure_is_failed(
        self, run, _which, _ensure_image
    ):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="collected 3 items", stderr="2 failed, 1 passed"
        )

        result = _run_in_docker(Path("project"), PYTHON_TEST_IMAGE, "python -m pytest -q")

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("2 failed, 1 passed", result["output"])

    @patch("app.main._ensure_docker_image", return_value=(True, ""))
    @patch("app.main.shutil.which", return_value="docker")
    @patch("app.main.subprocess.run")
    def test_node_install_and_tests_run_inside_sandbox(
        self, run, _which, _ensure_image
    ):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="1 passing", stderr=""
        )

        result = _run_in_docker(
            Path("project"),
            NODE_TEST_IMAGE,
            "npm test",
            "npm ci",
            "repository/apps/backend",
        )

        docker_command = run.call_args.args[0]
        self.assertIn("--network", docker_command)
        self.assertIn("none", docker_command)
        self.assertIn("--tmpfs", docker_command)
        self.assertIn("/workspace:rw,nosuid,size=512m", docker_command)
        self.assertIn("cd /workspace/repository/apps/backend && npm ci && npm test", docker_command[-1])
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["exit_code"], 0)

    @patch("app.main._ensure_docker_image", return_value=(True, ""))
    @patch("app.main.shutil.which", return_value="docker")
    @patch("app.main.subprocess.run")
    def test_node_install_uses_writable_npm_paths_and_separate_test_phase(
        self, run, _which, _ensure_image
    ):
        run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="installed", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="passed", stderr=""),
        ]

        with patch("app.main._start_node_preparation_network", return_value=("controlled", "proxy")), \
             patch("app.main._stop_node_preparation_network"):
            with __import__("tempfile").TemporaryDirectory() as directory:
                project = Path(directory)
                (project / "package.json").write_text("{}")
                result = _run_node_in_docker(project, NODE_TEST_IMAGE, "npm test", "npm ci", ".")

        install_command = run.call_args_list[0].args[0]
        test_command = run.call_args_list[1].args[0]
        self.assertEqual(run.call_count, 2)
        self.assertIn("NPM_CONFIG_CACHE=/tmp/npm-cache", install_command)
        self.assertIn("NPM_CONFIG_LOGS_DIR=/tmp/npm-logs", install_command)
        self.assertIn("HOME=/tmp/npm-home", install_command)
        self.assertTrue(any(argument.endswith(":/workspace:rw") for argument in install_command))
        self.assertTrue(any(argument.endswith(":/workspace:ro") for argument in test_command))
        self.assertEqual(result["installation"]["status"], "VERIFIED")
        self.assertEqual(result["tests"]["status"], "VERIFIED")

    @patch("app.main._ensure_docker_image", return_value=(True, ""))
    @patch("app.main.shutil.which", return_value="docker")
    @patch("app.main.subprocess.run")
    def test_node_install_failure_does_not_start_tests(
        self, run, _which, _ensure_image
    ):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="npm install failed"
        )

        with patch("app.main._start_node_preparation_network", return_value=("controlled", "proxy")), \
             patch("app.main._stop_node_preparation_network"):
            with __import__("tempfile").TemporaryDirectory() as directory:
                result = _run_node_in_docker(Path(directory), NODE_TEST_IMAGE, "npm test", "npm ci", ".")

        self.assertEqual(run.call_count, 1)
        self.assertEqual(result["installation"]["status"], "FAILED")
        self.assertEqual(result["tests"]["status"], "UNKNOWN")
        self.assertIn("npm install failed", result["installation"]["stderr"])

    @patch("app.main._ensure_docker_image", return_value=(True, ""))
    @patch("app.main.shutil.which", return_value="docker")
    @patch("app.main.subprocess.run")
    def test_node_install_timeout_leaves_tests_unknown(
        self, run, _which, _ensure_image
    ):
        with patch("app.main._start_node_preparation_network", return_value=("controlled", "proxy")), \
             patch("app.main._stop_node_preparation_network"):
            run.side_effect = subprocess.TimeoutExpired(["docker"], 1, output="partial")

            with __import__("tempfile").TemporaryDirectory() as directory:
                result = _run_node_in_docker(Path(directory), NODE_TEST_IMAGE, "npm test", "npm ci", ".")

        self.assertEqual(run.call_count, 1)
        self.assertEqual(result["installation"]["status"], "TIMEOUT")
        self.assertEqual(result["tests"]["status"], "UNKNOWN")

    @patch("app.main._ensure_docker_image", return_value=(True, ""))
    @patch("app.main.shutil.which", return_value="docker")
    @patch("app.main.subprocess.run")
    def test_node_test_failure_and_timeout_are_captured_after_install(
        self, run, _which, _ensure_image
    ):
        run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="installed", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=1, stdout="test output", stderr="assertion failed"),
        ]

        with patch("app.main._start_node_preparation_network", return_value=("controlled", "proxy")), \
             patch("app.main._stop_node_preparation_network"):
            with __import__("tempfile").TemporaryDirectory() as directory:
                failed = _run_node_in_docker(Path(directory), NODE_TEST_IMAGE, "npm test", "npm ci", ".")

        self.assertEqual(failed["installation"]["status"], "VERIFIED")
        self.assertEqual(failed["tests"]["status"], "FAILED")
        self.assertEqual(failed["tests"]["exit_code"], 1)
        self.assertIn("assertion failed", failed["tests"]["stderr"])

        run.reset_mock()
        run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.TimeoutExpired(["docker"], 1, stderr="test timed out"),
        ]
        with patch("app.main._start_node_preparation_network", return_value=("controlled", "proxy")), \
             patch("app.main._stop_node_preparation_network"):
            with __import__("tempfile").TemporaryDirectory() as directory:
                timed_out = _run_node_in_docker(Path(directory), NODE_TEST_IMAGE, "npm test", "npm ci", ".")

        self.assertEqual(timed_out["installation"]["status"], "VERIFIED")
        self.assertEqual(timed_out["tests"]["status"], "TIMEOUT")


class PatchApplicationTests(unittest.TestCase):
    PATCH = b"""diff --git a/src/email_utils.py b/src/email_utils.py
index 1111111..2222222 100644
--- a/src/email_utils.py
+++ b/src/email_utils.py
@@ -1,2 +1,2 @@
 def normalize_email(value: str) -> str:
-    return value.lower()
+    return value.strip().lower()
"""

    def test_changed_files_preserve_unprefixed_patch_paths(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            source = project / "module.py"
            source.write_text("value = 1\n")
            patch = project / "change.patch"
            patch.write_text(
                "--- module.py\n"
                "+++ module.py\n"
                "@@ -1 +1 @@\n"
                "-value = 1\n"
                "+value = 2\n"
            )

            applied, message = _apply_patch(patch, project)

            self.assertTrue(applied, message)
            self.assertEqual(source.read_text(), "value = 2\n")

    def _apply(self, patch_data: bytes) -> tuple[bool, str, str]:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "src").mkdir()
            source = project / "src" / "email_utils.py"
            source.write_text("def normalize_email(value: str) -> str:\n    return value.lower()\n")
            patch = project / "change.patch"
            patch.write_bytes(_normalize_patch(patch_data))
            applied, message = _apply_patch(patch, project)
            return applied, message, source.read_text()

    def test_valid_patch_applies_and_changes_working_copy(self):
        applied, message, source = self._apply(self.PATCH)

        self.assertTrue(applied, message)
        self.assertIn("value.strip().lower()", source)

    def test_patch_applies_but_failed_tests_are_tests_failed(self):
        bad_patch = self.PATCH.replace(
            b"return value.strip().lower()",
            b'return "wrong@example.com"',
        )
        applied, message, source = self._apply(bad_patch)

        self.assertTrue(applied, message)
        self.assertIn('return "wrong@example.com"', source)
        self.assertEqual(_verification_outcome(applied, "FAILED"), "TESTS_FAILED")

    def test_invalid_patch_does_not_apply(self):
        applied, message, source = self._apply(b"not a unified diff\n")

        self.assertFalse(applied)
        self.assertTrue(message)
        self.assertIn("value.lower()", source)

    def test_crlf_patch_applies(self):
        applied, message, source = self._apply(self.PATCH.replace(b"\n", b"\r\n"))

        self.assertTrue(applied, message)
        self.assertIn("value.strip().lower()", source)

    def test_utf8_bom_patch_applies(self):
        applied, message, source = self._apply(b"\xef\xbb\xbf" + self.PATCH)

        self.assertTrue(applied, message)
        self.assertIn("value.strip().lower()", source)

    def test_outcomes_distinguish_patch_and_test_results(self):
        self.assertEqual(_verification_outcome(False, "UNKNOWN"), "PATCH_APPLICATION_FAILED")
        self.assertEqual(_verification_outcome(True, "FAILED"), "TESTS_FAILED")
        self.assertEqual(_verification_outcome(True, "TIMEOUT"), "TESTS_FAILED")
        self.assertEqual(_verification_outcome(True, "VERIFIED"), "TESTS_PASSED")
        self.assertEqual(_verification_outcome(True, "UNKNOWN"), "UNKNOWN")

    def test_nested_patch_applies_from_archive_root_without_modifying_baseline(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            archive_root = Path(directory) / "baseline"
            patched_root = Path(directory) / "patched"
            source = archive_root / "repository" / "apps" / "backend" / "server.js"
            source.parent.mkdir(parents=True)
            source.write_text("export const value = 'old';\n")
            import shutil
            shutil.copytree(archive_root, patched_root)
            patch_data = b"""diff --git a/repository/apps/backend/server.js b/repository/apps/backend/server.js
index 1111111..2222222 100644
--- a/repository/apps/backend/server.js
+++ b/repository/apps/backend/server.js
@@ -1 +1 @@
-export const value = 'old';
+export const value = 'new';
"""
            normalized = _normalize_patch(patch_data)
            valid, message = _validate_patch_targets(normalized, archive_root)
            self.assertTrue(valid, message)
            patch = Path(directory) / "nested.patch"
            patch.write_bytes(normalized)
            applied, message = _apply_patch(patch, patched_root)

            self.assertTrue(applied, message)
            self.assertIn("'old'", source.read_text())
            self.assertIn("'new'", (patched_root / "repository" / "apps" / "backend" / "server.js").read_text())

    def test_missing_and_traversal_patch_targets_are_rejected(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = b"--- a/missing.js\n+++ b/missing.js\n@@ -1 +1 @@\n-old\n+new\n"
            unsafe = b"--- a/../../escape.js\n+++ b/../../escape.js\n@@ -1 +1 @@\n-old\n+new\n"

            self.assertEqual(_validate_patch_targets(missing, root)[0], False)
            self.assertIn("not found", _validate_patch_targets(missing, root)[1])
            self.assertEqual(_validate_patch_targets(unsafe, root)[0], False)
            self.assertIn("unsafe", _validate_patch_targets(unsafe, root)[1])


if __name__ == "__main__":
    unittest.main()
