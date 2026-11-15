// A small QR code encoder for the pages (no dependency, no network): byte mode, error correction level L,
// versions 1 to 6 (up to 134 bytes), fixed mask 0. Enough for a link to this gateway. Checked by decoding its
// output with OpenCV's QR detector for inputs from 1 to 134 bytes, the gateway's own links among them.
// QR.matrix(text) -> array of rows of booleans (true = dark), or null when the text is too long.
// QR.svg(text, {dark, light, border}) -> an <svg> string, or "" when the text is too long.
"use strict";
const QR = (() => {
  const DATA = [19, 34, 55, 80, 108, 136];   // data codewords per version, level L
  const EC = [7, 10, 15, 20, 26, 18];        // error correction codewords per block
  const BLOCKS = [1, 1, 1, 1, 1, 2];

  // Reed-Solomon over GF(256), polynomial 0x11d
  const EXP = new Uint8Array(512), LOG = new Uint8Array(256);
  for (let i = 0, x = 1; i < 255; i++) { EXP[i] = x; LOG[x] = i; x <<= 1; if (x & 0x100) x ^= 0x11d; }
  for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
  const mul = (a, b) => (a && b ? EXP[LOG[a] + LOG[b]] : 0);
  function generator(deg) {
    let g = [1];
    for (let i = 0; i < deg; i++) {
      const n = new Array(g.length + 1).fill(0);
      for (let j = 0; j < g.length; j++) { n[j] ^= g[j]; n[j + 1] ^= mul(g[j], EXP[i]); }
      g = n;
    }
    return g;  // highest degree first, g[0] = 1
  }
  function remainder(data, deg) {
    const g = generator(deg), r = new Array(deg).fill(0);
    for (const b of data) {
      const f = b ^ r.shift();
      r.push(0);
      for (let i = 0; i < deg; i++) r[i] ^= mul(g[i + 1], f);
    }
    return r;
  }

  function matrix(text) {
    const bytes = [...new TextEncoder().encode(text)];
    const v = DATA.findIndex((cap) => bytes.length <= cap - 2) + 1;  // 4 bits mode + 8 bits count + data + terminator
    if (!v) return null;
    const cap = DATA[v - 1];
    // bit stream
    const bits = [];
    const put = (val, n) => { for (let i = n - 1; i >= 0; i--) bits.push((val >>> i) & 1); };
    put(4, 4); put(bytes.length, 8); bytes.forEach((b) => put(b, 8));
    for (let i = 0; i < 4 && bits.length < cap * 8; i++) bits.push(0);
    while (bits.length % 8) bits.push(0);
    const cw = [];
    for (let i = 0; i < bits.length; i += 8) cw.push(parseInt(bits.slice(i, i + 8).join(""), 2));
    for (let p = 0; cw.length < cap; p ^= 1) cw.push(p ? 0x11 : 0xec);
    // blocks, error correction, interleaving
    const nb = BLOCKS[v - 1], per = cap / nb, blocks = [], ecs = [];
    for (let b = 0; b < nb; b++) { const d = cw.slice(b * per, (b + 1) * per); blocks.push(d); ecs.push(remainder(d, EC[v - 1])); }
    const all = [];
    for (let i = 0; i < per; i++) for (const b of blocks) all.push(b[i]);
    for (let i = 0; i < EC[v - 1]; i++) for (const e of ecs) all.push(e[i]);

    const n = 17 + 4 * v;
    const m = Array.from({ length: n }, () => new Array(n).fill(false));
    const fn = Array.from({ length: n }, () => new Array(n).fill(false));
    const set = (x, y, dark) => { m[y][x] = dark; fn[y][x] = true; };
    // timing
    for (let i = 0; i < n; i++) { set(6, i, i % 2 === 0); set(i, 6, i % 2 === 0); }
    // finders with separators
    const finder = (cx, cy) => {
      for (let dy = -4; dy <= 4; dy++) for (let dx = -4; dx <= 4; dx++) {
        const x = cx + dx, y = cy + dy;
        if (x < 0 || y < 0 || x >= n || y >= n) continue;
        const d = Math.max(Math.abs(dx), Math.abs(dy));
        set(x, y, d !== 2 && d !== 4);
      }
    };
    finder(3, 3); finder(n - 4, 3); finder(3, n - 4);
    // one alignment pattern from version 2 up
    if (v >= 2) for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) set(n - 7 + dx, n - 7 + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
    // format information: level L (01), mask 0
    const data = (1 << 3) | 0;
    let rem = data;
    for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
    const fmt = ((data << 10) | rem) ^ 0x5412;
    const bit = (i) => ((fmt >>> i) & 1) === 1;
    for (let i = 0; i <= 5; i++) set(8, i, bit(i));
    set(8, 7, bit(6)); set(8, 8, bit(7)); set(7, 8, bit(8));
    for (let i = 9; i < 15; i++) set(14 - i, 8, bit(i));
    for (let i = 0; i < 8; i++) set(n - 1 - i, 8, bit(i));
    for (let i = 8; i < 15; i++) set(8, n - 15 + i, bit(i));
    set(8, n - 8, true);
    // data, zigzag from the bottom right, then mask 0
    let k = 0;
    for (let right = n - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5;
      for (let vert = 0; vert < n; vert++) for (let j = 0; j < 2; j++) {
        const x = right - j, up = ((right + 1) & 2) === 0, y = up ? n - 1 - vert : vert;
        if (fn[y][x]) continue;
        const dark = k < all.length * 8 ? ((all[k >>> 3] >>> (7 - (k & 7))) & 1) === 1 : false;
        k++;
        m[y][x] = dark !== ((x + y) % 2 === 0);
      }
    }
    return m;
  }

  function svg(text, { dark = "#141412", light = "#ffffff", border = 2 } = {}) {
    const m = matrix(text);
    if (!m) return "";
    const n = m.length, s = n + 2 * border;
    let d = "";
    for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) if (m[y][x]) d += `M${x + border} ${y + border}h1v1h-1z`;
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${s} ${s}" shape-rendering="crispEdges" role="img" aria-label="QR code"><rect width="${s}" height="${s}" fill="${light}"/><path d="${d}" fill="${dark}"/></svg>`;
  }
  return { matrix, svg };
})();
if (typeof module !== "undefined") module.exports = QR;
