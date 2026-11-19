/* newui/js/ui.js — 渲染与交互总装。
 * 基于现有 UI 版图重写：dock + 栏目/列表 + 搜索 + 歌词卡片 + 口播栏 + EQ + 主题 + 设置。
 * 只改 newui/；所有数据走现有 /api/* 与 /ws。
 */
import * as player from './player.js';
import * as wp from './wallpaper.js';
import * as data from './data.js';
import * as browser from './browser.js';
import * as eq from './eq.js';
import * as theme from './theme.js';
import * as settings from './settings.js';
import { mountAll, bindClose } from './components.js';
import { onEvent } from './api.js';
import * as cascade from './cascade.js';
import * as ring from './ring.js';
import * as palette from './palette.js';
import * as glass from './glass.js';
import * as lyricCanvas from './lyric-canvas.js';

const $ = id => document.getElementById(id);
const fmt = ms => {
  if (!ms || ms < 0) ms = 0;
  const s = Math.floor(ms / 1000);
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
};
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ═══════ 背景（壁纸模块唯一 renderer） ═══════ */
function renderWallpaper(snap, src) {
  const el = $('bg-media'); if (!el) return;
  if (snap.mode === 'cover') {
    const cover = player.snapshot().cover;
    if (cover) { el.src = cover; el.style.opacity = '1'; }
    else { el.removeAttribute('src'); el.style.opacity = '0'; }
    el.classList.remove('brand', 'custom');
  } else {
    if (src) { el.src = src; el.style.opacity = '1'; }
    else { el.removeAttribute('src'); el.style.opacity = '0'; }
    el.classList.toggle('brand', snap.mode === 'brand');
    el.classList.toggle('custom', snap.mode === 'custom');
  }
}
wp.setRenderer(renderWallpaper);

/* ═══════ Toast（统一提示，WS toast/radio_error 走这里） ═══════ */
function toast(msg) {
  const t = $('toast'); if (!t) return;
  t.textContent = String(msg || ''); t.classList.add('show');
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove('show'), 2400);
}
window.__newuiToast = toast;
/* 点击涟漪（照抄旧 UI .ripple）：从按钮中心扩散一圈 */
function ripple(el) {
  if (!el) return;
  const r = el.getBoundingClientRect();
  const span = document.createElement('span');
  span.className = 'ripple';
  const d = Math.max(r.width, r.height);
  span.style.cssText = 'left:' + (r.left + r.width/2 - d/2) + 'px;top:' + (r.top + r.height/2 - d/2)
    + 'px;width:' + d + 'px;height:' + d + 'px;position:fixed';
  document.body.appendChild(span);
  setTimeout(() => span.remove(), 520);
}

/* ═══════ 播放器区 ═══════ */
function renderPlayer(s) {
  // 封面取色（照抄旧 UI applyCoverPalette：写 --primary/--accent 驱动全局配色）
  if (s.cover) palette.applyCoverPalette(s.cover);
  const title = $('track-title'), artist = $('track-artist'), cover = $('track-cover');
  if (title) title.textContent = s.title || '未播放';
  if (artist) artist.textContent = s.artist || '';
  if (cover) {
    if (s.cover && cover.getAttribute('src') !== s.cover) cover.src = s.cover;
    cover.style.opacity = s.cover ? '1' : '0';
  }
  const play = $('btn-play');
  if (play) {
    play.textContent = s.playing ? '⏸' : '▶';
    play.setAttribute('aria-label', s.playing ? '暂停' : '播放');
    play.classList.toggle('playing', !!s.playing);   // 呼吸环（照抄旧 UI :649）
  }
  const pos = $('pos'), dur = $('dur');
  if (pos) pos.textContent = fmt(s.position);
  if (dur) dur.textContent = fmt(s.duration);
  const bar = $('progress-fill');
  if (bar) bar.style.width = (s.duration > 0 ? (s.position / s.duration * 100) : 0) + '%';
  $('btn-fav')?.classList.toggle('on', !!s.favorited);
  $('btn-dislike')?.classList.toggle('on', !!s.disliked);
  $('btn-radio')?.classList.toggle('on', !!s.roamOn);
  $('btn-ai')?.classList.toggle('on', !!s.djOn);
  player.modes().forEach(m => {
    const b = $('mode-' + m.key); if (!b) return;
    b.classList.add('lit');
    const active = m.key === s.modeEff;
    b.classList.toggle('active', active);
    b.setAttribute('aria-pressed', active ? 'true' : 'false');
    b.title = m.name + (s.modeLocked ? '（漫游/推荐下锁定）' : '');
    b.style.opacity = s.modeLocked ? '0.45' : '';
  });
  if (wp.snapshot().mode === 'cover') renderWallpaper(wp.snapshot(), wp.resolveSrc());
}
player.onChange(renderPlayer);
wp.onChange(() => renderWallpaper(wp.snapshot(), wp.resolveSrc()));

/* ═══════ 栏目导航 + 列表 ═══════ */
function renderBrowser(snap) {
  document.querySelectorAll('.view-tab').forEach(b => b.classList.toggle('active', b.dataset.view === snap.active));
  // 弹簧弧形卡片墙（照抄旧 UI）：列表变化时重建，点卡身份起播
  cascade.build(snap.items, { activate: async (e) => {
    await browser.play(e); player.refresh(); toast('已起播');
  }});
  const box = $('entry-list'); if (!box) return;
  if (!snap.items.length) { box.innerHTML = '<div class="empty">暂无内容</div>'; return; }
  const curTitle = player.snapshot().title;
  const prog = player.snapshot().duration > 0
    ? Math.round(player.snapshot().position / player.snapshot().duration * 100) : 0;
  box.innerHTML = snap.items.map((e, i) => {
    const isPlaying = !!curTitle && (e.title === curTitle);
    return '<div class="entry' + (isPlaying ? ' playing' : '') + '" data-i="' + i + '">'
    + '<span class="entry-n">' + (i + 1) + '</span>'
    + '<span class="entry-main"><span class="entry-title">' + esc(e.title || '未知曲目') + '</span>'
    + '<span class="entry-artist">' + esc(e.artist || '') + '</span></span>'
    + '<span class="entry-acts">'
    + '<button class="mini" data-act="next" data-i="' + i + '" title="下一首播放">下一首</button>'
    + '<button class="mini" data-act="fav" data-i="' + i + '" title="收藏">收藏</button>'
    + '<button class="mini" data-act="remove" data-i="' + i + '" title="移除">移除</button>'
    + '</span>'
    + '<span class="entry-prog"><span class="fill" style="width:' + (isPlaying ? prog : 0) + '%"></span></span>'
    + '</div>';
  }).join('');
  box.querySelectorAll('.entry').forEach(row => row.addEventListener('click', ev => {
    if (ev.target.closest('button')) return;
    const i = parseInt(row.dataset.i, 10);
    browser.play(snap.items[i]).then(() => player.refresh()).then(() => toast('已起播'));
  }));
  box.querySelectorAll('button[data-act]').forEach(btn => btn.addEventListener('click', async ev => {
    ev.stopPropagation();
    const i = parseInt(btn.dataset.i, 10), e = snap.items[i], act = btn.dataset.act;
    if (act === 'next') { await browser.playNext(e); toast('已插到下一首'); }
    else if (act === 'fav') { await browser.fav(e, true); toast('已收藏'); }
    else if (act === 'remove') { await browser.remove(e); toast('已移除'); }
  }));
}
browser.onChange(renderBrowser);

/* ═══════ 搜索 ═══════ */
let searchTimer = 0;
function renderSearch(snap) {
  const box = $('search-results'); if (!box) return;
  if (!snap.searchResults.length) { box.innerHTML = ''; box.classList.remove('show'); return; }
  box.classList.add('show');
  box.innerHTML = snap.searchResults.slice(0, 8).map((e, i) =>
    '<div class="sresult" data-i="' + i + '">' + esc(e.title || '') + ' <span>' + esc(e.artist || '') + '</span></div>').join('');
  box.querySelectorAll('.sresult').forEach(el => el.addEventListener('click', async () => {
    const e = snap.searchResults[parseInt(el.dataset.i, 10)];
    await browser.playSearch(e); box.classList.remove('show'); player.refresh(); toast('已起播');
  }));
}
browser.onChange(renderSearch);

/* ═══════ 歌词卡片 ═══════ */
let lyricKey = '', lyricLoading = false;
async function loadLyrics(force) {
  const s = player.snapshot();
  const key = (s.title || '') + '|' + (s.artist || '');
  if (!force && key === lyricKey) return;
  lyricKey = key;
  const body = $('lyric-body'); if (!body) return;
  body.innerHTML = '<span class="lyric-empty">歌词加载中…</span>';
  if (lyricLoading) return;
  lyricLoading = true;
  try {
    const d = await fetch('/api/lyrics').then(r => r.json());
    const lines = Array.isArray(d && d.lines) ? d.lines : [];
    lyricCanvas.setLyricLines(lines);                 // 喂海报画布（3 行缓动滚动）
    body.innerHTML = lines.length
      ? lines.slice(0, 12).map(l => '<div>' + esc(l.text != null ? l.text : l) + '</div>').join('')
      : '<span class="lyric-empty">暂无歌词</span>';
  } catch (_e) { body.innerHTML = '<span class="lyric-empty">歌词暂不可用</span>'; }
  finally { lyricLoading = false; }
}
let lastLyricTitle = '';
player.onChange(s => {
  lyricCanvas.seekLyric(s.position);                  // 照抄旧 UI mainLoop 每帧 seekLyric
  if (s.title && s.title !== lastLyricTitle) { lastLyricTitle = s.title; loadLyrics(true); }
});

/* ═══════ 口播栏 ═══════ */
function renderBroadcast() {
  const tx = $('ai-text'); if (!tx) return;
  const text = data.getBroadcast();
  if (text) { tx.textContent = text; $('aibar')?.classList.add('show'); }
}
data.onChange(renderBroadcast);
onEvent('dj_speech', p => { data.setBroadcastFromWS(p); renderBroadcast(); });
setInterval(() => { if (player.snapshot().djOn) data.loadBroadcast().then(renderBroadcast); }, 3000);

/* ═══════ EQ 面板 ═══════ */
function renderEq(s) {
  $('eqp-power')?.classList.toggle('on', s.enabled);
  document.querySelectorAll('.eq-tab').forEach(b => b.classList.toggle('active', b.dataset.source === s.source));
  const box = $('eqp-bands');
  if (box && s.source === 'manual') {
    if (!box.children.length) {
      box.innerHTML = eq.freqs().map((f, i) => '<div class="eq-band"><label>' + f + 'Hz</label>'
        + '<input type="range" min="-12" max="12" value="0" data-i="' + i + '" orient="vertical">'
        + '<span class="db">0</span></div>').join('');
      box.querySelectorAll('input').forEach(inp => inp.addEventListener('input', e => {
        const i = parseInt(e.target.dataset.i, 10);
        e.target.nextElementSibling.textContent = (e.target.value > 0 ? '+' : '') + e.target.value;
        eq.setBand(i, e.target.value);
      }));
    }
    box.querySelectorAll('input').forEach((inp, i) => {
      inp.value = s.bands[i];
      inp.nextElementSibling.textContent = (s.bands[i] > 0 ? '+' : '') + s.bands[i];
    });
  }
  if (box && s.source !== 'manual') box.innerHTML = '';
  const chips = $('eqp-presets');
  if (chips && s.source === 'preset') {
    chips.innerHTML = eq.presets().map(p => '<button class="chip' + (p === s.preset ? ' active' : '') + '" data-p="' + p + '">' + p + '</button>').join('');
    chips.querySelectorAll('button').forEach(b => b.addEventListener('click', () => eq.setPreset(b.dataset.p)));
  }
  if (chips && s.source !== 'preset') chips.innerHTML = '';
  const dyn = $('eqp-dyn');
  if (dyn && s.source === 'dynamic') { dyn.style.display = ''; dyn.value = s.dyn; }
  else if (dyn) dyn.style.display = 'none';
}
eq.onChange(renderEq);

/* ═══════ 主题面板 ═══════ */
function renderTheme(s) {
  document.documentElement.style.setProperty('--accent', s.accent || '#8b7dff');
  document.body.classList.toggle('beat-on', s.beat);
  document.querySelectorAll('.accent-dot').forEach(d => d.classList.toggle('active', d.dataset.key === s.accent));
  document.querySelectorAll('.bg-chip').forEach(b => b.classList.toggle('active', b.dataset.key === s.bgmode));
  document.querySelectorAll('.ontop-chip').forEach(b => b.classList.toggle('active', b.dataset.key === s.ontop));
  $('beat-toggle')?.classList.toggle('on', s.beat);
}
theme.onChange(renderTheme);

/* ═══════ 设置面板 ═══════ */
function renderSettings() {
  const tabsBox = $('set-tabs');
  if (tabsBox && !tabsBox.children.length) {
    tabsBox.innerHTML = settings.tabs().map(t => '<button class="set-tab' + (t === settings.snapshot().activeTab ? ' active' : '') + '" data-t="' + esc(t) + '">' + esc(t) + '</button>').join('');
    tabsBox.querySelectorAll('button').forEach(b => b.addEventListener('click', () => settings.setTab(b.dataset.t)));
  }
  tabsBox?.querySelectorAll('button').forEach(b => b.classList.toggle('active', b.dataset.t === settings.snapshot().activeTab));
  const box = $('set-fields'); if (!box) return;
  const group = settings.fields().find(g => g.group === settings.snapshot().activeTab);
  if (!group) { box.innerHTML = ''; return; }
  box.innerHTML = group.items.map(it => {
    const v = settings.valueOf(it.path);
    if (it.type === 'bool') {
      const on = v === '1' || v === 'true' || v === true;
      return '<div class="set-row"><label>' + esc(it.label) + '</label>'
        + '<button class="switch' + (on ? ' on' : '') + '" data-path="' + esc(it.path) + '" data-v="' + (on ? '0' : '1') + '">' + (on ? '开' : '关') + '</button></div>';
    }
    return '<div class="set-row"><label>' + esc(it.label) + '</label>'
      + '<input type="text" value="' + esc(v) + '" data-path="' + esc(it.path) + '">'
      + '<button class="mini set-save" data-path="' + esc(it.path) + '">保存</button></div>';
  }).join('');
  box.querySelectorAll('.switch').forEach(b => b.addEventListener('click', () => settings.setValue(b.dataset.path, b.dataset.v)));
  box.querySelectorAll('.set-save').forEach(b => b.addEventListener('click', () => {
    const inp = box.querySelector('input[data-path="' + b.dataset.path + '"]');
    if (inp) { settings.setValue(b.dataset.path, inp.value); toast('已保存'); }
  }));
}
settings.onChange(renderSettings);

/* ═══════ 视图切换 ═══════ */
function showView(which) {
  ['player', 'settings', 'theme', 'eq'].forEach(v =>
    $('view-' + v)?.classList.toggle('hidden', v !== which));
  document.querySelectorAll('.nav-tab').forEach(b => b.classList.toggle('active', b.dataset.nav === which));
  if (which === 'settings') renderSettings();
}

/* ═══════ 事件绑定 ═══════ */
function bind() {
  mountAll(document);
  bindClose($('lyric-close'), () => $('lyric-card')?.classList.add('hidden'));
  ['settings', 'theme', 'eq'].forEach(v => bindClose($('close-' + v), () => showView('player')));

  document.querySelectorAll('.nav-tab').forEach(b => b.addEventListener('click', () => showView(b.dataset.nav)));
  document.querySelectorAll('.view-tab').forEach(b => b.addEventListener('click', () => browser.setView(b.dataset.view)));

  $('btn-play')?.addEventListener('click', () => player.toggle());
  $('btn-next')?.addEventListener('click', () => player.next());
  $('btn-prev')?.addEventListener('click', () => player.prev());
  $('btn-fav')?.addEventListener('click', e => {
    ripple(e.currentTarget);
    const s = player.snapshot();
    player.favorite(!s.favorited);
    if (s.current) data.favoriteTrack(s.current, !s.favorited);
  });
  $('btn-dislike')?.addEventListener('click', async e => {
    ripple(e.currentTarget);
    const s = player.snapshot();
    if (s.disliked) await data.removeDislike('i:' + (s.current && s.current.id));
    else if (s.current) await data.addDislike(s.current);
    player.refresh(); toast('已更新不喜欢');
  });
  $('btn-radio')?.addEventListener('click', async () => {
    const on = !player.snapshot().roamOn;
    await fetch('/api/radio/toggle?on=' + (on ? 1 : 0), { method: 'POST' });
    player.refresh(); toast(on ? '电台模式已开' : '电台模式已关');
  });
  $('btn-ai')?.addEventListener('click', async () => {
    const on = !player.snapshot().djOn;
    await fetch('/api/dj/toggle?on=' + (on ? 1 : 0), { method: 'POST' });
    player.refresh(); toast(on ? 'AI 伴听已开' : 'AI 伴听已关');
  });
  player.modes().forEach(m => $('mode-' + m.key)?.addEventListener('click', () => player.setMode(m.key)));
  $('vol')?.addEventListener('input', e => player.setVolume(parseFloat(e.target.value)));
  $('progress')?.addEventListener('click', e => {
    const d = player.snapshot().duration; if (!d) return;
    const r = e.currentTarget.getBoundingClientRect();
    player.seek((e.clientX - r.left) / r.width * d);
  });
  $('btn-playall')?.addEventListener('click', async () => { await browser.playAll(); player.refresh(); toast('已开始播放'); });
  $('btn-refresh')?.addEventListener('click', () => browser.refresh());
  let listMode = false;
  $('btn-listmode')?.addEventListener('click', e => {
    listMode = !listMode;
    $('cascade-cards')?.classList.toggle('hidden', listMode);
    $('entry-list')?.classList.toggle('hidden', !listMode);
    e.currentTarget.textContent = listMode ? '卡片' : '列表';
  });
  // SVG 进度环点击 seek（照抄旧 UI 120 段采样）
  $('ring-host')?.addEventListener('mousedown', e => ring.seekAt(e.clientX, e.clientY, true));

  const si = $('search-input');
  si?.addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => browser.doSearch(si.value), 300); });
  $('search-clear')?.addEventListener('click', () => { if (si) si.value = ''; browser.doSearch(''); });
  $('search-source')?.addEventListener('change', e => browser.setSource(e.target.value));

  $('eqp-power')?.addEventListener('click', () => eq.setEnabled(!eq.snapshot().enabled));
  document.querySelectorAll('.eq-tab').forEach(b => b.addEventListener('click', () => eq.setSource(b.dataset.source)));
  $('eqp-dyn')?.addEventListener('input', e => eq.setDyn(e.target.value));

  document.querySelectorAll('.accent-dot').forEach(d => d.addEventListener('click', () => theme.setAccent(d.dataset.key)));
  document.querySelectorAll('.bg-chip').forEach(b => b.addEventListener('click', () => theme.setBgMode(b.dataset.key)));
  document.querySelectorAll('.ontop-chip').forEach(b => b.addEventListener('click', () => theme.setOntop(b.dataset.key)));
  $('beat-toggle')?.addEventListener('click', () => theme.setBeat(!theme.snapshot().beat));

  document.addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if (e.code === 'Space') { e.preventDefault(); player.toggle(); }
    if (e.code === 'ArrowRight') player.next();
    if (e.code === 'ArrowLeft') player.prev();
    if (e.key === 'Escape') showView('player');
  });
}

/* ═══════ 启动：先读后端回填（壁纸/主题/设置），再显示 ═══════ */
export async function boot() {
  bind();
  document.body.classList.add('booting');
  await Promise.all([player.boot(), wp.boot(), browser.boot(), eq.boot(), theme.boot(), settings.boot()]);

  const ss = $('search-source');
  if (ss) {
    const s = browser.snapshot();
    ss.innerHTML = '<option value="">自动</option>' + s.sources.map(x =>
      '<option value="' + esc(x.key || '') + '">' + esc(x.short || x.label || x.key || '') + '</option>').join('');
    ss.value = s.currentSource || '';
  }
  const ac = $('accent-list');
  if (ac) ac.innerHTML = theme.accents().map(a =>
    '<button class="accent-dot" data-key="' + esc(a.key) + '" style="background:' + esc(a.color) + '" title="' + esc(a.name) + '"></button>').join('');

  // 挂载核心视觉系统（照抄旧 UI）
  ring.mount({ seek: ms => player.seek(ms) });
  ring.bindProximity();
  lyricCanvas.mount($('poster-canvas'));
  setTimeout(() => { try { glass.run(); } catch (_e) {} }, 60);  // liquid_glass.js:191 load 后 60ms

  renderWallpaperList();
  renderPlayer(player.snapshot());
  renderEq(eq.snapshot());
  renderTheme(theme.snapshot());
  renderSettings();
  data.loadBroadcast().then(renderBroadcast);
  loadLyrics(true);
  document.body.classList.remove('booting');
}

/* 壁纸设置列表 */
function renderWallpaperList() {
  const box = $('wallpaper-list'); if (!box) return;
  const snap = wp.snapshot();
  const cards = [
    { mode: 'cover', name: '跟随封面', desc: '背景随当前歌曲封面变化' },
    { mode: 'brand', name: '品牌壁纸', desc: 'Radio Intelligence 玻璃 logo' },
  ];
  wp.listItems().forEach(it => cards.push({ mode: 'custom', key: it.key, name: it.name || '自定义壁纸', desc: it.description || it.kind || '' }));
  box.innerHTML = cards.map(c => {
    const active = c.mode === snap.mode && (!c.key || c.key === snap.key);
    return '<button class="wp-card' + (active ? ' active' : '') + '" data-mode="' + c.mode + '"'
      + (c.key ? ' data-key="' + esc(c.key) + '"' : '') + '>'
      + '<span class="wp-name">' + esc(c.name) + '</span><span class="wp-desc">' + esc(c.desc) + '</span>'
      + (active ? '<span class="wp-check">✓ 当前</span>' : '') + '</button>';
  }).join('');
  box.querySelectorAll('.wp-card').forEach(btn => btn.addEventListener('click', async () => {
    await wp.select(btn.dataset.mode, btn.dataset.key || '');
    renderWallpaperList();
  }));
}
wp.onChange(renderWallpaperList);
