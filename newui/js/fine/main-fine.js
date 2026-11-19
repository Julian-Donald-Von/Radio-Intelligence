/* newui/js/fine/main-fine.js — 细粒度模块入口（自动生成）。
 * 复用一级 ctx/registry（../ctx.js, ../registry.js）。 */
import ctx, { def } from '../ctx.js';
import { register, boot, list } from '../registry.js';
import m_state_core from './state-core.js';
import m_ring_progress from './ring-progress.js';
import m_eq_tick from './eq-tick.js';
import m_beat_pulse from './beat-pulse.js';
import m_cover_palette from './cover-palette.js';
import m_render_core from './render-core.js';
import m_theme_prefs from './theme-prefs.js';
import m_prefs_io from './prefs-io.js';
import m_beat_switch from './beat-switch.js';
import m_theme_apply from './theme-apply.js';
import m_drawers from './drawers.js';
import m_bgmode from './bgmode.js';
import m_ontop_water from './ontop-water.js';
import m_wallpaper_src from './wallpaper-src.js';
import m_wallpaper_io from './wallpaper-io.js';
import m_video_wallpaper from './video-wallpaper.js';
import m_dislike_list from './dislike-list.js';
import m_skinset_bind from './skinset-bind.js';
import m_corner_buttons from './corner-buttons.js';
import m_settings_schema from './settings-schema.js';
import m_settings_api from './settings-api.js';
import m_settings_render from './settings-render.js';
import m_qr_login from './qr-login.js';
import m_settings_fold from './settings-fold.js';
import m_fx_water from './fx-water.js';
import m_dock_ctl from './dock-ctl.js';
import m_confetti from './confetti.js';
import m_fav_dislike from './fav-dislike.js';
import m_ai_radio from './ai-radio.js';
import m_volume from './volume.js';
import m_seek from './seek.js';
import m_orbit_geo from './orbit-geo.js';
import m_orbit_rotate from './orbit-rotate.js';
import m_liquid_bg from './liquid-bg.js';
import m_liquid_lens from './liquid-lens.js';
import m_poster_canvas from './poster-canvas.js';
import m_state_apply from './state-apply.js';
import m_column from './column.js';
import m_sidepanel from './sidepanel.js';
import m_cards_wall from './cards-wall.js';
import m_ws_events from './ws-events.js';
import m_eq_panel from './eq-panel.js';
import m_eggs from './eggs.js';
import m_search from './search.js';
import m_init_bridge from './init-bridge.js';
import m_ai_bar from './ai-bar.js';
import m_main_loop from './main-loop.js';

if (typeof window.__water === 'undefined') {
  window.__water = { setMusicEvents(){}, setSemantics(){}, setEnabled(){},
                     tune(){}, tuneState(){ return { water: false }; } };
}
ctx.__publish = (n,v) => def(n,v);
[ 'dock','cards','orbits','bg','bgimg','poster-canvas','water','frameHit',
  'column-panel','side-panel','eq-panel','settings-panel','theme-panel',
  'aibar','search-box','search-input','toast','rec-style','rs-slider' ]
  .forEach(id => { const el = document.getElementById(id); if (el && !(id in ctx)) ctx[id] = el; });

const MODULES = [
  [m_state_core, 'state-core'],
  [m_ring_progress, 'ring-progress'],
  [m_eq_tick, 'eq-tick'],
  [m_beat_pulse, 'beat-pulse'],
  [m_cover_palette, 'cover-palette'],
  [m_render_core, 'render-core'],
  [m_theme_prefs, 'theme-prefs'],
  [m_prefs_io, 'prefs-io'],
  [m_beat_switch, 'beat-switch'],
  [m_theme_apply, 'theme-apply'],
  [m_drawers, 'drawers'],
  [m_bgmode, 'bgmode'],
  [m_ontop_water, 'ontop-water'],
  [m_wallpaper_src, 'wallpaper-src'],
  [m_wallpaper_io, 'wallpaper-io'],
  [m_video_wallpaper, 'video-wallpaper'],
  [m_dislike_list, 'dislike-list'],
  [m_skinset_bind, 'skinset-bind'],
  [m_corner_buttons, 'corner-buttons'],
  [m_settings_schema, 'settings-schema'],
  [m_settings_api, 'settings-api'],
  [m_settings_render, 'settings-render'],
  [m_qr_login, 'qr-login'],
  [m_settings_fold, 'settings-fold'],
  [m_fx_water, 'fx-water'],
  [m_dock_ctl, 'dock-ctl'],
  [m_confetti, 'confetti'],
  [m_fav_dislike, 'fav-dislike'],
  [m_ai_radio, 'ai-radio'],
  [m_volume, 'volume'],
  [m_seek, 'seek'],
  [m_orbit_geo, 'orbit-geo'],
  [m_orbit_rotate, 'orbit-rotate'],
  [m_liquid_bg, 'liquid-bg'],
  [m_liquid_lens, 'liquid-lens'],
  [m_poster_canvas, 'poster-canvas'],
  [m_state_apply, 'state-apply'],
  [m_column, 'column'],
  [m_sidepanel, 'sidepanel'],
  [m_cards_wall, 'cards-wall'],
  [m_ws_events, 'ws-events'],
  [m_eq_panel, 'eq-panel'],
  [m_eggs, 'eggs'],
  [m_search, 'search'],
  [m_init_bridge, 'init-bridge'],
  [m_ai_bar, 'ai-bar'],
  [m_main_loop, 'main-loop'],
]
MODULES.forEach(([m,n]) => register(n, [], m.factory));
window.__newUIFine = { ctx, list, boot, manifest: [{"name": "state-core", "source": "ui-core.js", "space": "状态/模式/post/工具"}, {"name": "ring-progress", "source": "ui-core.js", "space": "SVG进度环/播放头"}, {"name": "eq-tick", "source": "ui-core.js", "space": "EQ频谱条"}, {"name": "beat-pulse", "source": "ui-core.js", "space": "封面音频律动"}, {"name": "cover-palette", "source": "ui-core.js", "space": "封面取色"}, {"name": "render-core", "source": "ui-core.js", "space": "render/默认皮肤"}, {"name": "theme-prefs", "source": "ui-skinset.js", "space": "主题/偏好状态/高亮"}, {"name": "prefs-io", "source": "ui-skinset.js", "space": "偏好落盘/去抖/序号"}, {"name": "beat-switch", "source": "ui-skinset.js", "space": "音频律动开关"}, {"name": "theme-apply", "source": "ui-skinset.js", "space": "主题应用/皮肤网格"}, {"name": "drawers", "source": "ui-skinset.js", "space": "双抽屉推挤透视"}, {"name": "bgmode", "source": "ui-skinset.js", "space": "背景效果 poster/acrylic"}, {"name": "ontop-water", "source": "ui-skinset.js", "space": "叠层水面+参数组"}, {"name": "wallpaper-src", "source": "ui-skinset.js", "space": "封面/logo/壁纸偏好恢复"}, {"name": "wallpaper-io", "source": "ui-skinset.js", "space": "壁纸上传导入"}, {"name": "video-wallpaper", "source": "ui-skinset.js", "space": "视频壁纸"}, {"name": "dislike-list", "source": "ui-skinset.js", "space": "不喜欢列表"}, {"name": "skinset-bind", "source": "ui-skinset.js", "space": "设置事件绑定"}, {"name": "corner-buttons", "source": "ui-skinset.js", "space": "角落按钮"}, {"name": "settings-schema", "source": "ui-settings.js", "space": "SETTING_SCHEMA"}, {"name": "settings-api", "source": "ui-settings.js", "space": "工具/后端通道"}, {"name": "settings-render", "source": "ui-settings.js", "space": "设置控件渲染"}, {"name": "qr-login", "source": "ui-settings.js", "space": "扫码登录"}, {"name": "settings-fold", "source": "ui-settings.js", "space": "折叠委托"}, {"name": "fx-water", "source": "ui-settings.js", "space": "高级视觉调参"}, {"name": "dock-ctl", "source": "ui-dock.js", "space": "dock控制/播放三键"}, {"name": "confetti", "source": "ui-dock.js", "space": "礼花粒子"}, {"name": "fav-dislike", "source": "ui-dock.js", "space": "喜欢/不喜欢"}, {"name": "ai-radio", "source": "ui-dock.js", "space": "AI伴听/电台开关"}, {"name": "volume", "source": "ui-dock.js", "space": "音量"}, {"name": "seek", "source": "ui-dock.js", "space": "进度seek/120段采样"}, {"name": "orbit-geo", "source": "ui-orbit.js", "space": "圆盘几何"}, {"name": "orbit-rotate", "source": "ui-orbit.js", "space": "圆盘轮转/动画"}, {"name": "liquid-bg", "source": "liquid.js", "space": "液态背景"}, {"name": "liquid-lens", "source": "liquid_glass.js", "space": "液态玻璃透镜"}, {"name": "poster-canvas", "source": "poster.js", "space": "歌词海报画布"}, {"name": "state-apply", "source": "ui-state.js", "space": "applyState/歌词/toast"}, {"name": "column", "source": "ui-column.js", "space": "栏目头/漫游滑块"}, {"name": "sidepanel", "source": "ui-sidepanel.js", "space": "侧栏四tab"}, {"name": "cards-wall", "source": "ui-cards.js", "space": "弹簧卡片墙"}, {"name": "ws-events", "source": "ui-view.js", "space": "WS事件"}, {"name": "eq-panel", "source": "ui-eq.js", "space": "EQ面板"}, {"name": "eggs", "source": "ui-eggs.js", "space": "彩蛋"}, {"name": "search", "source": "ui-init.js", "space": "搜索/建议/切源"}, {"name": "init-bridge", "source": "ui-init.js", "space": "初始化迁移/桥接"}, {"name": "ai-bar", "source": "ui-init.js", "space": "AI口播栏常驻"}, {"name": "main-loop", "source": "ui-init.js", "space": "主循环/轮询"}] };
boot().then(r => { window.__newUIFine.bootResult = r;
  console.info('[newui-fine] booted ' + r.booted.length + '/' + r.total);
}).catch(e => { window.__newUIFine.bootError = String(e);
  console.warn('[newui-fine] boot failed: ' + e); });