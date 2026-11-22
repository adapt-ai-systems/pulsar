import adapter from '@sveltejs/adapter-node';

/** @type {import('@sveltejs/kit').Config} */
const config = {
  kit: {
    // web/server.js serves the adapter-node output (pulsar-web.service).
    // scripts/build-live.sh points both dirs elsewhere so the live copy never
    // shares output with an ad-hoc `pnpm build` (the test gate).
    adapter: adapter({ out: process.env.PULSAR_WEB_OUT || 'build' }),
    outDir: process.env.PULSAR_WEB_KIT_DIR || '.svelte-kit',
    // Open tabs notice a new deploy and reload (src/routes/+layout.svelte).
    version: { pollInterval: 60000 }
  }
};

export default config;
