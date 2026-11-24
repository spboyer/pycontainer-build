import { test } from 'node:test';
import assert from 'node:assert/strict';
import { selectContent, resolveLink } from './prepare-docs.mjs';
import { base } from '../site.config.mjs';

test('published guides and README sections resolve to subpath-safe routes', () => {
  const documents = [
    { source: 'docs/validation.md', slug: 'docs/reference/validation', anchors: new Set(['rules']) },
    { source: 'README.md', slug: 'docs/build/configuration', section: 'Configuration', anchors: new Set(['configuration', 'explicit-configuration']) },
  ];
  assert.equal(resolveLink('validation.md#rules', 'docs/quickstart.md', documents),
    `${base}docs/reference/validation/#rules`);
  assert.equal(resolveLink('../README.md#configuration', 'docs/quickstart.md', documents),
    `${base}docs/build/configuration/`);
  assert.equal(resolveLink('../../README.md#explicit-configuration', 'docs/ci/jenkins.md', documents),
    `${base}docs/build/configuration/#explicit-configuration`);
});

test('unpublished sources stay on GitHub and invalid local links fail', () => {
  assert.equal(resolveLink('plans/2026-11-24-pycontainer-website-design.md', 'docs/quickstart.md', []),
    'https://github.com/spboyer/pycontainer-build/blob/main/docs/plans/2026-11-24-pycontainer-website-design.md');
  assert.throws(() => resolveLink('../../outside.md', 'docs/quickstart.md', []), /Broken source link/);
  assert.throws(() => resolveLink('missing.md', 'docs/quickstart.md', []), /Broken source link/);
  assert.equal(resolveLink('#local-section', 'docs/quickstart.md', []), '#local-section');
  assert.equal(resolveLink('https://example.com/guide', 'docs/quickstart.md', []), 'https://example.com/guide');
});

test('section selection excludes surrounding roadmap text and preserves code', () => {
  const markdown = '# Project\n\n## Configuration\n\n### Example\n\n```python\n# Not a heading\nprint("image")\n```\n\n## Roadmap\n\nFuture work.';
  const tree = selectContent(markdown, { source: 'README.md', section: 'Configuration' });
  assert.deepEqual(tree.children.map((node) => node.type), ['heading', 'code']);
  assert.equal(tree.children[1].value, '# Not a heading\nprint("image")');
  assert.throws(() => selectContent(markdown, { source: 'README.md', section: 'Absent' }), /Missing section/);
});
