import { defineConfig } from "astro/config";
import icon from "astro-icon";
import tailwindcss from "@tailwindcss/vite";
import nimbus, { defineConfig as defineNimbusConfig } from "@cloudflare/nimbus-docs";
import { tableScroll } from "@cloudflare/nimbus-docs/markdown";

const nimbusConfig = defineNimbusConfig({
  // Drives canonical URLs, OG image URLs, robots.txt, the sitemap, and the
  // links in /llms.txt. Must match where the site actually answers, or every
  // page points search engines at a host that does not resolve.
  site: "https://widelog-py.pages.dev",
  title: "widelog",
  description: "One wide event per operation, not a line per step. Wide-event logging for Python.",
  locale: "en",
  github: "https://github.com/ancs21/widelog-py",
  socialImageAlt: "widelog documentation",
});

export default defineConfig({
  output: "static",
  // The site root is the introduction. Nimbus routes content by file path, so
  // an index.mdx would land on /index rather than /, and rendering the entry
  // from a page of our own would mean rebuilding the sidebar, table of
  // contents and breadcrumb wiring that [...slug].astro already does.
  redirects: { "/": "/introduction" },
  // Tailwind v4 via its Vite plugin (the integration Astro recommends for
  // Tailwind v4 — replaces the PostCSS plugin, which doesn't build under
  // Astro 7's Vite 8 bundler).
  vite: {
    plugins: [tailwindcss()],
    // RunPython.astro imports the package source from one level up
    server: { fs: { allow: [".", ".."] } },
  },
  // Hover-prefetch link targets so full-page navigations feel instant without
  // a client-side router.
  prefetch: {
    prefetchAll: true,
    defaultStrategy: "hover",
  },
  integrations: [
    icon(),
    nimbus(nimbusConfig, {
      // Authoring rules are opt-in by design — your repo, your taste. The
      // two below are the load-bearing pair: frontmatter has to validate
      // against the content schema for the page to render properly, and
      // broken internal links are 404s for your readers. Add the others
      // (heading hierarchy, code-block language, style, etc.) when you're
      // ready to enforce them — see `nimbus-docs lint --help`.
      rules: {
        "nimbus/frontmatter-shape": "error",
        "nimbus/internal-link": "error",
      },
      // Wrap wide tables so they scroll instead of overflowing the page
      // (styled by `.nb-table-scroll` in src/styles/prose.css).
      markdown: {
        hastPlugins: [tableScroll()],
      },
    }),
  ],
});
