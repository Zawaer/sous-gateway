// Deterministic card helpers. Every card sets window.DURATION and window.render(t); no timers, no CSS transitions.
const Q = new URLSearchParams(location.search);
function setup(defaultDur) {
  window.DURATION = parseFloat(Q.get('dur')) || defaultDur;
  // design times are written for the default duration; a longer/shorter render stretches the build-up
  window.K = window.DURATION / defaultDur;
}
const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
const ease = x => { x = clamp(x); return 1 - Math.pow(1 - x, 3); };
const easeInOut = x => { x = clamp(x); return x < .5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2; };
// eased progress of an animation that starts at `start` and lasts `dur` (both in design seconds, scaled by K)
function p(t, start, dur) { return ease((t - start * K) / (dur * K)); }
function lin(t, start, dur) { return clamp((t - start * K) / (dur * K)); }
function fadeUp(el, a, dy = 16) {
  el.style.opacity = a;
  el.style.transform = `translateY(${((1 - a) * dy).toFixed(2)}px)`;
}
function hl(el, a) { el.style.backgroundSize = `${(clamp(a) * 100).toFixed(2)}% 100%`; }
const $ = id => document.getElementById(id);

const ICON = {
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="4.5 12.5 9.5 17.5 19.5 6.5"/></svg>',
  notify: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5.5" width="18" height="13" rx="2"/><polyline points="3.5 6.5 12 13 20.5 6.5"/></svg>',
  ask: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>',
  block: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg>',
  tri: '<svg viewBox="0 0 14 14" fill="currentColor"><path d="M3 1.5 L12 7 L3 12.5 Z"/></svg>',
};

// state: 0 = all dim, 1..4 = that step active (earlier ones done), 5 = whole flow lit
function chapterBar(state, theme) {
  const names = ['Request', 'Lookup', 'Order', 'Approval'];
  let html = `<div class="bar ${theme || ''}">`;
  names.forEach((n, i) => {
    const k = i + 1;
    let cls = 'dim';
    if (state === 5) cls = 'lit';
    else if (k < state) cls = 'done';
    else if (k === state) cls = 'active';
    const inner = (cls === 'done' || cls === 'lit') ? ICON.check : String(k);
    html += `<span class="pill ${cls}"><span class="num">${inner}</span>${n}</span>`;
    if (k < 4) html += `<span class="arr ${(state === 5 || k < state) ? 'on' : ''}">${ICON.tri}</span>`;
  });
  return html + '</div>';
}

function tierBadge(kind, label) {
  return `<span class="tier tier-${kind}">${ICON[kind === 'silent' ? 'check' : kind]}${label}</span>`;
}

function fmt(n) { return Math.round(n).toLocaleString('en-US'); }
