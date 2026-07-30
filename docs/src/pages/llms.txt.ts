// Root /llms.txt — sectioned index for AI agents.
import { getIndexedTopLevel } from "@cloudflare/nimbus-docs";
import { config } from "virtual:nimbus/config";

export const prerender = true;

export async function GET() {
  const { leaves, groups } = await getIndexedTopLevel();

  const lines = [
    `# ${config.title}`,
    "",
    config.description ?? "Documentation index for AI agents.",
    "",
    `Full corpus (all pages, one document): ${new URL("/llms-full.txt", config.site).href}`,
    "",
    "## Pages",
    "",
  ];

  // Sort by sidebar order, so the index reads in the order the site does.
  // Alphabetical put the guides group ahead of the introduction.
  type Row = { order: number; key: string; line: string };
  const rows: Row[] = [];

  for (const leaf of leaves) {
    const description = leaf.description ? ` — ${leaf.description}` : "";
    const data = leaf.entry.data as { sidebar?: { order?: number } };
    rows.push({
      order: data.sidebar?.order ?? Number.MAX_SAFE_INTEGER,
      key: leaf.url,
      line: `- [${leaf.title}](${new URL(leaf.markdownUrl, config.site).href})${description}`,
    });
  }

  for (const group of groups) {
    // Older doc versions have their own /<v>/llms.txt; don't list them here.
    if (group.kind === "version") continue;
    // A group sorts by its earliest member, which is how the sidebar places it.
    const order = Math.min(
      ...group.members.map((member) => {
        const data = member.entry.data as { sidebar?: { order?: number } };
        return data.sidebar?.order ?? Number.MAX_SAFE_INTEGER;
      }),
    );
    rows.push({
      order,
      key: `/${group.slug}`,
      line: `- [${group.label}](${new URL(`/${group.slug}/llms.txt`, config.site).href})`,
    });
  }

  rows.sort((a, b) => a.order - b.order || a.key.localeCompare(b.key));
  for (const row of rows) lines.push(row.line);

  lines.push("");

  return new Response(lines.join("\n"), {
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });
}
