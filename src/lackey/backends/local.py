"""LocalBackend — runs lackey in a local Docker container (DESIGN.md §8.3)."""

# ruff: noqa: T201 — print is used for user-facing status output

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from lackey.backends.base import RunResult


class LocalBackend:
    """Launches lackey runs via `docker run` with container hardening."""

    def __init__(
        self,
        output_base: Path | None = None,
        env_file: str | Path | None = None,
    ) -> None:
        self.output_base = output_base or Path("/tmp/lackey")
        self.env_file = Path(env_file).resolve() if env_file else None

    def launch(
        self,
        *,
        task: str,
        repo: str,
        run_id: str,
        image: str,
        timeout: int,
    ) -> RunResult:
        repo_path = Path(repo).resolve()
        if not repo_path.is_dir():
            print(f"ERROR: repo path {repo_path} does not exist", file=sys.stderr)
            raise SystemExit(1)

        output_dir = self.output_base / run_id
        output_dir.mkdir(parents=True, exist_ok=True)

        self._ensure_image(image)

        cmd = [
            "docker",
            "run",
            "--rm",
            # Hardening flags (DESIGN.md §8.3)
            "--read-only",
            "--tmpfs",
            "/work:size=4g,uid=1000,gid=1000",
            "--tmpfs",
            "/tmp:size=1g,uid=1000,gid=1000",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user",
            "1000:1000",
            # Bind mounts
            "-v",
            f"{repo_path}:/repo:ro",
            "-v",
            f"{output_dir}:/output",
            # Environment
            "-e",
            f"TASK={task}",
            "-e",
            f"RUN_ID={run_id}",
            "-e",
            f"TIMEOUT={timeout}",
            "-e",
            "LACKEY_DEBUG=1",
        ]

        if self.env_file:
            cmd += ["--env-file", str(self.env_file)]

        cmd.append(image)

        print(f"Launching local run {run_id}")
        print(f"  repo: {repo_path}")
        print(f"  image: {image}")
        print(f"  output: {output_dir}")

        result = subprocess.run(cmd, capture_output=False)

        return self._collect_result(run_id, output_dir, result.returncode)

    def _ensure_image(self, image: str) -> None:
        """Check if image exists locally; build if not."""
        check = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
        )
        if check.returncode != 0:
            print(f"Image {image} not found locally, building...")
            build = subprocess.run(
                ["docker", "build", "-t", image, "."],
                capture_output=False,
            )
            if build.returncode != 0:
                print("ERROR: Docker build failed", file=sys.stderr)
                raise SystemExit(1)

    def _collect_result(self, run_id: str, output_dir: Path, exit_code: int) -> RunResult:
        """Read run_summary.json from output dir and build RunResult."""
        summary_path = output_dir / "run_summary.json"

        if summary_path.exists():
            data = json.loads(summary_path.read_text())
            return RunResult(
                run_id=run_id,
                outcome=data.get("outcome", "error"),
                branch=data.get("branch", ""),
                artifact_dir=output_dir,
                runtime="local",
            )

        # Container failed before producing a summary
        return RunResult(
            run_id=run_id,
            outcome="error" if exit_code != 0 else "success",
            artifact_dir=output_dir,
            runtime="local",
        )
