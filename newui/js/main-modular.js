/* newui/js/main-modular.js — 模块化入口（自动生成，勿手改）。
 * 切换方式：index.html 的 <script type=module> 从 main.js 改为 main-modular.js。
 * 水体不加载（冻结核心）。 */
import { ctx, def } from './ctx.js';
import { register, boot, list } from './registry.js';
import global-liquid from './modules/global-liquid.js';
import liquid-glass from './modules/liquid-glass.js';
import poster from './modules/poster.js';
import core from './modules/core.js';
import state from './modules/state.js';
import dock from './modules/dock.js';
import orbit from './modules/orbit.js';
import column from './modules/column.js';
import sidepanel from './modules/sidepanel.js';
import cards from './modules/cards.js';
import view from './modules/view.js';
import skinset from './modules/skinset.js';
import settings from './modules/settings.js';
import eq from './modules/eq.js';
import eggs from './modules/eggs.js';
import init from './modules/init.js';

// 水体空实现兜底（老 UI 直接引用 window.__water）
if (typeof window.__water === 'undefined') {
  window.__water = { setMusicEvents(){}, setSemantics(){}, setEnabled(){},
                     tune(){}, tuneState(){ return { water: false }; } };
}
ctx.__publish = function (name, value) { def(name, value); };
/* 共享 DOM 引用预置：老代码里 dock/cards 等是裸标识符（靠共享全局可见），
   拆模块后必须挂到 ctx，with(ctx) 才能解析（如 core 的 bindFrameHeadProximity 用 dock）。 */
[ 'dock', 'cards', 'orbits', 'bg', 'bgimg', 'poster-canvas', 'water',
  'column-panel', 'side-panel', 'eq-panel', 'settings-panel', 'theme-panel',
  'aibar', 'search-box', 'search-input', 'toast', 'rec-style', 'rs-slider' ]
  .forEach(id => { const el = document.getElementById(id); if (el && !(id in ctx)) ctx[id] = el; });

const MODULES = [
  [global-liquid, 'global-liquid'],
  [liquid-glass, 'liquid-glass'],
  [poster, 'poster'],
  [core, 'core'],
  [state, 'state'],
  [dock, 'dock'],
  [orbit, 'orbit'],
  [column, 'column'],
  [sidepanel, 'sidepanel'],
  [cards, 'cards'],
  [view, 'view'],
  [skinset, 'skinset'],
  [settings, 'settings'],
  [eq, 'eq'],
  [eggs, 'eggs'],
  [init, 'init'],
]
MODULES.forEach(([m, n]) => register(n, [], m.factory));
window.__newUIModules = { ctx, list, boot,
  modules: () => list(),
  decls: {"global-liquid": ["canvas", "_ready", "_imgW", "VERT", "FRAG", "initGL", "resize", "draw", "tick", "apply_"], "liquid-glass": ["supportsLG", "SVGNS", "XLINK", "defs", "defsEl", "smoothstep", "clamp128", "sdf", "buildMap", "registry", "makeFilter", "buildMapFor", "applyGlass", "TARGETS", "run", "start"], "poster": ["cv", "lyricLines", "viewPos", "viewPosInit", "hoverIdx", "hoverAt", "HOVER_RETURN_MS", "LH", "viewTarget", "state", "FONT", "lyricUrl", "init", "resize", "drawSkinBase", "drawPoster", "LYRIC_EASE", "_lastBoxW", "drawLyricView", "loop", "parseLRC"], "core": ["$", "API", "st", "MODES", "modeIdx", "post", "fmt", "framePath", "pathD", "computeFramePath", "_frameProgLen", "ringHoldTimer", "frameHeadNear", "HEAD_NEAR", "bindFrameHeadProximity", "updateFrameProgress", "eqTimer", "eqTick", "startEq", "stopEq", "beatLevel", "beatPulse", "beatTick", "applyCoverPalette", "_defSkin", "ensureDefaultSkin", "render"], "state": ["PLAY_GRACE_MS", "_playGraceUntil", "armPlayGrace", "clearPlayGrace", "inPlayGrace", "applyState", "lastDjAt", "djHold", "clearDjHold", "aiWaitTimer", "setAIWaiting", "positionAIBar", "showAIBar", "setAIBarText", "hideToken", "hideAIBar", "loadBroadcast", "toast", "positionToast", "lyricTrackKey", "lyricLoading", "lyricReqSeq", "loadLyrics"], "dock": ["dock", "ctlBusy", "ctl", "spawnBurst", "bindToggle", "volEl", "setVolFromEvent", "seeking", "REMOTE_TOL_MS", "seekGuardUntil", "_seekPostAt", "adoptRemotePosition", "seekToMs", "dockSeek", "beginSeek"], "orbit": ["ORBIT_MODE_ORDER", "ORBIT_IDS", "orbitEls", "ORBIT_R", "ORBIT_EDGE", "baseDy", "ORBIT_O_SHIFT", "orbitGeo", "orbitOC", "ORBIT_POS_LIST", "orbitActive", "orbitShown", "orbitAnim", "ORBIT_SPIN_DEG", "orbitSpinDir", "orbitSpinDeg", "orbitCurPos", "orbitAngOf", "orbitArcPoint", "orbitPlace", "orbitCurrentAng", "orbitLayoutRAF", "orbitRequestLayout", "orbitLayout", "orbitRotate", "orbitInZone", "cardsInZone", "AUTO_HIDE_MS", "autoHideTimer", "armAutoHide", "pokeShow", "pokeHide", "colTimer", "pokeColumn", "syncColumnPanel", "columnProbeZone", "columnFlashOnMode", "hideColumnIfIdle", "syncColumnByPlaying", "PANEL_SEL", "isolatedZone", "shelfCardZone", "lyricZone"], "column": ["COL_META", "modeName", "syncColumnHead", "ORBIT_API", "onOrbitModeChange", "RS_LABELS", "rsSlider", "rsTimer", "rsShow", "rsHide", "rsAutoShow", "syncRecMode", "PLAYALL_API"], "sidepanel": ["sideOpen", "sideTab", "HEART_SVG", "BROKEN_SVG", "PLAY_SVG", "NEXT_SVG", "RECO_SVG", "DEL_SVG", "rowSideActions", "drowHtml", "_disKey", "sideItems", "sidePlist", "loadSidePanel", "openPlaylist", "SIDE_TITLE", "positionSidePanel", "openSidePanel", "closeSidePanel", "spPlayAll", "spClose", "bindSideBtn"], "cards": ["CARD_W", "SHELF_CARD_HALF", "ARC_STEP", "STEP_Z", "VISIBLE", "shelfCenterX", "Spring", "spring", "cards", "lastSig", "lastCurId", "keyOf", "sigOf", "pendingReset", "scrollToCurrent", "esc", "PAL", "buildCards", "sampleCoverColor", "updateCardProgress", "layout", "step", "cardsBox", "VIEWS"], "view": ["curView", "plistGroup", "plistSource", "loadView", "loadCards", "poll", "ws"], "skinset": ["THEMES", "DEF_THEME", "uiThemeKey", "uiAccent", "uiBgMode", "uiWallpaper", "uiOnTop", "uiWaterOpts", "WATER_OPTS_DEF", "accentLocked", "lockAccent", "PREFS_DEBOUNCE_MS", "PREFS_SID", "prefsSeq", "persistPrefs", "flushPrefs", "uiBeat", "setBeat", "syncBeatUI", "applyThemePalette", "hexToRgb", "applyPrefsTheme", "syncSkinUI", "drawerSpr", "applyDrawerTransform", "drawerSetTarget", "openDrawer", "closeDrawer", "anyDrawerOpen", "openSettings", "closeSettings", "openTheme", "closeTheme", "refreshSettings", "renderSkinList", "setBgMode", "syncBgModeUI", "ONTOP_IDS", "onTopNormalize", "setOnTop", "syncOnTopUI", "applyWaterOpts", "syncWaterOptsUI", "segSync", "waterOptsPatch", "bindSeg", "bindWaterOpts", "wpType", "useCoverSkin", "applyBrandSkin", "applyWallpaperPref", "bindWallpaper", "uploadWallpaperFile", "uploadWallpaperZip", "importWallpaperSrc", "importWallpaperEngine", "applyWallpaperVideo", "hideVideoWallpaper", "applyWallpaperImage", "isCustomWallpaper", "renderDislikeList", "bindSettingsEvents", "bindCornerButtons"], "settings": ["QWEN3_TTS_VOICES", "FAMILIARITY_LABELS", "SETTING_SCHEMA", "setEsc", "setPathGet", "restoreFoldState", "setApiGet", "setApiSet", "setApiTest", "setStatus", "setMark", "setControl", "renderSettingsGroups", "bindSettingsTabs", "startQrLogin", "openQrModal", "pollQrLoop", "closeQrModal", "initSettingsFolds", "FX_WATER_SCHEMA", "buildFxWater"], "eq": ["EQ_FREQS", "EQ_PRESETS", "EQ_PRESET_HINT", "EQ_LABELS", "eqState", "eqPanelOpen", "_dynTimer", "eqFetch", "eqPost", "syncEqPower", "syncSeg", "renderBands", "fmtFreq", "fmtDb", "onBandInput", "renderChips", "pickPreset", "setSource", "setDynamic", "setPower", "resetBands", "openEqPanel", "closeEqPanel", "toggleEqPanel", "isEqOpen", "initEqUI"], "eggs": ["pick", "_mode", "deriveMode", "tick", "readPhysics", "diagnostics", "_eventCounts", "events", "FG", "_todayKey", "_fgUsed", "_fgBump", "freqGate", "discoveryMatrix", "ARCHIVE_HTML", "archive", "_closeArchive", "tune"], "init": ["initSearchR54", "SEM_OPT_V", "migrateSemOpts", "_lastTick"]} };
boot().then(r => { window.__newUIModules.bootResult = r;
  console.info('[newui-modular] booted ' + r.booted.length + '/' + r.total);
}).catch(e => { window.__newUIModules.bootError = String(e);
  console.warn('[newui-modular] boot failed: ' + e); });