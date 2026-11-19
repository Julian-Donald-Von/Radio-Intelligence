/*!
 * ui-art-bus.js — 前端美术插件消息总线（冷层专用）
 * 新增文件；不修改任何冻结核心。与后端 webapi/bus.py 不是同一个东西：
 * 后端 bus 走 WS → ui-view.js:56 switch；本总线只在页面内分发冷层事件。
 * 权威决策：docs/UI_ARCH_DECISION_20261117_084816.md §3
 */
(function () {
  'use strict';

  var topics = Object.create(null);
  var stats = { emitted: 0, delivered: 0, dropped: 0, byTopic: Object.create(null) };

  // 高频白名单：feat(30Hz)/spectrum(10Hz) 原始流禁止直接 emit（见决策 §3.1 硬禁令）
  var HIGH_FREQ_DENY = { feat: 1, spectrum: 1 };
  // 冷层可订阅主题（与 gate.ALLOWED_CAPABILITIES 对应）
  var ALLOWED = {
    mood: 1, beats: 1, track_changed: 1, favorite: 1,
    'art.energy': 1, 'plugins.changed': 1
  };
  // 节流表：topic -> 最小间隔 ms（mood 声明 @2hz）
  var MIN_GAP = { mood: 500 };
  var lastAt = Object.create(null);

  function on(topic, fn) {
    if (typeof fn !== 'function') throw new TypeError('ArtBus.on: fn must be function');
    (topics[topic] || (topics[topic] = [])).push(fn);
    return function off() { offOne(topic, fn); };
  }

  function offOne(topic, fn) {
    var arr = topics[topic];
    if (!arr) return;
    var i = arr.indexOf(fn);
    if (i >= 0) arr.splice(i, 1);
    if (!arr.length) delete topics[topic];
  }

  function emit(topic, payload) {
    if (HIGH_FREQ_DENY[topic]) {
      stats.dropped++;
      return false; // 原始高频流不进冷层
    }
    var allowed = ALLOWED[topic] || /^art\.plugin\./.test(topic);
    if (!allowed) {
      stats.dropped++;
      return false;
    }
    if (!topics[topic] || !topics[topic].length) return true; // 无人订阅，静默成功
    var gap = MIN_GAP[topic];
    if (gap) {
      var now = Date.now();
      if (lastAt[topic] && now - lastAt[topic] < gap) { stats.dropped++; return false; }
      lastAt[topic] = now;
    }
    stats.emitted++;
    var arr = topics[topic].slice();
    for (var i = 0; i < arr.length; i++) {
      try { arr[i](payload); stats.delivered++; }
      catch (e) { /* 插件回调异常不得拖垮宿主；宿主 probe 记录见 host */ }
    }
    return true;
  }

  window.ArtBus = {
    on: on,
    off: offOne,
    emit: emit,
    stats: function () { return JSON.parse(JSON.stringify(stats)); },
    allowedTopics: function () { return Object.keys(ALLOWED); }
  };
})();
