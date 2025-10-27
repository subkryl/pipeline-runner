import logging
import os
from typing import TypeVar, cast

from .config import config
from .context import StepRunContext
from .models import CloneSettings, Image

logger = logging.getLogger(__name__)


T = TypeVar("T")


class RepositoryCloner:
    def __init__(
        self,
        ctx: StepRunContext,
        environment: dict[str, str],
        user: int | str | None,
        parent_container_name: str,
        data_volume_name: str,
        output_logger: logging.Logger,
    ) -> None:
        self._ctx = ctx
        self._repository = ctx.pipeline_ctx.repository
        self._step_clone_settings = ctx.step.clone_settings
        self._global_clone_settings = ctx.pipeline_ctx.clone_settings
        self._environment = environment
        self._user = str(user) if user is not None else None
        self._name = f"{parent_container_name}-clone"
        self._data_volume_name = data_volume_name
        self._output_logger = output_logger

        self._container = None

    def clone(self) -> None:
        # TODO: Fix cyclic import
        from .container import ContainerRunner  # noqa: PLC0415  # Import should be at top of file

        if not self._should_clone():
            logger.info("Clone disabled: skipping")
            return

        image = Image(name="alpine/git", run_as_user=self._user)
        runner = ContainerRunner(
            self._ctx,
            self._name,
            image,
            None,
            self._data_volume_name,
            self._environment,
            self._output_logger,
        )
        runner.start()

        try:
            exec_result = runner.run_command(
                f"git config --system --add safe.directory '{config.remote_workspace_dir}/.git'", user=0
            )
            if exec_result.exit_code:
                raise Exception("Error setting up repository")

            clone_script = self._get_clone_script()
            exit_code = runner.run_script(clone_script)

            if exit_code:
                raise Exception("Error setting up repository")
        finally:
            runner.stop()

    def _get_clone_script(self) -> list[str]:
        # Check if we're in a submodule and adjust the workspace path accordingly
        workspace_path = config.remote_workspace_dir
        
        # If we have a parent repository path from environment, use it
        parent_repo_path = os.environ.get('PIPELINE_RUNNER_PARENT_REPO_PATH')
        if parent_repo_path:
            workspace_path = parent_repo_path
        
        return [
            # First, let's see what's in the workspace
            f"echo 'Contents of workspace {workspace_path}:'",
            f"ls -la {workspace_path}",
            # Copy the repository from host to container
            f"cp -r {workspace_path} $BUILD_DIR",
            # Copy the .git file from the submodule if it exists
            f"echo 'Checking for .git file in current workspace...'",
            f"if [ -f {workspace_path}/.git ]; then",
            f"  echo 'Found .git file at: {workspace_path}/.git'",
            f"  echo 'This is a submodule .git file, removing it to let git handle it properly'",
            f"  rm -f $BUILD_DIR/.git",
            "else",
            f"  echo 'No .git file found in current workspace'",
            "fi",
            # Initialize a git repository if we don't have one
            "if [ ! -d $BUILD_DIR/.git ]; then",
            "  echo 'Initializing git repository...'",
            "  cd $BUILD_DIR",
            "  git init",
            "  git config user.name bitbucket-pipelines",
            "  git config user.email commits-noreply@bitbucket.org",
            "  git add .",
            "  git commit -m 'Initial commit'",
            "  echo 'Creating initial commit...'",
            "else",
            "  echo 'Repository already exists, resetting...'",
            "  cd $BUILD_DIR",
            "  git add .",
            "  git commit -m 'Update commit'",
            "fi",
            "cd $BUILD_DIR",
            # Create initial commit first
            "echo 'Creating initial commit...'",
            "git add .",
            "if git diff --cached --quiet; then",
            "  echo 'No changes to commit, working tree is clean'",
            "else",
            "  git commit -m 'Initial commit'",
            "  echo 'Initial commit created'",
            "fi",
            "git config user.name bitbucket-pipelines",
            "git config user.email commits-noreply@bitbucket.org",
            "git config push.default current",
            # TODO: "git config http.${BITBUCKET_GIT_HTTP_ORIGIN}.proxy http://localhost:29418/",
            # Only set remote if it doesn't exist or if we have a different URL
            "if git remote get-url origin >/dev/null 2>&1; then",
            f"  git remote set-url origin file://{workspace_path}",
            "  echo 'Updated existing remote origin'",
            "else",
            f"  git remote add origin file://{workspace_path}",
            "  echo 'Added remote origin'",
            "fi",
            "git reflog expire --expire=all --all",
            "echo '.bitbucket/pipelines/generated' >> .git/info/exclude",
            # Initialize and update submodules if they exist
            "if [ -f .gitmodules ]; then",
            "  git submodule update --init --recursive",
            "fi",
        ]

    @staticmethod
    def _get_origin() -> str:
        # https://x-token-auth:$REPOSITORY_OAUTH_ACCESS_TOKEN@bitbucket.org/$BITBUCKET_REPO_FULL_NAME.git
        
        # First, check if we have a parent repository path from environment
        parent_repo_path = os.environ.get('PIPELINE_RUNNER_PARENT_REPO_PATH')
        if parent_repo_path:
            return f"file://{parent_repo_path}"
        
        # Then try to detect it automatically
        if hasattr(self, '_parent_repo_path') and self._parent_repo_path:
            return f"file://{self._parent_repo_path}"
        
        # Default to the configured workspace directory
        return f"file://{config.remote_workspace_dir}"

    def _get_clone_command(self, origin: str) -> str:
        git_clone_cmd = []

        if not self._should_clone_lfs():
            git_clone_cmd += ["GIT_LFS_SKIP_SMUDGE=1"]

        # TODO: Add `retry n`
        branch = self._repository.get_current_branch()
        git_clone_cmd += ["git", "clone", f"--branch='{branch}'"]

        clone_depth = self._get_clone_depth()
        if clone_depth:
            git_clone_cmd += ["--depth", str(clone_depth)]

        git_clone_cmd += [origin, "$BUILD_DIR"]

        return " ".join(git_clone_cmd)

    def _should_clone(self) -> bool:
        return bool(
            self._first_non_none_value(
                self._step_clone_settings.enabled,
                self._global_clone_settings.enabled,
                CloneSettings().enabled,
            )
        )

    def _should_clone_lfs(self) -> bool:
        return bool(
            self._first_non_none_value(
                self._step_clone_settings.lfs,
                self._global_clone_settings.lfs,
                CloneSettings().lfs,
            )
        )

    def _get_clone_depth(self) -> str | int | None:
        depth = self._first_non_none_value(
            self._step_clone_settings.depth,
            self._global_clone_settings.depth,
            CloneSettings().depth,
        )

        return cast("str | int | None", depth)

    @staticmethod
    def _first_non_none_value(*args: T | None) -> T | None:
        return next((v for v in args if v is not None), None)
