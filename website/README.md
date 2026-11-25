# Pycontainer website

Custom Astro homepage and Starlight documentation, published at
`https://spboyer.github.io/pycontainer-build/`.
Website dependencies are development tools only and do not change the Python
package's runtime dependencies.

## Local development

Use Node.js 24 and npm from this directory:

```powershell
npm ci
npm run dev
```

Open the URL printed by Astro, including `/pycontainer-build/`.
For the production output:

```powershell
npm test
npm run check
npm run build
npm run preview
```

`build` verifies every local HTML link, asset and anchor, the published route
allowlist and generated search coverage. External URLs are not fetched by this
check. Verify search in the production preview; Pagefind indexes at build time,
not in the development server.

## Documentation source

Repository Markdown remains authoritative. `site.config.mjs` is the explicit
page and navigation allowlist. `scripts/prepare-docs.mjs` generates ignored pages
under `src/content/docs/`, supplies titles and source edit links, and rewrites
Markdown links through a syntax tree. Code examples are not link-rewritten.

Selected README sections provide configuration, limitations and Python API
reference without publishing its marketing or historical phase checklist.
Maintenance plans, the knowledge-routing index and aspirational architecture
and testing documents are excluded from site navigation and search. Links to
unpublished source files remain links to GitHub.

Edit the original file, not a generated page. Add a current guide to the allowlist
to publish it. The explicit route keeps public URLs stable when source filenames
change. Broken source links fail before generated output is replaced.

## Branding

`src/styles/home.css` defines the homepage palette and layout.
`src/styles/docs.css` styles both documentation themes. The light theme uses a
darker coral derivative for readable text links; the supplied Hyper Coral is
retained for primary calls to action, the logo and dark-surface accents.
The layer illustration and logo are local SVG; fonts use system stacks.
There are no analytics, external font requests or hosted search APIs.

## GitHub Pages

`.github/workflows/pages.yml` builds affected PRs with read-only permissions.
Only a successful main-branch build can upload and deploy the Pages artifact.
Manual dispatch also requires main. Deployment uses the `github-pages`
environment with `pages: write` and `id-token: write`; PRs cannot deploy.

Enable Pages once in repository **Settings > Pages > Source > GitHub Actions**,
or through the authenticated Pages REST API using `build_type: workflow`.
The first deployment occurs after the workflow is merged to main.
Do not change the base path without updating links and validating the build.
