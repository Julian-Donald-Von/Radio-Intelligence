/*!
 * 示例冷层插件骨架：墨迹涟漪
 * 事件驱动（bus:beats），无 rAF、不碰冻结核心、不写全局快照。
 * 状态：P1 骨架，registry.enabled=false，不自动启用。
 */
(function () {
  'use strict';

  var layer = null;
  var offBeat = null;

  function mount(ctx) {
    layer = document.createElement('div');
    layer.className = 'art-ripple-ink';
    layer.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:4;overflow:hidden';
    ctx.root.appendChild(layer);

    if (typeof ctx.bus.on === 'function') {
      offBeat = ctx.bus.on('beats', function (payload) {
        if (!layer) return;
        var el = document.createElement('span');
        var x = payload && payload.x != null ? payload.x : 50;
        var y = payload && payload.y != null ? payload.y : 50;
        el.style.cssText =
          'position:absolute;left:' + x + '%;top:' + y + '%;width:12px;height:12px;' +
          'border-radius:50%;border:2px solid var(--accent,#a99bff);' +
          'animation:artRippleInk .6s ease-out forwards';
        el.addEventListener('animationend', function () { el.remove(); }, { once: true });
        layer.appendChild(el);
      });
    }

    return {
      unmount: function () {
        if (offBeat) { offBeat(); offBeat = null; }
        if (layer) { layer.remove(); layer = null; }
      }
    };
  }

  function unmount() {
    if (offBeat) { offBeat(); offBeat = null; }
    if (layer) { layer.remove(); layer = null; }
  }

  window.ArtPlugins = window.ArtPlugins || {};
  window.ArtPlugins['art.ripple.ink'] = { mount: mount, unmount: unmount };
})();
