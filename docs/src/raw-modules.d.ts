// The live runner imports widelog's source with Vite's ?raw suffix, so the
// bundler resolves the path at build time and fails loudly if it moves.
declare module "*.py?raw" {
  const content: string;
  export default content;
}
