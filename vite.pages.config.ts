import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import viteReact from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const rootDir = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  root: "spa",
  base: process.env.GITHUB_ACTIONS ? "/nai-recon/" : "./",
  publicDir: path.resolve(rootDir, "public"),
  envDir: rootDir,
  plugins: [viteReact(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(rootDir, "src") },
  },
  define: {
    "import.meta.env.VITE_PAGES": JSON.stringify("true"),
  },
  build: {
    outDir: path.resolve(rootDir, "dist-pages"),
    emptyOutDir: true,
  },
  server: {
    host: "0.0.0.0",
    port: 4173,
  },
});
