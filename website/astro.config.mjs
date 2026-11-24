import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import { base, groups, origin, repository } from './site.config.mjs';

export default defineConfig({
  site: origin,
  base,
  trailingSlash: 'always',
  integrations: [
    starlight({
      title: 'pycontainer',
      description: 'Build OCI images from Python projects. No Dockerfile. No Docker daemon.',
      favicon: '/favicon.svg',
      disable404Route: true,
      customCss: ['./src/styles/docs.css'],
      components: {
        SiteTitle: './src/components/DocsTitle.astro',
      },
      social: [{ icon: 'github', label: 'GitHub', href: repository }],
      sidebar: groups.map(({ label, pages }) => ({
        label,
        items: pages.map(([, slug]) => ({ slug })),
      })),
    }),
  ],
});
