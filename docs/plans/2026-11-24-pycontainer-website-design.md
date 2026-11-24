# Pycontainer website design

Approved by Shayne on 2026-11-24.

## Purpose and visual direction

Create a distinctive product homepage and searchable documentation on GitHub
Pages. Use Obsidian Blue (`#1a1f2b`), Sand Dune (`#d6c6a8`) and Hyper Coral
(`#ff4f5e`). The homepage uses editorial typography, generous spacing, a
purpose-built OCI layer illustration and real command examples. Avoid generic
marketing cards, gradients, invented testimonials and unsupported speed claims.
Reading surfaces are light; navigation and the hero are dark. Coral is an accent,
not low-contrast body text.

## Content

Explain the daemon-free Python-to-OCI workflow, source installation, framework
detection, layer caching, registry publication and tool integrations. Label the
project experimental and make dependency and platform limitations visible.

Organize current guides into getting started, configuration, images, registries,
integrations and security/reference. Add a focused first-build guide. Keep the
existing repository Markdown authoritative, publish through an explicit route
allowlist, and translate relative links to public routes or repository sources.
Do not publish maintenance plans or aspirational architecture/testing documents
as current product documentation.

## Architecture and alternatives

Use Astro with Starlight, customized for the approved visual direction. Node
dependencies are website build tools only; the Python package keeps its existing
runtime contract. MkDocs would be simpler but constrain the custom homepage.
Hand-written HTML would require maintaining documentation navigation and search.

Generate documentation pages from allowlisted Markdown during the build,
preserving code blocks and source edit links. Generate search locally, with no
analytics or external search service. Use the `/pycontainer-build/` Pages base.

## Delivery and validation

Build and check internal links on pull requests. Deploy only from main through
GitHub's Pages artifact workflow with deployment-specific permissions. Include
keyboard navigation, visible focus, reduced-motion support and mobile layouts.
Validate build output, document routes, anchor links, assets and search coverage;
inspect the homepage and documentation on desktop and mobile.

The design critique identified Markdown link resolution, accidental historical
content publication and subpath-safe assets as primary risks. Address these with
an explicit allowlist, syntax-aware link transformation and post-build checks.
