"""Hatch build hooks for pycontainer-build."""

from pathlib import Path
from typing import Any, Dict
from hatchling.plugin import hookimpl


class ContainerBuildHook:
    """Build hook for creating container images with pycontainer-build."""

    PLUGIN_NAME = "pycontainer"

    def __init__(self, root: str, config: Dict[str, Any]):
        """Initialize the build hook.
        
        Args:
            root: Project root directory
            config: Hook configuration from pyproject.toml
        """
        self.root = Path(root)
        self.config = config

    def initialize(self, version: str, build_data: Dict[str, Any]) -> None:
        """Initialize the build process."""
        if self.config.get("skip", False):
            return

        try:
            from pycontainer.config import BuildConfig
            from pycontainer.builder import ImageBuilder
        except ImportError:
            raise RuntimeError(
                "pycontainer-build is not installed. "
                "Install it with: pip install pycontainer-build"
            )

        # Get configuration
        tag = self.config.get("tag")
        if not tag:
            # Read from pyproject.toml
            import tomllib
            pyproject_path = self.root / "pyproject.toml"
            if pyproject_path.exists():
                with open(pyproject_path, "rb") as f:
                    pyproject = tomllib.load(f)
                    project = pyproject.get("project", {})
                    name = project.get("name", "app")
                    tag = f"{name}:{version}"
            else:
                tag = f"app:{version}"

        base_image = self.config.get("base-image", "python:3.11-slim")
        registry = self.config.get("registry")
        push = self.config.get("push", False)
        include_deps = self.config.get("include-deps", True)
        sbom = self.config.get("sbom")
        verbose = self.config.get("verbose", False)
        env = self.config.get("env", {})
        labels = self.config.get("labels", {})
        use_cache = not self.config.get("no-cache", False)

        # Add build metadata to labels
        labels["org.opencontainers.image.version"] = version

        if verbose:
            print(f"Building container image: {tag}")
            print(f"  Base image: {base_image}")
            print(f"  Context: {self.root}")

        # Create build configuration
        config = BuildConfig(
            tag=tag,
            context_path=str(self.root),
            base_image=base_image,
            include_deps=include_deps,
            env=env,
            labels=labels,
            verbose=verbose,
            use_cache=use_cache,
        )

        if sbom:
            config.generate_sbom = sbom

        # Build the image
        try:
            builder = ImageBuilder(config)
            builder.build()

            if verbose:
                print(f"✓ Container image built: {tag}")

            # Push if requested
            if push:
                if verbose:
                    print("Pushing image to registry...")
                builder.push()
                if verbose:
                    print("✓ Image pushed successfully")

        except Exception as e:
            raise RuntimeError(f"Container build failed: {e}")

        # Store build info in build_data
        build_data["container_image"] = {
            "tag": tag,
            "base_image": base_image,
            "pushed": push,
        }


@hookimpl
def hatch_register_build_hook():
    """Register the container build hook with Hatch."""
    return ContainerBuildHook
