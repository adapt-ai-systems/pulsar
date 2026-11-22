<script>
  // The Pulsar cat: a black long-haired cat who lives on the UI's lines.
  import { PulsarCat } from './pulsarcat.js';

  let el = $state();
  $effect(() => {
    const cat = PulsarCat(el, {
      scale: 0.7,
      tone: 1 / 9, // dark coat with grey sides
      flank: 1,
      ledges: [
        { sel: '.grid-tabs', edge: 'bottom' },
        { sel: '.pane-head', edge: 'bottom' },
        { sel: '.fuel-rail', edge: 'bottom' },
        { sel: '.ta-field', edge: 'top' },
      ],
    });
    return () => cat.destroy();
  });
</script>

<div class="cat-layer" bind:this={el}></div>

<style>
  .cat-layer { position: fixed; inset: 0; pointer-events: none; z-index: 40; overflow: hidden; }
  .cat-layer :global(.cat-z) { position: absolute; color: #9aa3b5; font-size: 12px; pointer-events: none; animation: cat-zz 3s forwards; }
  .cat-layer :global(.cat-heart) { position: absolute; color: #ff7aa8; font-size: 13px; pointer-events: none; animation: cat-up 1.4s forwards; }
  @keyframes cat-zz { 0% { opacity: 0 } 30% { opacity: 1 } 100% { opacity: 0; transform: translate(14px, -30px) } }
  @keyframes cat-up { to { transform: translateY(-34px); opacity: 0 } }
</style>
