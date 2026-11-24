import { readFile, writeFile, mkdir, readdir, unlink } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { unified } from 'unified';
import remarkParse from 'remark-parse';
import remarkGfm from 'remark-gfm';
import remarkStringify from 'remark-stringify';
import { visit } from 'unist-util-visit';
import { toString } from 'mdast-util-to-string';
import GithubSlugger from 'github-slugger';
import { base, pages, repository } from '../site.config.mjs';

const website = fileURLToPath(new URL('../', import.meta.url));
const root = path.resolve(website, '..');
const output = path.join(website, 'src', 'content', 'docs');
const processor = unified().use(remarkParse).use(remarkGfm).use(remarkStringify);

export function selectContent(markdown, page) {
  const tree = processor.parse(markdown);
  if (page.section) {
    const start = tree.children.findIndex(
      (node) => node.type === 'heading' && node.depth === 2
        && toString(node).includes(page.section),
    );
    if (start === -1) throw new Error(`Missing section ${page.section} in ${page.source}`);
    const end = tree.children.findIndex(
      (node, index) => index > start && node.type === 'heading' && node.depth === 2,
    );
    tree.children = tree.children.slice(start, end === -1 ? undefined : end);
  }
  if (tree.children[0]?.type === 'heading') tree.children.shift();
  return tree;
}

function anchors(tree) {
  const slugger = new GithubSlugger();
  const ids = new Set();
  visit(tree, 'heading', (node) => ids.add(slugger.slug(toString(node))));
  return ids;
}

export function resolveLink(url, source, documents, repositoryRoot = root) {
  if (/^[a-z][a-z\d+.-]*:|^\/\/|^#/i.test(url)) return url;
  const [pathname, fragment] = url.split('#');
  const resolved = path.posix.normalize(path.posix.join(path.posix.dirname(source), decodeURI(pathname)));
  const matches = documents.filter((page) => page.source === resolved);
  const target = fragment
    ? matches.find((page) => page.anchors.has(decodeURIComponent(fragment)))
    : matches[0];
  if (target) {
    const isTitle = target.section && fragment === new GithubSlugger().slug(target.section);
    return `${base}${target.slug}/${fragment && !isTitle ? `#${fragment}` : ''}`;
  }
  if (resolved === 'docs' || resolved === 'docs/') return `${base}docs/start/quickstart/`;
  if (resolved.startsWith('../') || !existsSync(path.join(repositoryRoot, resolved))) {
    throw new Error(`Broken source link in ${source}: ${url}`);
  }
  const isDirectory = pathname.endsWith('/') || !path.posix.extname(pathname);
  return `${repository}/${isDirectory ? 'tree' : 'blob'}/main/${resolved}${fragment ? `#${fragment}` : ''}`;
}

async function cleanGenerated(directory) {
  await mkdir(directory, { recursive: true });
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const name = path.join(directory, entry.name);
    if (entry.isDirectory()) await cleanGenerated(name);
    else if (entry.name.endsWith('.md')) await unlink(name);
    else throw new Error(`Unexpected file in generated docs: ${name}`);
  }
}

export async function prepareDocs() {
  const documents = await Promise.all(pages.map(async (page) => {
    const tree = selectContent(await readFile(path.join(root, page.source), 'utf8'), page);
    const ids = anchors(tree);
    if (page.section) ids.add(new GithubSlugger().slug(page.section));
    return { ...page, tree, anchors: ids };
  }));
  const errors = [];
  for (const page of documents) {
    visit(page.tree, (node) => {
      if (['link', 'image', 'definition'].includes(node.type)) {
        try {
          node.url = resolveLink(node.url, page.source, documents);
        } catch (error) {
          errors.push(error);
        }
      }
    });
  }
  if (errors.length) throw new AggregateError(errors, 'Documentation source links are invalid.');
  // Generated output is disposable; source documents never change during publication.
  await cleanGenerated(output);
  for (const page of documents) {
    const destination = path.join(output, `${page.slug}.md`);
    await mkdir(path.dirname(destination), { recursive: true });
    const editUrl = `${repository}/edit/main/${page.source}`;
    const header = `---\ntitle: ${JSON.stringify(page.title)}\neditUrl: ${JSON.stringify(editUrl)}\n---\n\n`;
    await writeFile(destination, header + processor.stringify(page.tree));
  }
  console.log(`Prepared ${documents.length} documentation pages from repository sources.`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await prepareDocs();
}
