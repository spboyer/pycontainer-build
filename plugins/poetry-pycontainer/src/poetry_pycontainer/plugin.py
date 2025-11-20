"""Poetry plugin implementation for pycontainer-build."""

from cleo.commands.command import Command
from cleo.helpers import option
from poetry.plugins.application_plugin import ApplicationPlugin


class ContainerBuildCommand(Command):
    """Build a container image from the Poetry project."""

    name = "build-container"
    description = "Build an OCI container image using pycontainer-build"
    
    options = [
        option("tag", "t", "Container image tag", flag=False, default=None),
        option("base-image", "b", "Base container image", flag=False, default="python:3.11-slim"),
        option("registry", "r", "Container registry URL", flag=False, default=None),
        option("push", "p", "Push image to registry", flag=True),
        option("include-deps", "d", "Include Poetry dependencies", flag=True, default=True),
        option("sbom", "s", "Generate SBOM (spdx or cyclonedx)", flag=False, default=None),
        option("verbose", "v", "Verbose output", flag=True),
        option("dry-run", None, "Show what would be built without building", flag=True),
        option("no-cache", None, "Disable layer caching", flag=True),
    ]

    def handle(self) -> int:
        """Handle the build-container command."""
        try:
            # Import pycontainer-build
            from pycontainer.config import BuildConfig
            from pycontainer.builder import ImageBuilder
        except ImportError:
            self.line_error(
                "<error>pycontainer-build is not installed. "
                "Install it with: pip install pycontainer-build</error>"
            )
            return 1

        # Get Poetry project
        poetry = self.poetry
        package = poetry.package
        
        # Read configuration from pyproject.toml [tool.pycontainer]
        pyproject = poetry.pyproject
        tool_config = pyproject.data.get("tool", {}).get("pycontainer", {})
        
        # Determine tag
        tag = self.option("tag")
        if not tag:
            tag = tool_config.get("tag")
        if not tag:
            name = package.name
            version = str(package.version)
            tag = f"{name}:{version}"
        
        # Build configuration
        base_image = self.option("base-image") or tool_config.get("base_image", "python:3.11-slim")
        registry = self.option("registry") or tool_config.get("registry")
        push = self.option("push") or tool_config.get("push", False)
        include_deps = self.option("include-deps") or tool_config.get("include_deps", True)
        sbom = self.option("sbom") or tool_config.get("sbom")
        verbose = self.option("verbose") or tool_config.get("verbose", False)
        dry_run = self.option("dry-run") or tool_config.get("dry_run", False)
        no_cache = self.option("no-cache") or tool_config.get("no_cache", False)
        
        # Get environment variables from config
        env = tool_config.get("env", {})
        labels = tool_config.get("labels", {})
        
        # Add Poetry metadata to labels
        labels.update({
            "org.opencontainers.image.title": package.name,
            "org.opencontainers.image.version": str(package.version),
            "org.opencontainers.image.description": package.description or "",
        })
        if package.authors:
            authors = ", ".join(str(a) for a in package.authors)
            labels["org.opencontainers.image.authors"] = authors
        
        # Get project path
        project_path = poetry.file.path.parent
        
        self.line(f"<info>Building container image for {package.name} v{package.version}</info>")
        if verbose:
            self.line(f"  Tag: {tag}")
            self.line(f"  Base image: {base_image}")
            self.line(f"  Context: {project_path}")
            self.line(f"  Include deps: {include_deps}")
            if push:
                self.line(f"  Push: {registry or 'default registry'}")
        
        # Create build configuration
        config = BuildConfig(
            tag=tag,
            context_path=str(project_path),
            base_image=base_image,
            include_deps=include_deps,
            env=env,
            labels=labels,
            verbose=verbose,
            dry_run=dry_run,
            use_cache=not no_cache,
        )
        
        # Generate SBOM if requested
        if sbom:
            config.generate_sbom = sbom
        
        # Build the image
        try:
            builder = ImageBuilder(config)
            result = builder.build()
            
            self.line("<info>✓ Container image built successfully</info>")
            if verbose:
                self.line(f"  Output: {result or 'dist/image/'}")
            
            # Push if requested
            if push:
                self.line("<info>Pushing image to registry...</info>")
                builder.push()
                self.line("<info>✓ Image pushed successfully</info>")
            
            return 0
            
        except Exception as e:
            self.line_error(f"<error>Build failed: {e}</error>")
            if verbose:
                import traceback
                self.line_error(traceback.format_exc())
            return 1


class PycontainerPlugin(ApplicationPlugin):
    """Poetry plugin for pycontainer-build integration."""

    def activate(self, application):
        """Activate the plugin by registering commands."""
        application.command_loader.register_factory("build-container", lambda: ContainerBuildCommand())
