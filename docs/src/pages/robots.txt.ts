import { config } from "virtual:nimbus/config";

export const prerender = true;

export function GET() {
  const body = [
    "User-agent: *",
    // contentsignals.org. The site publishes llms.txt and a markdown copy of
    // every page, both of which exist to be read by a model, so declaring
    // anything other than yes here would contradict what is already served.
    "Content-Signal: ai-train=yes, search=yes, ai-input=yes",
    "Allow: /",
    "",
    `Sitemap: ${new URL("/sitemap-index.xml", config.site).href}`,
    "",
  ].join("\n");

  return new Response(body, {
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });
}
