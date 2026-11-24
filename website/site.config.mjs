export const base = '/pycontainer-build/';
export const origin = 'https://spboyer.github.io';
export const repository = 'https://github.com/spboyer/pycontainer-build';
export const docsRedirect = '/docs/';

export const groups = [
  {
    label: 'Start here',
    pages: [
      ['docs/quickstart.md', 'docs/start/quickstart', 'Your first image'],
      ['docs/images-and-dependencies.md', 'docs/start/images-and-dependencies', 'Images & dependencies'],
      ['docs/local-development.md', 'docs/start/local-development', 'Local development'],
    ],
  },
  {
    label: 'Configure & build',
    pages: [
      ['README.md', 'docs/build/configuration', 'Configuration', 'Configuration'],
      ['docs/init-config.md', 'docs/build/init', 'Configuration templates'],
      ['docs/validate-config.md', 'docs/build/validate-config', 'Validate configuration'],
      ['docs/pre-commit.md', 'docs/build/pre-commit', 'Pre-commit hook'],
      ['docs/dockerfile-import.md', 'docs/build/dockerfile-import', 'Dockerfile import'],
      ['docs/metadata.md', 'docs/build/metadata', 'Image metadata'],
      ['docs/multiple-tags.md', 'docs/build/multiple-tags', 'Multiple tags'],
      ['docs/layer-squashing.md', 'docs/build/layer-squashing', 'Layer squashing'],
      ['docs/build-history.md', 'docs/build/history', 'Build history'],
    ],
  },
  {
    label: 'Registries',
    pages: [
      ['docs/REGISTRY_PUSH.md', 'docs/registries/push', 'Publish images'],
      ['docs/AUTHENTICATION.md', 'docs/registries/authentication', 'Authentication'],
      ['docs/REGISTRY_CLEANUP.md', 'docs/registries/cleanup', 'Registry cleanup'],
    ],
  },
  {
    label: 'Integrations',
    pages: [
      ['docs/github-actions.md', 'docs/integrations/github-actions', 'GitHub Actions'],
      ['docs/ci/gitlab.md', 'docs/integrations/gitlab', 'GitLab CI'],
      ['docs/ci/circleci.md', 'docs/integrations/circleci', 'CircleCI'],
      ['docs/ci/jenkins.md', 'docs/integrations/jenkins', 'Jenkins'],
      ['docs/skaffold-tilt.md', 'docs/integrations/skaffold-tilt', 'Skaffold & Tilt'],
      ['docs/azd-integration.md', 'docs/integrations/azd', 'Azure Developer CLI'],
      ['plugins/poetry-pycontainer/README.md', 'docs/integrations/poetry', 'Poetry'],
      ['plugins/hatch-pycontainer/README.md', 'docs/integrations/hatch', 'Hatch'],
      ['docs/deployment-artifacts.md', 'docs/integrations/deployment', 'Deployment artifacts'],
    ],
  },
  {
    label: 'Security & reference',
    pages: [
      ['docs/validation.md', 'docs/reference/validation', 'Validate OCI images'],
      ['docs/vulnerability-scanning.md', 'docs/reference/scanning', 'Vulnerability scanning'],
      ['docs/security-fragments.md', 'docs/reference/security', 'Runtime security fragments'],
      ['docs/system-packages.md', 'docs/reference/system-packages', 'System packages'],
      ['docs/libc-codename-mappings.md', 'docs/reference/libc', 'Libc release mappings'],
      ['README.md', 'docs/reference/limitations', 'Current limitations', 'Current Limitations'],
      ['README.md', 'docs/reference/python-api', 'Python API', 'Programmatic Usage'],
    ],
  },
];

export const pages = groups.flatMap(({ pages }) =>
  pages.map(([source, slug, title, section]) => ({ source, slug, title, section })),
);
