<script>
  import '../app.css';
  import { updated } from '$app/state';
  import { loadModels } from '$lib/modelstore.svelte.js';
  let { children } = $props();

  $effect(() => {
    loadModels();
  });

  // A deploy (scripts/build-live.sh) made a new build. The phone home-screen
  // app has no reload button, so load it as soon as nobody is typing; chat
  // drafts are saved server-side. SvelteKit checks every 60 s
  // (svelte.config.js version.pollInterval).
  $effect(() => {
    if (!updated.current) return;
    const reloadIfIdle = () => {
      const el = document.activeElement;
      const typing = !!el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT' || el.isContentEditable);
      if (document.visibilityState === 'hidden' || !typing) location.reload();
    };
    reloadIfIdle();
    const timer = setInterval(reloadIfIdle, 5000);
    document.addEventListener('visibilitychange', reloadIfIdle);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', reloadIfIdle);
    };
  });
</script>

{@render children()}
