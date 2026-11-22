import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8008" } },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("/node_modules/katex/")) return "math";
          if (
            id.includes("/node_modules/react-dom/") ||
            id.includes("/node_modules/react/")
          )
            return "react";
        },
      },
    },
  },
});
