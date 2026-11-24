import { readFile, readdir, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { fromHtml } from 'hast-util-from-html';
import { visit } from 'unist-util-visit';
import { base, origin, pages } from '../site.config.mjs';

const dist = fileURLToPath(new URL('../dist/', import.meta.url));

async function listFiles(directory) {
  const result = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const filename = path.join(directory, entry.name);
    if (entry.isDirectory()) result.push(...await listFiles(filename));
    else result.push(filename);
  }
  return result;
}

export function localPath(url, current) {
  const resolved = new URL(url, `${origin}${current}`);
  if (resolved.origin !== origin) return null;
  if (!resolved.pathname.startsWith(base)) throw new Error(`URL escapes Pages base: ${url} on ${current}`);
  return {
    pathname: decodeURIComponent(resolved.pathname.slice(base.length)),
    fragment: decodeURIComponent(resolved.hash.slice(1)),
  };
}

const files = await listFiles(dist);
const htmlFiles = files.filter((file) => file.endsWith('.html'));
const documents = new Map();
for (const file of htmlFiles) {
  const relative = path.relative(dist, file).split(path.sep).join('/');
  const route = relative.endsWith('index.html') ? relative.slice(0, -10) : relative;
  const tree = fromHtml(await readFile(file, 'utf8'));
  const ids = new Set();
  const links = [];
  let searchable = false;
  visit(tree, 'element', (node) => {
    if (node.properties.id) ids.add(String(node.properties.id));
    if ('dataPagefindBody' in node.properties) searchable = true;
    for (const property of ['href', 'src']) {
      if (typeof node.properties[property] === 'string') links.push(node.properties[property]);
    }
    if (typeof node.properties.srcSet === 'string') {
      links.push(...node.properties.srcSet.split(',').map((candidate) => candidate.trim().split(/\s+/)[0]));
    }
  });
  documents.set(route, { ids, links, searchable });
}

const errors = [];
for (const [route, document] of documents) {
  for (const link of document.links) {
    try {
      const target = localPath(link, `${base}${route}`);
      if (!target) continue;
      const destination = !target.pathname ? 'index.html' : target.pathname.endsWith('/')
        ? `${target.pathname}index.html` : target.pathname;
      const resolved = path.resolve(dist, destination);
      if (path.relative(dist, resolved).startsWith('..')) throw new Error(`Link escapes output: ${link}`);
      if (!(await stat(resolved)).isFile()) throw new Error(`Missing file: ${link}`);
      if (target.fragment) {
        const targetRoute = destination.endsWith('index.html') ? destination.slice(0, -10) : destination;
        const targetDoc = documents.get(targetRoute);
        if (targetDoc && !targetDoc.ids.has(target.fragment)) throw new Error(`Missing anchor: ${link}`);
      }
    } catch (error) {
      errors.push(`${route || '/'}: ${error.message}`);
    }
  }
}
for (const page of pages) {
  if (!documents.get(`${page.slug}/`)?.searchable) {
    errors.push(`Documentation missing from searchable HTML: ${page.slug}`);
  }
}
const publishedDocs = [...documents.keys()].filter((route) => route.startsWith('docs/'));
if (publishedDocs.length !== pages.length) errors.push('Published documentation differs from the allowlist.');
const search = JSON.parse(await readFile(path.join(dist, 'pagefind', 'pagefind-entry.json'), 'utf8'));
const indexedPages = Object.values(search.languages).reduce((sum, language) => sum + language.page_count, 0);
if (indexedPages !== pages.length) errors.push(`Search indexes ${indexedPages} pages; expected ${pages.length}.`);
if (errors.length) throw new Error(`Site validation failed:\n${errors.join('\n')}`);
console.log(`Checked ${htmlFiles.length} HTML pages, all local links/assets/anchors, and ${indexedPages} searchable guides.`);
