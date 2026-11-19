"""Hatch build hooks for pycontainer-build."""

from pathlib import Path
from typing import Any, Dict
from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from hatchling.plugin import hookimpl


class ContainerBuildHook(BuildHookInterface):
    """Build hook for creating container images with pycontainer-build."""

    PLUGIN_NAME = "pycontainer"

    def initialize(self, version: str, build_data: Dict[str, Any]) -> None:
        """Initialize the build process."""
        if self.config.get("skip", False):
            return

        project_root = Path(self.root)

        try:
            from pycontainer.config import BuildConfig
            from pycontainer.builder import ImageBuilder
        except ImportError:
            raise RuntimeError(
                "pycontainer-build is not installed. "
                "Install it with: pip install pycontainer-build"
            )

        # Get configuration
        project_name = self.metadata.name or "app"
        project_version = self.metadata.version or version
        tag = self.config.get("tag")
        if not tag:
            tag = f"{project_name}:{project_version}"

        base_image = self.config.get("base-image", "python:3.11-slim")
        push = self.config["push"] if "push" in self.config else False
        include_deps = self.config["include-deps"] if "include-deps" in self.config else True
        registry = self.config.get("registry")
        sbom = self.config.get("sbom")
        verbose = self.config["verbose"] if "verbose" in self.config else False
        dry_run = self.config.get("dry-run", False)
        env = dict(self.config.get("env", {}))
        labels = dict(self.config.get("labels", {}))
        use_cache = not self.config["no-cache"] if "no-cache" in self.config else True

        # Add build metadata to labels
        labels["org.opencontainers.image.version"] = project_version

        if verbose:
            print(f"Building container image: {tag}")
            print(f"  Base image: {base_image}")
            print(f"  Context: {project_root}")

        # Create build configuration
        config = BuildConfig(
            tag=tag,
            context_dir=str(project_root),
            base_image=base_image,
            include_deps=include_deps,
            generate_sbom=sbom or False,
            env=env,
            labels=labels,
            verbose=verbose,
            dry_run=dry_run,
            use_cache=use_cache,
        )

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
                builder.push(registry_url=registry)
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
