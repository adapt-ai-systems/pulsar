// Pulsar cat — procedural, canvas-rendered, skeletal side-scroller cat that
// lives on the UI's lines. Usage: const cat = PulsarCat(overlayEl, {ledges, scale, tone})
// ledges: [{sel, edge: 'top'|'bottom'}] — the DOM lines she walks on.
  const TAU = Math.PI * 2;
  const lerp = (a, b, t) => a + (b - a) * t;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const rnd = i => { const x = Math.sin(i * 127.1 + 311.7) * 43758.5453; return x - Math.floor(x); };
  const smooth = (dt, k) => 1 - Math.exp(-dt * k);

  // ---- palette: near-black, long-haired ----
  const C0 = {
    base: '#0b0b0d', far: '#040405', hairDark: '#08080a', hairMid: '#131316',
    hairLight: '#1f2026', rim: 'rgba(130,138,160,0.28)', inside: 'rgba(60,62,72,0.22)',
    ear: '#2a1a1f', nose: '#2c1d22', iris0: '#f2c14e', iris1: '#9b6a10', whisker: 'rgba(225,225,235,0.55)',
  };

  // Pose library. Body frame: x forward, y down, ground at y=0. Units ~ 1px at scale 1.
  // tb = tail base angle, tk = tail curl per segment, eye = open fraction.
  const POSES = {
    stand:   { hip: [-20, -25], sh: [18, -27], head: [31, -40], ha: 0,    sag: 3,  hf: [-24, 0], hn: [-16, 0], ff: [14, 0], fn: [22, 0], meta: [-5, -8],  tb: 3.95, tk: -0.10, eye: 1,  tf: 0 },
    sit:     { hip: [-12, -12], sh: [6, -35],   head: [11, -51], ha: -0.1, sag: -2, hf: [-1, 0],  hn: [3, 0],   ff: [5, 0],  fn: [10, 0], meta: [-11, -1], tb: 2.30, tk: -0.14, eye: 1,  tf: 1 },
    groom:   { hip: [-12, -12], sh: [6, -34],   head: [14, -45], ha: 0.55, sag: -2, hf: [-1, 0],  hn: [3, 0],   ff: [5, 0],  fn: [18, -31], meta: [-11, -1], tb: 2.30, tk: -0.14, eye: 0.3, tf: 1 },
    lay:     { hip: [-18, -11], sh: [14, -12],  head: [27, -27], ha: 0,    sag: 0,  hf: [-4, 0],  hn: [0, 0],   ff: [30, 0], fn: [35, 0], meta: [-11, -1], tb: 2.95, tk: -0.035, eye: 1, tf: 0 },
    sleep:   { hip: [-16, -10], sh: [12, -10],  head: [25, -11], ha: 0.45, sag: 1,  hf: [-6, 0],  hn: [-2, 0],  ff: [21, 0], fn: [25, 0], meta: [-11, -1], tb: 2.35, tk: -0.21, eye: 0,  tf: 1 },
    crouch:  { hip: [-18, -16], sh: [16, -17],  head: [30, -27], ha: 0.05, sag: 1,  hf: [-20, 0], hn: [-14, 0], ff: [14, 0], fn: [19, 0], meta: [-8, -4],  tb: 3.25, tk: 0.0,   eye: 1,  tf: 0 },
    jump:    { hip: [-23, -27], sh: [22, -29],  head: [36, -36], ha: -0.1, sag: -1, hf: [-46, -14], hn: [-42, -10], ff: [42, -18], fn: [47, -14], meta: [-9, -2], tb: 3.15, tk: 0.02, eye: 1, tf: 0 },
    stretch: { hip: [-20, -31], sh: [16, -12],  head: [31, -17], ha: 0.15, sag: 6,  hf: [-24, 0], hn: [-18, 0], ff: [37, 0], fn: [43, 0], meta: [-6, -9],  tb: 4.25, tk: -0.05, eye: 0.4, tf: 0 },
  };
  const KEYS = Object.keys(POSES.stand);
  const clonePose = p => { const o = {}; for (const k of KEYS) o[k] = Array.isArray(p[k]) ? p[k].slice() : p[k]; return o; };

  const STRIDE = 20, LIFT = 7, TAIL_N = 16, TAIL_SEG = 3.9;

  function ik(ax, ay, tx, ty, l1, l2, bend) {
    let dx = tx - ax, dy = ty - ay, d = Math.hypot(dx, dy);
    d = clamp(d, Math.abs(l1 - l2) + 0.01, l1 + l2 - 0.01);
    const a = Math.acos(clamp((l1 * l1 + d * d - l2 * l2) / (2 * l1 * d), -1, 1));
    const base = Math.atan2(dy, dx) + bend * a;
    return [ax + l1 * Math.cos(base), ay + l1 * Math.sin(base)];
  }
  const rot = (x, y, a) => [x * Math.cos(a) - y * Math.sin(a), x * Math.sin(a) + y * Math.cos(a)];

  const hx = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
  const mix = (a, b, t) => { const A = hx(a), B = hx(b); return '#' + A.map((v, i) => Math.round(lerp(v, B[i], t)).toString(16).padStart(2, '0')).join(''); };
  function palette(t, flank) {   // t: 0 = pure black … 1 = grey
    return Object.assign({}, C0, {
      base: mix('#0b0b0d', '#55575e', t), far: mix('#040405', '#34353a', t), hairDark: mix('#08080a', '#43454b', t),
      hairMid: mix('#131316', '#686a72', t), hairLight: mix('#1f2026', '#8c8e97', t), farLight: mix('#101013', '#45474d', t),
      flankA: flank, greyS: `rgba(${hx(mix('#808290', '#b4b6c0', t)).join(',')},${0.55 * flank})`,
      flankC: hx(mix('#6e7078', '#a0a2ab', t)).join(','),
    });
  }
  export function PulsarCat(app, opt = {}) {
    const C = palette(opt.tone ?? 0, opt.flank ?? 1);
    const S0 = opt.scale || 0.85;
    const cv = document.createElement('canvas');
    cv.style.cssText = 'position:absolute;inset:0;pointer-events:none;z-index:5';
    app.appendChild(cv);
    const ctx = cv.getContext('2d');
    let dpr = 1, W = 0, H = 0, ledges = [];

    function measure() {
      dpr = window.devicePixelRatio || 1; W = app.clientWidth; H = app.clientHeight;
      cv.width = W * dpr; cv.height = H * dpr; cv.style.width = W + 'px'; cv.style.height = H + 'px';
      const a = app.getBoundingClientRect(), cur = ledges[st.l] && ledges[st.l].el;
      const next = [];
      for (const d of opt.ledges || []) for (const e of document.querySelectorAll(d.sel)) {
        const r = e.getBoundingClientRect();
        if (!r.width || !e.offsetParent) continue;
        const y = (d.edge === 'top' ? r.top : r.bottom) - a.top;
        const x0 = Math.max(r.left - a.left, 0) + 55 * st.S, x1 = Math.min(r.right - a.left, W) - 55 * st.S;
        if (x1 - x0 > 40 && y > 70 * st.S && y < H) next.push({ el: e, y, x0, x1, e0: r.left - a.left, e1: r.right - a.left });
      }
      if (!next.length) next.push({ el: null, y: H - 2, x0: 60, x1: W - 60, e0: 0, e1: W });
      ledges = next;
      const i = cur ? ledges.findIndex(l => l.el === cur) : -1;
      if (i >= 0) { st.l = i; if (!st.fly) { st.y = ledges[i].y; if (!st.offstage && (st.x < ledges[i].x0 || st.x > ledges[i].x1)) { st.x = clamp(st.x, ledges[i].x0, ledges[i].x1); } } }
      else if (st.l != null && !st.fly && !st.away) {           // her ledge vanished: land on the nearest one
        let best = 0; for (let k = 1; k < ledges.length; k++) if (Math.abs(ledges[k].y - st.y) < Math.abs(ledges[best].y - st.y)) best = k;
        st.l = best; st.y = ledges[best].y; st.x = clamp(st.x, ledges[best].x0, ledges[best].x1);
      }
    }

    const st = {
      S: S0, x: 0, y: 0, l: 0, dir: 1, sx: 1, rotA: 0,
      pose: 'sit', cur: clonePose(POSES.sit), k: 4,
      walkW: 0, target: null, phase: 0, fly: null,
      look: 0, lookT: 0, lookTimer: 2, ear: 0, earTimer: 3, blink: 0, blinkTimer: 2,
      flick: 0, flickTimer: 4, purr: 0, t: 0,
      tail: new Array(TAIL_N).fill(0).map((_, i) => POSES.sit.tb + POSES.sit.tk * i),
    };
    st.l = null; measure();
    window.addEventListener('resize', measure);
    const remeasure = setInterval(measure, 1200);
    st.l = 0; st.x = (ledges[0].x0 + ledges[0].x1) / 2; st.y = ledges[0].y;

    // ---------- skeleton → circles ----------
    function skeleton() {
      const c = st.cur, t = st.t, w = st.walkW;
      const breath = 1 + Math.sin(t * TAU / (st.pose === 'sleep' ? 4.2 : 3.0)) * (st.pose === 'sleep' ? 0.035 : 0.018) + st.purr * 0.01 * Math.sin(t * 60);
      const ph = st.phase, bob = Math.sin(ph * TAU * 2) * 1.1 * w;
      const hip = [c.hip[0], c.hip[1] + bob], sh = [c.sh[0], c.sh[1] + Math.sin((ph + 0.25) * TAU * 2) * 0.9 * w];
      let ha = c.ha + st.look;
      if (st.pose === 'groom') ha += Math.sin(t * 7) * 0.12;
      const head = [c.head[0], c.head[1] + Math.sin((ph + 0.1) * TAU * 2) * 0.5 * w];

      const groups = { far: [], tail: [], body: [], near: [] };
      const add = (g, x, y, r, fx, fy, len, sd, side) => groups[g].push({ x, y, r, fx, fy, len, sd, side: side ?? (sd >= 100 && sd <= 121) });

      // body spine (quadratic bezier hip→shoulder)
      const cx = (hip[0] + sh[0]) / 2, cy = (hip[1] + sh[1]) / 2 + c.sag;
      let fdx = hip[0] - sh[0], fdy = hip[1] - sh[1]; const fl = Math.hypot(fdx, fdy) || 1; fdx /= fl; fdy /= fl;
      for (let i = 0; i <= 10; i++) {
        const u = i / 10, a = (1 - u) * (1 - u), b = 2 * (1 - u) * u, d = u * u;
        const x = a * hip[0] + b * cx + d * sh[0], y = a * hip[1] + b * cy + d * sh[1];
        add('body', x, y + 1.5, lerp(11.5, 12.5, u) * breath, fdx, fdy + 0.35, 8.5, 100 + i);
      }
      add('body', hip[0] + 1, hip[1] + 2, 12.5 * breath, -0.3, 0.9, 9, 120);           // haunch / pants
      add('body', sh[0] + 4, sh[1] + 5, 10 * breath, -0.2, 1, 10.5, 121);            // chest ruff
      const nb = [sh[0] + 3, sh[1] - 3];
      for (let i = 0; i < 3; i++) { const u = (i + 1) / 4; add('body', lerp(nb[0], head[0], u), lerp(nb[1], head[1], u), lerp(9.5, 8.5, u), fdx, fdy, 8, 130 + i); }
      const R = (x, y) => { const p = rot(x, y, ha); return [head[0] + p[0], head[1] + p[1]]; };
      let p = R(0, 0); add('body', p[0], p[1], 9.5, -0.9, -0.2, 4.5, 140);
      p = R(-3, 4.5); add('body', p[0], p[1], 7.8, -0.6, 0.6, 8, 141);                   // cheek fluff
      p = R(7, 3.2); add('body', p[0], p[1], 5, -0.8, 0.3, 1.6, 142);                     // muzzle

      // legs
      const legs = {};
      const gait = (off) => {
        const q = ((ph + off) % 1 + 1) % 1;
        if (q < 0.6) return [(STRIDE / 2 - STRIDE * q / 0.6) * w, 0];
        const s = (q - 0.6) / 0.4, e = s * s * (3 - 2 * s);
        return [(-STRIDE / 2 + STRIDE * e) * w, -LIFT * Math.sin(Math.PI * s) * w];
      };
      const hind = (name, off, g, shade) => {
        const gg = gait(off), F = [c[name][0] + gg[0], c[name][1] + gg[1]];
        const Hk = [F[0] + c.meta[0], F[1] + c.meta[1]];
        const J = [hip[0] + 2, hip[1] + 3];
        const K = ik(J[0], J[1], Hk[0], Hk[1], 13, 13, -1);
        capsule(g, J, K, 7.5, 4.6, 5, 'leg' + name);
        capsule(g, K, Hk, 4.4, 3.2, 3, 'shin' + name);
        capsule(g, Hk, [F[0] + 1, F[1] - 2.6], 3.2, 3.2, 2, 'meta' + name);
        add(g, F[0] + 1.8, F[1] - 2.8, 3.4, 0.5, 0.5, 1.8, name.charCodeAt(1) * 7);
        legs[name] = { F, shade };
      };
      const fore = (name, off, g, shade) => {
        const gg = gait(off), F = [c[name][0] + gg[0], c[name][1] + gg[1]];
        const Wr = [F[0] - 0.5, F[1] - 3.2];
        const J = [sh[0] + 2, sh[1] + 3];
        const E = ik(J[0], J[1], Wr[0], Wr[1], 12.5, 12.5, 1);
        capsule(g, J, E, 6.5, 4, 4, 'arm' + name);
        capsule(g, E, Wr, 3.8, 3.1, 4, 'fore' + name);
        add(g, F[0] + 1.6, F[1] - 2.9, 3.3, 0.6, 0.4, 1.8, name.charCodeAt(1) * 11);
        legs[name] = { F, shade };
      };
      function capsule(g, A, B, r0, r1, n, key) {
        let fx = B[0] - A[0], fy = B[1] - A[1]; const l = Math.hypot(fx, fy) || 1; fx /= l; fy /= l;
        for (let i = 0; i <= n; i++) { const u = i / n; add(g, lerp(A[0], B[0], u), lerp(A[1], B[1], u), lerp(r0, r1, u), fx, fy, lerp(6, 2.5, u), key.length * 31 + key.charCodeAt(key.length - 1) * 3 + i, (key.startsWith('leg') || key.startsWith('arm')) && u < 0.7); }
      }
      hind('hf', 0.5, 'far', 1); fore('ff', 0.75, 'far', 1);
      hind('hn', 0.0, 'near', 0); fore('fn', 0.25, 'near', 0);

      // tail chain
      const tb = [hip[0] - 10, hip[1] - 3];
      let tx = tb[0], ty = tb[1];
      const pts = [[tx, ty]];
      for (let i = 0; i < TAIL_N; i++) {
        const a = st.tail[i];
        tx += Math.cos(a) * TAIL_SEG; ty += Math.sin(a) * TAIL_SEG;
        if (ty > -2.5) ty = -2.5;
        pts.push([tx, ty]);
      }
      for (let i = 0; i < pts.length - 1; i++) {
        const u = i / TAIL_N, A = pts[i], B = pts[i + 1];
        let fx = B[0] - A[0], fy = B[1] - A[1]; const l = Math.hypot(fx, fy) || 1; fx /= l; fy /= l;
        const r = (u < 0.5 ? lerp(3.4, 4.6, u * 2) : lerp(4.6, 2.6, (u - 0.5) * 2));
        add('tail', A[0], A[1], r, fx, fy, lerp(7, 10, u), 200 + i);
        add('tail', (A[0] + B[0]) / 2, (A[1] + B[1]) / 2, r, fx, fy, lerp(7, 10, u), 230 + i);
      }
      return { groups, hip, sh, head, ha, legs, pts };
    }

    // ---------- fur ----------
    function furPass(circles, others, fill, isFar, flank) {
      ctx.beginPath();
      for (const c of circles) { ctx.moveTo(c.x + c.r, c.y); ctx.arc(c.x, c.y, c.r, 0, TAU); }
      ctx.fillStyle = fill; ctx.fill();
      if (flank) {                                   // grey side patch
        ctx.save(); ctx.clip();
        for (const c of circles) if (c.side) {
          const g = ctx.createRadialGradient(c.x, c.y - c.r * 0.1, 0, c.x, c.y - c.r * 0.1, c.r * 1.05);
          g.addColorStop(0, `rgba(${C.flankC},${0.17 * C.flankA})`); g.addColorStop(0.6, `rgba(${C.flankC},${0.09 * C.flankA})`); g.addColorStop(1, `rgba(${C.flankC},0)`);
          ctx.fillStyle = g; ctx.fillRect(c.x - c.r * 1.1, c.y - c.r * 1.1, c.r * 2.2, c.r * 2.2);
        }
        ctx.restore();
      }
      const grey = new Path2D();
      const dark = new Path2D(), mid = new Path2D(), light = new Path2D(), rim = new Path2D(), inner = new Path2D();
      const all = others ? circles.concat(others) : circles, t = st.t, wind = st.walkW * 0.25 + 0.08;
      for (let ci = 0; ci < circles.length; ci++) {
        const c = circles[ci], n = Math.ceil(c.r * 3.2);
        for (let k = 0; k < n; k++) {
          const sd = c.sd * 97 + k * 13.37, th = (k + rnd(sd) * 0.8) / n * TAU;
          const nx = Math.cos(th), ny = Math.sin(th);
          const px = c.x + nx * c.r, py = c.y + ny * c.r;
          let hidden = false;
          for (const o of all) { if (o !== c && (px - o.x) ** 2 + (py - o.y) ** 2 < (o.r - 0.6) ** 2) { hidden = true; break; } }
          if (hidden) continue;
          for (let layer = 0; layer < 2; layer++) {
            const s2 = sd + layer * 5.1, L = c.len * (0.55 + rnd(s2 + 1) * 0.75);
            const sway = Math.sin(t * 1.8 + rnd(s2 + 2) * 9) * wind;
            let dx = nx * 0.55 + c.fx * 0.9 + sway * -ny, dy = ny * 0.55 + c.fy * 0.9 + sway * nx;
            const dl = Math.hypot(dx, dy) || 1; dx /= dl; dy /= dl;
            const sx = px - nx * 2, sy = py - ny * 2;
            const ex = px + dx * L, ey = py + dy * L;
            const curl = (rnd(s2 + 3) - 0.5) * L * 0.5;
            const qx = px + dx * L * 0.45 + -dy * curl, qy = py + dy * L * 0.45 + dx * curl;
            const side = flank && c.side;
            const tgt = ny < -0.45 ? (layer ? rim : side ? grey : light) : ny < 0.2 ? (side ? grey : mid) : dark;
            tgt.moveTo(sx, sy); tgt.quadraticCurveTo(qx, qy, ex, ey);
          }
        }
        if (!isFar && c.r > 4) {                    // interior sheen strands
          for (let k = 0; k < (flank && c.side ? 9 : 3); k++) {
            const s3 = c.sd * 53 + k * 7.7, a = rnd(s3) * TAU, rr = Math.sqrt(rnd(s3 + 1)) * c.r * 0.8;
            const ix = c.x + Math.cos(a) * rr, iy = c.y + Math.sin(a) * rr, L = 5 + rnd(s3 + 2) * 5;
            if (iy > c.y + c.r * 0.2) continue;
            (flank && c.side ? grey : inner).moveTo(ix, iy); inner.quadraticCurveTo(ix + c.fx * L * 0.5 + 1, iy + c.fy * L * 0.5 - 0.6, ix + c.fx * L, iy + c.fy * L);
          }
        }
      }
      ctx.lineCap = 'round';
      ctx.lineWidth = 1.2; ctx.strokeStyle = isFar ? C.far : C.hairDark; ctx.stroke(dark);
      ctx.lineWidth = 1.0; ctx.strokeStyle = isFar ? C.far : C.hairMid; ctx.stroke(mid);
      ctx.lineWidth = 0.8; ctx.strokeStyle = isFar ? C.farLight : C.hairLight; ctx.stroke(light);
      if (!isFar) { ctx.lineWidth = 0.5; ctx.strokeStyle = C.rim; ctx.stroke(rim); ctx.lineWidth = 0.9; ctx.strokeStyle = C.inside; ctx.stroke(inner); ctx.lineWidth = 0.9; ctx.strokeStyle = C.greyS; ctx.stroke(grey); }
    }

    // ---------- face ----------
    function face(sk) {
      const { head, ha } = sk, t = st.t;
      const R = (x, y) => { const p = rot(x, y, ha); return [head[0] + p[0], head[1] + p[1]]; };
      // ears (near), with twitch
      const tw = st.ear * Math.sin(t * 40) * 0.25 + (st.purr ? -0.15 : 0);
      const ear = (bx, by, tx2, ty2, cx2, cy2, col, inner) => {
        const b1 = R(bx, by), tip = R(tx2 + tw * 6, ty2 + Math.abs(tw) * 2), b2 = R(cx2, cy2);
        ctx.beginPath(); ctx.moveTo(b1[0], b1[1]); ctx.quadraticCurveTo((b1[0] + tip[0]) / 2 - 1.5, (b1[1] + tip[1]) / 2, tip[0], tip[1]);
        ctx.quadraticCurveTo((b2[0] + tip[0]) / 2 + 1.5, (b2[1] + tip[1]) / 2, b2[0], b2[1]); ctx.closePath();
        ctx.fillStyle = col; ctx.fill();
        if (inner) {
          const i1 = R(bx + 2.5, by - 1), i2 = R(cx2 - 2, cy2 - 1.5), it = R(tx2 + tw * 6 + 0.6, ty2 + 4);
          ctx.beginPath(); ctx.moveTo(i1[0], i1[1]); ctx.lineTo(it[0], it[1]); ctx.lineTo(i2[0], i2[1]); ctx.closePath(); ctx.fillStyle = C.ear; ctx.fill();
          // tufts
          ctx.strokeStyle = 'rgba(170,170,185,0.35)'; ctx.lineWidth = 0.5; ctx.beginPath();
          for (let k = 0; k < 5; k++) { const a = R(bx + 3 + k * 0.6, by - 2 - k * 1.3), b = R(bx + 5 + k * 0.4, by - 6 - k * 1.8); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); }
          ctx.stroke();
          // tip tuft (lynx-ish)
          ctx.strokeStyle = C.hairLight; ctx.lineWidth = 0.6; ctx.beginPath(); ctx.moveTo(tip[0], tip[1]); ctx.lineTo(tip[0] + 0.5, tip[1] - 3.5); ctx.stroke();
        }
      };
      ear(-8.5, -4, -4, -20, 0, -8.5, C.base, true);

      // eye
      const blink = st.blink > 0 ? Math.sin(Math.PI * (1 - st.blink / 0.16)) : 0;
      const open = clamp(st.cur.eye * (1 - blink) * (st.purr ? 0.25 : 1), 0, 1);
      const e = R(5.2, -1.6);
      if (open < 0.12) {
        ctx.strokeStyle = '#000'; ctx.lineWidth = 1; ctx.beginPath();
        const a = R(2.6, -1), b = R(5.5, 0.3), d = R(8, -1.2);
        ctx.moveTo(a[0], a[1]); ctx.quadraticCurveTo(b[0], b[1], d[0], d[1]); ctx.stroke();
        ctx.strokeStyle = C.hairLight; ctx.lineWidth = 0.4; ctx.beginPath(); ctx.moveTo(a[0], a[1] - 0.6); ctx.quadraticCurveTo(b[0], b[1] - 0.6, d[0], d[1] - 0.6); ctx.stroke();
      } else {
        ctx.save(); ctx.translate(e[0], e[1]); ctx.rotate(ha - 0.18);
        ctx.beginPath(); ctx.ellipse(0, 0, 3.1, 2.9 * open, 0, 0, TAU); ctx.fillStyle = '#000'; ctx.fill();
        ctx.save(); ctx.clip();
        const g = ctx.createRadialGradient(-0.5, -0.6, 0.3, 0, 0, 3.2); g.addColorStop(0, C.iris0); g.addColorStop(0.7, '#d99a22'); g.addColorStop(1, C.iris1);
        ctx.beginPath(); ctx.ellipse(0, 0, 2.7, 2.6, 0, 0, TAU); ctx.fillStyle = g; ctx.fill();
        const pw = st.pose === 'lay' || st.purr ? 1.4 : 0.75;
        ctx.beginPath(); ctx.ellipse(0.5, 0, pw, 2.4, 0, 0, TAU); ctx.fillStyle = '#000'; ctx.fill();
        ctx.beginPath(); ctx.arc(-0.6, -1.1, 0.65, 0, TAU); ctx.fillStyle = 'rgba(255,255,255,0.9)'; ctx.fill();
        ctx.beginPath(); ctx.arc(1.2, 1, 0.3, 0, TAU); ctx.fillStyle = 'rgba(255,255,255,0.5)'; ctx.fill();
        ctx.restore();
        ctx.beginPath(); ctx.ellipse(0, 0, 3.1, 2.9 * open, 0, Math.PI * 1.05, Math.PI * 1.95); ctx.strokeStyle = '#000'; ctx.lineWidth = 0.9; ctx.stroke();
        ctx.restore();
      }
      // nose + mouth
      const n = R(12.4, 1.2);
      ctx.beginPath(); ctx.moveTo(n[0] - 0.3, n[1] - 1); ctx.lineTo(n[0] + 1.1, n[1] + 0.2); ctx.lineTo(n[0] - 0.6, n[1] + 0.9); ctx.closePath(); ctx.fillStyle = C.nose; ctx.fill();
      const m0 = R(11.5, 2.4), m1 = R(10.6, 4.8), m2 = R(8.4, 5.4);
      ctx.beginPath(); ctx.moveTo(m0[0], m0[1]); ctx.lineTo(m1[0], m1[1]); ctx.quadraticCurveTo(m1[0] - 1, m1[1] + 0.8, m2[0], m2[1]);
      ctx.strokeStyle = '#000'; ctx.lineWidth = 0.6; ctx.stroke();
      if (st.pose === 'groom') {   // little pink tongue flick
        const tl = Math.max(0, Math.sin(t * 7)), tp = R(11.2 + tl * 1.6, 5 + tl * 0.4);
        ctx.beginPath(); ctx.ellipse(tp[0], tp[1], 1.1 * tl + 0.1, 0.6, ha, 0, TAU); ctx.fillStyle = '#b25c6a'; ctx.fill();
      }
      // whiskers
      const sw = Math.sin(t * 1.3) * 0.6 + st.walkW * Math.sin(st.phase * TAU * 2) * 0.4;
      ctx.strokeStyle = C.whisker; ctx.lineWidth = 0.32; ctx.beginPath();
      for (let k = 0; k < 5; k++) {
        const a = R(9.5, 2.6 + k * 0.6), c2 = R(17 + k, 0.5 + k * 1.6 + sw), b = R(24 - k * 0.8, 1 + k * 3.2 + sw * 1.5);
        ctx.moveTo(a[0], a[1]); ctx.quadraticCurveTo(c2[0], c2[1], b[0], b[1]);
      }
      const b0 = R(5, -4.5), b1 = R(9, -10 + sw), b2 = R(12, -13 + sw);
      ctx.moveTo(b0[0], b0[1]); ctx.quadraticCurveTo(b1[0], b1[1], b2[0], b2[1]);
      ctx.stroke();
    }

    function farEar(sk) {
      const { head, ha } = sk;
      const R = (x, y) => { const p = rot(x, y, ha); return [head[0] + p[0], head[1] + p[1]]; };
      const a = R(-1, -8), tp = R(4.5, -19.5 + st.ear * Math.sin(st.t * 37) * 1.5), b = R(7, -5);
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.quadraticCurveTo(tp[0] - 2, tp[1] + 4, tp[0], tp[1]); ctx.quadraticCurveTo(tp[0] + 2, tp[1] + 6, b[0], b[1]); ctx.closePath();
      ctx.fillStyle = C.far; ctx.fill();
    }

    // ---------- draw ----------
    function draw() {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      if (st.away) return;
      const S = st.S, airborne = st.fly ? st.fly.h : 0;
      // ground shadow
      const gy = st.fly ? st.fly.gy : st.y;
      const sh = ctx.createRadialGradient(st.x, gy, 1, st.x, gy, 46 * S);
      const alpha = 0.55 * (1 - clamp(airborne / 120, 0, 0.8));
      sh.addColorStop(0, `rgba(0,0,0,${alpha})`); sh.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.save(); ctx.translate(st.x, gy); ctx.scale(1, 0.18); ctx.translate(-st.x, -gy);
      ctx.fillStyle = sh; ctx.beginPath(); ctx.arc(st.x, gy, 46 * S, 0, TAU); ctx.fill(); ctx.restore();

      const sk = skeleton();
      ctx.save();
      ctx.translate(st.x, st.y); ctx.rotate(st.rotA); ctx.scale(S * st.sx, S);
      const G = sk.groups;
      furPass(G.far, null, C.far, true);
      farEar(sk);
      if (!st.cur.tf || st.cur.tf < 0.5) furPass(G.tail, null, C.base, false);
      furPass(G.body, null, C.base, false, true);
      furPass(G.near, G.body, C.base, false, true);
      if (st.cur.tf >= 0.5) furPass(G.tail, null, C.base, false);
      face(sk);
      ctx.restore();
    }

    // ---------- simulation ----------
    let last = performance.now();
    let alive = true;
    function frame(now) {
      if (!alive) return;
      if (document.hidden) { last = now; requestAnimationFrame(frame); return; }
      const dt = Math.min(0.05, (now - last) / 1000); last = now; st.t += dt;
      const VMAX = 46 * st.S / 0.85;

      // walking
      if (st.target != null && ledges[st.l] && !st.offstage) st.target = clamp(st.target, ledges[st.l].x0, ledges[st.l].x1);
      if (st.target != null) {
        const d = st.target - st.x, want = Math.abs(d) > 1.5 ? clamp(Math.abs(d) / 45, 0.2, 1) : 0;
        st.walkW = lerp(st.walkW, want, smooth(dt, 3.5));
        const v = VMAX * st.walkW * dt;
        st.x += Math.sign(d) * Math.min(Math.abs(d), v);
        st.phase += dt * VMAX * 0.6 / (STRIDE * st.S);
        if (Math.abs(d) < 0.6 && st.walkW < 0.04) { st.walkW = 0; st.target = null; st.onArrive && st.onArrive(); }
      } else st.walkW = lerp(st.walkW, 0, smooth(dt, 6));

      // flight
      if (st.fly) {
        const f = st.fly; f.t += dt; const p = clamp(f.t / f.T, 0, 1);
        const x = lerp(f.x0, f.x1, p), y = (1 - p) * (1 - p) * f.y0 + 2 * (1 - p) * p * f.peak + p * p * f.y1;
        const p2 = clamp(p + 0.02, 0, 1), y2 = (1 - p2) * (1 - p2) * f.y0 + 2 * (1 - p2) * p2 * f.peak + p2 * p2 * f.y1;
        st.rotA = clamp(Math.atan2(y2 - y, (f.x1 - f.x0) * 0.02) * 0.45, -0.5, 0.5) * st.dir;
        st.x = x; st.y = y; f.h = Math.max(0, lerp(f.y0, f.y1, p) - y);
        f.gy = p < 0.5 ? f.y0 : f.y1;
        if (p >= 1) { st.fly = null; st.rotA = 0; f.done(); }
      } else st.rotA = lerp(st.rotA, 0, smooth(dt, 10));

      // turning
      st.sx = lerp(st.sx, st.dir, smooth(dt, 7));

      // pose blending
      const tgt = POSES[st.pose], k = smooth(dt, st.k);
      for (const key of KEYS) {
        if (Array.isArray(tgt[key])) { st.cur[key][0] = lerp(st.cur[key][0], tgt[key][0], k); st.cur[key][1] = lerp(st.cur[key][1], tgt[key][1], k); }
        else st.cur[key] = lerp(st.cur[key], tgt[key], k);
      }

      // tail: per-segment lag gives follow-through
      const idle = st.walkW < 0.1 && !st.fly;
      const amp = st.pose === 'sleep' ? 0.03 : idle ? 0.07 + st.flick * 0.35 : 0.12;
      const om = st.pose === 'sleep' ? 0.6 : idle ? 1.4 + st.flick * 4 : 3.2;
      for (let i = 0; i < TAIL_N; i++) {
        const u = i / TAIL_N;
        const target = st.cur.tb + st.cur.tk * i + Math.sin(st.t * om - i * 0.38) * amp * (0.3 + u) - st.walkW * 0.012 * i;
        st.tail[i] = lerp(st.tail[i], target, smooth(dt, 9 - u * 6));
      }

      // idle life: look around, ear twitch, blink, tail flick
      st.lookTimer -= dt; if (st.lookTimer < 0) { st.lookTimer = 2 + Math.random() * 5; st.lookT = (st.pose === 'sit' || st.pose === 'lay' || st.pose === 'stand') && idle ? (Math.random() - 0.6) * 0.5 : 0; }
      st.look = lerp(st.look, st.lookT, smooth(dt, 3));
      st.earTimer -= dt; if (st.earTimer < 0) { st.earTimer = 3 + Math.random() * 7; st.ear = 1; } st.ear = Math.max(0, st.ear - dt * 4);
      st.blinkTimer -= dt; if (st.blinkTimer < 0) { st.blinkTimer = 2 + Math.random() * 5; st.blink = 0.16; } st.blink = Math.max(0, st.blink - dt);
      st.flickTimer -= dt; if (st.flickTimer < 0) { st.flickTimer = 4 + Math.random() * 8; st.flick = 1; } st.flick = Math.max(0, st.flick - dt * 0.8);
      st.purr = Math.max(0, st.purr - dt * 0.4);

      draw();
      requestAnimationFrame(frame);
    }

    // ---------- behaviours ----------
    let token = 0, timer = null;
    const wait = ms => new Promise(r => { timer = setTimeout(r, ms); });
    const setPose = (p, k = 4) => { st.pose = p; st.k = k; opt.onPose && opt.onPose(p); };
    async function face2(dx) { const d = dx < 0 ? -1 : 1; if (d !== st.dir) { st.dir = d; await wait(380); } }
    async function walkTo(x) {
      const L = ledges[st.l]; if (!st.offstage) x = clamp(x, L.x0, L.x1);
      if (Math.abs(x - st.x) < 4) return;
      if (st.pose !== 'stand') { setPose('stand', 4); await wait(700); }
      await face2(x - st.x);
      await new Promise(r => { st.onArrive = r; st.target = x; });
    }
    async function jumpTo(li) {
      const L = ledges[li], x = clamp(st.x + (Math.random() * 140 - 70) * st.S, L.x0, L.x1);
      if (st.pose !== 'stand') { setPose('stand', 4); await wait(600); }
      await face2(x - st.x);
      setPose('crouch', 6); await wait(520);
      setPose('jump', 14);
      const y0 = st.y, y1 = L.y, peak = Math.min(y0, y1) - 55 * st.S;
      await new Promise(r => { st.fly = { x0: st.x, x1: x, y0, y1, peak, t: 0, T: 0.62 + Math.abs(y1 - y0) / 900, h: 0, gy: y0, done: r }; });
      st.l = li; st.y = y1; setPose('crouch', 10); await wait(260); setPose('stand', 4); await wait(400);
    }
    function zz(ms) {
      return new Promise(async res => {
        const end = Date.now() + ms;
        while (Date.now() < end && st.pose === 'sleep') {
          const z = document.createElement('span'); z.className = 'cat-z'; z.textContent = 'z';
          z.style.left = (st.x + st.dir * 20 * st.S) + 'px'; z.style.top = (st.y - 30 * st.S) + 'px'; app.appendChild(z);
          setTimeout(() => z.remove(), 3000); await wait(1300);
        }
        res();
      });
    }
    // Visits: she is away most of the time (~90%) and pops in now and then.
    const visitMs = () => ((opt.visitMin ?? 2) + Math.random() * (opt.visitJitter ?? 2)) * 60000;
    const awayMs = () => ((opt.awayMin ?? 18) + Math.random() * (opt.awayJitter ?? 18)) * 60000;
    let visitEnd = 0;
    async function comeBack() {
      measure();
      const back = ledges.map((L, i) => i).filter(i => ledges[i].e0 <= 8 || ledges[i].e1 >= W - 8);
      const li = back.length ? back[Math.floor(Math.random() * back.length)] : 0, B = ledges[li];
      const fromR = B.e1 >= W - 8 && (B.e0 > 8 || Math.random() < .5);
      st.l = li; st.y = B.y; st.x = fromR ? W + 120 * st.S : -120 * st.S; st.dir = fromR ? -1 : 1; st.sx = st.dir;
      st.away = false; setPose('stand', 20);
      await walkTo(fromR ? B.x1 - Math.random() * (B.x1 - B.x0) * 0.5 : B.x0 + Math.random() * (B.x1 - B.x0) * 0.5);
      st.offstage = false; visitEnd = Date.now() + visitMs();
      setPose('stand'); await wait(1200);
    }
    const pick = () => { const r = Math.random(); return r < .36 ? 'walk' : r < .52 ? 'jump' : r < .64 ? 'sit' : r < .74 ? 'lay' : r < .84 ? 'sleep' : r < .92 ? 'groom' : 'stretch'; };
    async function life(force) {
      const my = ++token, ok = () => my === token;
      while (ok()) {
        const r = force || (Date.now() > visitEnd ? 'away' : pick()); force = null;
        if (r === 'walk') {
          // pick a spot on her line far enough away; head to the side with room
          const L = ledges[st.l], room = [st.x - L.x0, L.x1 - st.x], min = 40 * st.S;
          if (Math.max(room[0], room[1]) < min) { force = 'jump'; continue; }
          const side = room[0] < min ? 1 : room[1] < min ? -1 : (Math.random() < .5 ? -1 : 1);
          const span = side < 0 ? room[0] : room[1];
          await walkTo(st.x + side * (min + Math.random() * (span - min))); if (!ok()) return; setPose('stand'); await wait(900 + Math.random() * 1500); }
        else if (r === 'jump') {
          const o = ledges.map((_, i) => i).filter(i => i !== st.l && Math.abs(ledges[i].y - st.y) < 260 && ledges[i].y > 20);
          if (o.length) await jumpTo(o[Math.floor(Math.random() * o.length)]);
          else { setPose('sit'); await wait(3000); }
        }
        else if (r === 'away') {
          // wander off the side of the screen, stay away a while, come back on some line
          const exits = ledges.map((L, i) => i).filter(i => ledges[i].e0 <= 8 || ledges[i].e1 >= W - 8);
          if (exits.length) {
            if (!exits.includes(st.l)) { await jumpTo(exits[Math.floor(Math.random() * exits.length)]); if (!ok()) return; }
            const L = ledges[st.l], goR = L.e1 >= W - 8 && (L.e0 > 8 || st.x > W / 2);
            st.offstage = true;
            await walkTo(goR ? W + 120 * st.S : -120 * st.S);
            if (!ok()) { st.offstage = false; return; }
          }
          st.away = st.offstage = true; opt.onPose && opt.onPose('away');
          await wait(awayMs());
          if (!ok()) return;
          await comeBack(); if (!ok()) return;
        }
        else if (r === 'sit') { setPose('sit'); await wait(3500 + Math.random() * 4500); }
        else if (r === 'groom') { setPose('sit'); await wait(1000); setPose('groom', 3); await wait(4000 + Math.random() * 3000); setPose('sit'); await wait(1500); }
        else if (r === 'lay') { setPose('sit'); await wait(1100); setPose('lay', 2.5); await wait(6000 + Math.random() * 6000); }
        else if (r === 'sleep') { if (st.pose !== 'lay') { setPose('sit'); await wait(1000); setPose('lay', 2.5); await wait(2200); } setPose('sleep', 1.5); await zz(10000 + Math.random() * 10000); if (!ok()) return; setPose('lay', 2); await wait(1800); setPose('stretch', 2.5); await wait(2200); }
        else if (r === 'stretch') { setPose('stand'); await wait(700); setPose('stretch', 2.5); await wait(2400); setPose('stand', 3); await wait(800); }
      }
    }
    function interrupt(r) {
      clearTimeout(timer);
      visitEnd = Date.now() + visitMs();
      if (st.away || st.offstage) { st.away = st.offstage = false; const L = ledges[st.l] || ledges[0]; st.y = L.y; st.x = clamp(st.x, L.x0, L.x1); } st.target = null; st.onArrive = null; if (!st.fly) life(r); else { const f = st.fly.done; st.fly.done = () => { f(); life(r); }; } }

    const onTap = e => {
      if (st.away) return;
      const a = app.getBoundingClientRect(), x = e.clientX - a.left, y = e.clientY - a.top;
      if (Math.hypot(x - st.x, y - (st.y - 25 * st.S)) > 50 * st.S) return;
      st.purr = 4;
      for (let i = 0; i < 3; i++) setTimeout(() => {
        const h = document.createElement('span'); h.className = 'cat-heart'; h.textContent = i === 1 ? 'prrr' : '♥';
        h.style.left = (st.x - 6 + (i - 1) * 16) + 'px'; h.style.top = (st.y - 70 * st.S) + 'px'; app.appendChild(h); setTimeout(() => h.remove(), 1400);
      }, i * 180);
      if (st.pose === 'sleep') interrupt('sit');
    };
    window.addEventListener('pointerdown', onTap, true);

    requestAnimationFrame(frame);
    // start away: she shows up after a random part of an away spell
    st.away = st.offstage = true;
    (async () => { const my = token; await wait(Math.random() * awayMs()); if (my === token) { await comeBack(); if (my === token) life(); } })();
    return {
      do: interrupt, setScale: s => { st.S = s; measure(); }, state: st,
      destroy() { alive = false; token++; clearTimeout(timer); clearInterval(remeasure); window.removeEventListener('resize', measure); window.removeEventListener('pointerdown', onTap, true); cv.remove(); },
    };
  }
