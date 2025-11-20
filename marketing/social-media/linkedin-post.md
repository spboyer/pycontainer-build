# LinkedIn Announcement Post

🚀 **Introducing pycontainer-build: Docker-free container images for Python**

Tired of Docker daemon dependencies? Want to build OCI images with pure Python code?

I'm working on an experimental project inspired by .NET's native container tooling that lets you:
✅ Build OCI-compliant images without Docker or Dockerfiles
✅ Use pure Python APIs—no shell commands or external daemons
✅ Auto-detect Python versions, dependencies, and entrypoints
✅ Create multi-layer images programmatically

**Why this matters:**
- Simpler CI/CD pipelines (no Docker-in-Docker)
- Faster local development iterations
- Native Python developer experience
- Works in restricted environments (Codespaces, locked-down VMs)

**Quick example:**
```bash
pip install -e .
pycontainer build --tag myapp:latest
```

That's it. No Dockerfile needed. The tool reads your `pyproject.toml`, auto-detects entry points, and creates a production-ready OCI image.

**Current features:**
- Registry push support (GHCR, ACR, Docker Hub)
- Smart base image selection from `requires-python`
- Layer caching for fast incremental builds
- SBOM generation (SPDX/CycloneDX)
- Framework auto-detection (FastAPI, Flask, Django)
- Poetry, Hatch, and GitHub Actions integrations

🔍 **Looking for developers to try it out and share feedback!**

This is an experimental project, and I'd love to hear:
- What use cases resonate with you?
- What blockers do you face with current Docker workflows?
- What features would make this production-ready for your team?

**Links:**
- 📦 Repo: https://github.com/spboyer/pycontainer-build
- 💬 Feedback: https://github.com/spboyer/pycontainer-build/issues
- 📖 Docs: See README for quick start guide

#Python #Containers #DevOps #OpenSource #CloudNative #Docker #OCI #DeveloperTools

---

## Posting Tips

**Timing:**
- Best posting times: Tuesday-Thursday, 8-10 AM or 12-2 PM (your timezone)
- Avoid Mondays (overloaded feeds) and Fridays (weekend mode)

**Engagement Strategy:**
- Respond to all comments within first 2 hours
- Ask follow-up questions to encourage discussion
- Share to relevant LinkedIn groups (Python Developers, DevOps Engineers, Cloud Native)

**Visual Enhancement:**
Consider adding:
- Screenshot of terminal showing `pycontainer build` command
- Diagram comparing Docker workflow vs. pycontainer workflow
- Short GIF/video demo (optional but recommended)

**Hashtag Strategy:**
- Primary: #Python #DevOps #Containers
- Secondary: #OpenSource #CloudNative #Docker
- Niche: #OCI #DeveloperTools
