export default {
  test: {
    environment: "jsdom",
    setupFiles: "./frontend/pm-console/src/test/setup.ts",
    globals: false,
  },
  esbuild: {
    jsx: "automatic",
    jsxImportSource: "react",
  },
};
