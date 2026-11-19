/* newui/js/main.js — 全新 UI 的 ESM 入口（重构版）。
 *
 * 架构（用户 2026-11-18「把老ui复制过来，每个模块按新架构重构」）：
 *   · DOM+CSS 与老 UI 逐字一致 → 视觉不可能漂移（newui/index.html）
 *   · runtime.js = 老 UI 全模块按原加载顺序的 ESM 拼接体（共享作用域原样保留）
 *   · 本入口是唯一 ESM script，未来可逐个 import 独立 ESM 模块替换 runtime 内的对应部分
 *   · T11：水体 ui-water.js 在 index.html 里以 classic script 先于本入口同步加载
 *     （冻结核心，源码不改）；它挂载 window.__water，runtime 的水面参数接线才生效。
 *     runtime 顶部的空实现只在水体加载失败时兜底。
 */
/* P1：布局模块先于 runtime install——runtime 末尾初始化要读 __layout。
   默认值 = 现状硬编码，接入后零视觉变化（小心拆装）。 */
import layout from './layout.js';
layout.install();
bootLayoutUI();   // ESM 静态提升：必须在 install 后显式调（layout-ui 不自动 boot）
/* P2：布局预设/微调 UI（换肤页"播放器布局"分组） */
import { bootLayoutUI } from './layout-ui.js';
import legacy, { __BUILD } from './runtime.js';
/* T12：可插拔效果层（叠层水面 + 背景亚克力）——插件注册表驱动，
   每类效果整体是一个模块，可从 effects-boot.js 增删插件。 */
import './effects-boot.js';

// 暴露调试/验证句柄（新 UI 的统一观测面）
window.__newUI = {
  build: __BUILD,
  legacy,
  version: '2.0.0',
};

// runtime 末尾会跑 ui-init 的 poll()/主循环；这里不重复初始化。
// 若运行时异常，给出可见提示（不静默白屏）。
window.addEventListener('error', function (e) {
  if (!e || !e.message) return;
  console.warn('[newui] runtime error:', e.message);
});
