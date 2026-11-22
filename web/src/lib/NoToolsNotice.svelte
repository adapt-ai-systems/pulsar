<div class="no-tools-card" role="status">
  <div class="no-tools-title">No agent tool found</div>
  <p>
    Pulsar did not find any supported agent tool on this machine. Install and sign in to at least one tool to start a pulse:
  </p>
  <ul class="no-tools-list">
    <li><strong>Claude Code</strong> (<code>claude</code>)</li>
    <li><strong>Codex</strong> (<code>codex</code>)</li>
    <li><strong>pi</strong> (<code>pi</code>)</li>
    <li><strong>Antigravity</strong> (<code>agy</code>)</li>
    <li><strong>Grok</strong> (<code>grok</code>)</li>
  </ul>
  <p class="no-tools-next">
    See the <strong>Agent tools</strong> section in <code>README.md</code> for setup steps, or <code>docs/setup/models.md</code> for local models. Reload this page after setup.
  </p>
</div>

<style>
  .no-tools-card {
    max-width: min(440px, calc(100% - 32px));
    padding: 18px 20px;
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: 12px;
    color: var(--ink-dim);
    font-family: var(--font-sans, inherit);
    font-size: calc(13px * var(--tscale-ui));
    line-height: 1.5;
    text-align: left;
  }
  .no-tools-title {
    font-family: var(--font-mono);
    font-size: calc(11px * var(--tscale-ui));
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink);
    margin-bottom: 10px;
  }
  .no-tools-card p {
    margin: 0 0 10px;
  }
  .no-tools-card p:last-child {
    margin-bottom: 0;
  }
  .no-tools-list {
    margin: 0 0 12px;
    padding-left: 18px;
    color: var(--ink);
  }
  .no-tools-list li {
    margin-bottom: 3px;
  }
  .no-tools-card code {
    font-family: var(--font-mono);
    font-size: calc(11px * var(--tscale-ui));
    color: var(--ink);
  }
  .no-tools-next {
    color: var(--ink-meta);
    font-size: calc(12px * var(--tscale-ui));
  }
</style>
