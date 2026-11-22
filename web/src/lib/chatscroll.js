// A bottom gap is geometry, not user intent: streamed rows, ghost handovers,
// and the bars below chat can all create one without anyone scrolling.
export function createChatScroll(node, content, {
  follows, setFollowing, catchUp,
  requestFrame = requestAnimationFrame, cancelFrame = cancelAnimationFrame,
  Observer = ResizeObserver, viewport = window.visualViewport,
}) {
  let frame = 0;
  let revision = 0;
  let destroyed = false;
  let lastTop = node.scrollTop;
  let touchY = null;
  // The re-pin loop is bounded, not endless. See anchor().
  let pinFrames = 0;
  const MAX_PIN_FRAMES = 120;
  const cleanups = [];
  const nearBottom = () => node.scrollHeight - node.scrollTop - node.clientHeight < 96;
  function cancel() {
    revision++;
    cancelFrame(frame);
    frame = 0;
  }
  function release() {
    cancel();
    setFollowing(false);
  }
  // One anchor call = ONE settle, not a standing order to stay at the bottom.
  // The loop used to run until the next cancel()/release(), so a pane following
  // the tail was pinned to scrollHeight every frame indefinitely. The flush
  // that needs catching is a handful of frames; after that, any growth in
  // scrollHeight is content reflow *below* the fold — and re-pinning it scrolls
  // the reply the user is reading out of view. At the turn boundary the
  // finished reply and the streaming ghost render together; markdown reflow on
  // the finished reply grew the content, the endless pin loop scrolled it away,
  // and the ghost (the
  // same text) was the only reply left visible. The ghost retired on the next
  // render, so nothing ever un-duplicated it on screen.
  //
  // resized() still re-arms a settle on a genuine viewport/content resize, so
  // legitimately late-growing content (images loading) keeps its follow.
  function anchor(keep = 0, toBottom = true) {
    cancel();
    const token = revision;
    pinFrames = 0;
    const pin = () => {
      if (destroyed || token !== revision || (toBottom && !follows())) return;
      node.scrollTop = toBottom ? node.scrollHeight : Math.max(0, node.scrollHeight - keep);
      lastTop = node.scrollTop;
      if (++pinFrames >= MAX_PIN_FRAMES) {
        frame = 0;
        return;
      }
      frame = requestFrame(pin);
    };
    frame = requestFrame(pin);
  }
  function resized() {
    if (follows()) anchor();
  }
  function onScroll() {
    const movedDown = node.scrollTop > lastTop;
    lastTop = node.scrollTop;
    // Never detach on a layout-generated scroll. Only explicit input below
    // can release the tail; scrolling down by hand can rejoin it.
    if (!follows() && movedDown && nearBottom()) catchUp();
  }
  function nestedScroll(target, delta) {
    for (let el = target; el && el !== node; el = el.parentElement) {
      if (el.scrollHeight > el.clientHeight && /auto|scroll/.test(getComputedStyle(el).overflowY)) {
        if (delta < 0 ? el.scrollTop > 0 : el.scrollTop + el.clientHeight < el.scrollHeight) return true;
      }
    }
    return false;
  }
  function onWheel(e) {
    if (e.deltaY < 0 && !nestedScroll(e.target, e.deltaY)) release();
  }
  function onTouchStart(e) { touchY = e.touches[0]?.clientY ?? null; }
  function onTouchMove(e) {
    const y = e.touches[0]?.clientY ?? null;
    if (y !== null && touchY !== null && y > touchY && !nestedScroll(e.target, touchY - y)) release();
    touchY = y;
  }
  function onKey(e) {
    if (e.target?.closest('input, textarea, select, button, a, [contenteditable="true"], [role="button"]')) return;
    if (['ArrowUp', 'PageUp', 'Home'].includes(e.key) || (e.key === ' ' && e.shiftKey)) release();
  }
  function onPointer(e) {
    // A native scrollbar press has the scroll container itself as its target.
    // Release before its default action so no queued pin fights the drag.
    if (e.target === node && e.pointerType !== 'touch') release();
  }
  function listen(target, type, handler) {
    if (!target) return;
    target.addEventListener(type, handler, { passive: true });
    cleanups.push(() => target.removeEventListener(type, handler));
  }
  listen(node, 'scroll', onScroll);
  listen(node, 'wheel', onWheel);
  listen(node, 'touchstart', onTouchStart);
  listen(node, 'touchmove', onTouchMove);
  listen(node, 'keydown', onKey);
  listen(node, 'pointerdown', onPointer);
  listen(viewport, 'resize', resized);
  const observer = new Observer(resized);
  observer.observe(node);
  observer.observe(content);
  return {
    anchor, release,
    destroy() {
      destroyed = true;
      cancel();
      observer.disconnect();
      cleanups.forEach((off) => off());
    },
  };
}
