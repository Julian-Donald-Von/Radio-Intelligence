/* sonic-renderer.js — 音域回响地形渲染器（原生 Three.js，无 React）。
 * 视觉核心移植自 yin-yizhen/sonic-topography 的 MapScene（个人非商业学习）。
 * ★ 驱动源是**情绪**（window.__mood.axes 14 维），不是音频——原 AudioEngine 完全不用。
 * ★ 用户 11-19 增强：① 情绪调制 uTempo（高唤醒→波流更快）② 情绪涟漪（差分触发扩散波）
 *   ③ 原作本体自定义：主题色/密度/流星/浮空块开关。
 */
import * as THREE from '/new-assets/js/vendor/three.module.js';
import { TERRAIN_VERT, TERRAIN_FRAG, FLOAT_VERT, FLOAT_FRAG } from './shaders.js';

const clamp01 = v => Math.max(0, Math.min(1, Number(v) || 0));
const clamp = (v,a,b) => Math.max(a, Math.min(b, Number(v)||0));
const lerp = (a, b, t) => a + (b - a) * t;

/* 原作 13 个内置主题（子集，取有代表性的 6 个）——[base1, base2, coolCore, coolEdge, warmCore, warmEdge] */
const THEMES = {
  'ink-wash':      ['#0a0c12','#121826','#2a6fa8','#7aa5d8','#c96a4a','#e8b06a'],
  'neon-tokyo':    ['#08060f','#150a24','#00d4ff','#b026ff','#ff2d95','#ffb02e'],
  'cyber-forest':  ['#04100c','#0a1f18','#00e5a0','#3dffc0','#d0ff5c','#8bd450'],
  'glacier-day':   ['#0b1524','#132840','#5ec8ff','#a8e4ff','#ffd28a','#ff9e6b'],
  'wine-signal':   ['#120810','#240d1c','#ff4d8d','#ff9ec4','#ffcf5c','#ff7a45'],
  'blue-hour':     ['#070b18','#101a33','#4d7cff','#8ea9ff','#ffb877','#ff7a5c'],
};
const THEME_KEYS = Object.keys(THEMES);

/* 14 维情绪 → shader 8 能量 + 色彩 + 节奏（详见 docs/EMOTION_MAPPING_AND_LAYOUT_PRESET_20261119.md） */
function moodToEnergy(a){
  const g = k => clamp01(a[k]);
  const va=g('valence'), ar=g('arousal');
  const a1=g('a1_energetic'),a2=g('a2_tense'),a3=g('a3_bright'),a4=g('a4_warm');
  const a5=g('a5_dense'),a6=g('a6_electronic'),a7=g('a7_expansive'),a8=g('a8_sharp');
  const um=g('u_melancholic'),ud=g('u_dreamy'),ua=g('u_ambient'),ug=g('u_aggressive');
  const geo = Math.sqrt(ar*a1);                       // 中心山体
  return {
    subBass: geo,
    bass:    clamp01(ar * (1 - 0.4*ua) + 0.15*ug),
    lowMid:  va * a7,
    mid:     clamp01(a5*a2 + 0.2*um),
    highMid: a8*a6,
    presence:clamp01(a3*a1 + 0.2*ug),
    brilliance: clamp01(a3*(1-ud)),
    air:     ud*ua,
    warmth:  clamp01(a4 + 0.3*(1-um)),
    brightness: a3,
    sharpness: a8,
    glow:    clamp01(a7*0.6 + va*0.4),
    energy:  clamp01(ar*0.6 + a1*0.4),
    // ★ 节奏/跳动：高唤醒+活力+攻击性 → 波流更快（0.65 ~ 2.6 倍）
    tempo:   clamp(0.65 + 1.1*ar + 0.55*a1 + 0.3*ug, 0.65, 2.6),
    // ★ 涟漪触发强度（差分用）：紧张/攻击/高能 → 更密集的扩散波
    pulse:   clamp01(0.45*a2 + 0.35*ug + 0.3*a8 + 0.2*a1),
  };
}

function themeColors(id){
  const t = THEMES[id] || THEMES['blue-hour'];
  return {
    base1:new THREE.Color(t[0]), base2:new THREE.Color(t[1]),
    coolCore:new THREE.Color(t[2]), coolEdge:new THREE.Color(t[3]),
    warmCore:new THREE.Color(t[4]), warmEdge:new THREE.Color(t[5]),
  };
}

export class SonicRenderer {
  constructor(canvas, opts = {}){
    this.canvas = canvas;
    this.gridSize = opts.gridSize || 96;
    this.opts = {
      theme: opts.theme || 'blue-hour',
      density: opts.density ?? 46,
      meteors: opts.meteors !== false,
      ripples: opts.ripples !== false,
      floating: opts.floating !== false,
    };
    this.running = false; this.raf = 0; this.t0 = performance.now();
    this.sm = { subBass:0.25,bass:0.25,lowMid:0.25,mid:0.25,highMid:0.25,
                presence:0.25,brilliance:0.25,air:0.25,warmth:0.5,brightness:0.5,
                sharpness:0.5,glow:0.5,energy:0.25,tempo:1.0,pulse:0.0 };
    this._prevE = null;              // 情绪差分（触发涟漪）
    this._rippleIdx = 0;
    this._meteorIdx = 0;
    this._lastMeteor = 0;
    this._build();
  }

  _gridParams(){
    const d = clamp(this.opts.density, 0, 100);
    const g = Math.round(96 + (224-96) * d/100);
    const BASE = 168, spacing = BASE/g;
    return { g, spacing, boxWidth: spacing*(0.9/1.05) };
  }

  _build(){
    const { g, spacing, boxWidth } = this._gridParams();
    this.gridSize = g;
    this.renderer = new THREE.WebGLRenderer({ canvas:this.canvas, alpha:true, antialias:true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio||1, 1.5));
    this.renderer.setClearColor(0x000000, 0);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(50, 1, 0.1, 400);
    this.camera.position.set(-12, 24, 96); this.camera.lookAt(0,0,0);

    const U = () => ({ value: 0 });
    this.uniforms = {
      uTime:U(), uTempo:{value:1.0}, uPulse:{value:0.0}, uSubBass:U(), uBass:U(), uLowMid:U(), uMid:U(), uHighMid:U(),
      uPresence:U(), uBrilliance:U(), uAir:U(), uWarmth:U(), uBrightness:U(),
      uSharpness:U(), uSmoothness:{value:0.5}, uDensity:{value:this.opts.density/100},
      uSpectralCentroid:{value:0.5}, uEnergy:U(), uAmplitude:{value:1.0},
      uRipples:{value:Array.from({length:10},()=>({pos:new THREE.Vector2(),time:-100,strength:0,isActive:0,rippleType:0}))},
      uBaseColor1:{value:new THREE.Color()}, uBaseColor2:{value:new THREE.Color()},
      uFogColor:{value:new THREE.Color()},
      uCoolCore:{value:new THREE.Color()}, uCoolEdge:{value:new THREE.Color()},
      uWarmCore:{value:new THREE.Color()}, uWarmEdge:{value:new THREE.Color()},
      uRippleColor:{value:new THREE.Color(0.2,0.9,1.0)}, uGlowIntensity:{value:1.0},
    };
    this._applyTheme();
    const mat = new THREE.ShaderMaterial({
      uniforms:this.uniforms, vertexShader:TERRAIN_VERT, fragmentShader:TERRAIN_FRAG,
      transparent:true, side:THREE.DoubleSide,
    });
    const geo = new THREE.BoxGeometry(boxWidth, 1, boxWidth);
    this.mesh = new THREE.InstancedMesh(geo, mat, g*g);
    this.mesh.frustumCulled = false;
    const dummy = new THREE.Object3D(), offset = (g*spacing)/2;
    let i=0;
    for(let x=0;x<g;x++) for(let z=0;z<g;z++){
      dummy.position.set(x*spacing-offset, -0.5, z*spacing-offset);
      dummy.updateMatrix(); this.mesh.setMatrixAt(i++, dummy.matrix);
    }
    this.mesh.instanceMatrix.needsUpdate = true;
    this.scene.add(this.mesh);

    // 浮空块（InstancedMesh + 独立 shader）
    if (this.opts.floating){
      const fmat = new THREE.ShaderMaterial({
        uniforms:this.uniforms, vertexShader:FLOAT_VERT, fragmentShader:FLOAT_FRAG,
        transparent:true, side:THREE.DoubleSide,
      });
      this.floatMesh = new THREE.InstancedMesh(new THREE.BoxGeometry(1,1,1), fmat, 80);
      this.floatMesh.frustumCulled = false;
      this.floatData = Array.from({length:80},()=>({
        x:(Math.random()-0.5)*150, z:(Math.random()-0.5)*150,
        y:Math.random()*18, size:0.5+Math.random()*2, vy:(0.5+Math.random())*4,
      }));
      this.scene.add(this.floatMesh);
    }
    // 流星 + 尾迹粒子
    if (this.opts.meteors){
      this.meteorMesh = new THREE.InstancedMesh(new THREE.BoxGeometry(0.4,1.2,0.4),
        new THREE.MeshBasicMaterial({color:0xffffff,toneMapped:false}), 20);
      this.meteorMesh.frustumCulled=false; this.scene.add(this.meteorMesh);
      this.meteorData = Array.from({length:20},()=>({active:false,x:0,y:0,z:0,vx:0,vz:0,life:0}));
    }
  }

  _applyTheme(){
    const t = themeColors(this.opts.theme), u = this.uniforms;
    u.uBaseColor1.value.copy(t.base1); u.uBaseColor2.value.copy(t.base2);
    u.uFogColor.value.copy(t.base1);
    u.uCoolCore.value.copy(t.coolCore); u.uCoolEdge.value.copy(t.coolEdge);
    u.uWarmCore.value.copy(t.warmCore); u.uWarmEdge.value.copy(t.warmEdge);
  }

  _addRipple(t, strength){
    if(!this.opts.ripples) return;
    const r = this.uniforms.uRipples.value[this._rippleIdx];
    r.pos.set((Math.random()-0.5)*80,(Math.random()-0.5)*80);
    r.time=t; r.strength=clamp01(strength); r.isActive=1; r.rippleType=0;
    this._rippleIdx = (this._rippleIdx+1)%10;
  }

  /* 参数热更新（不重建 renderer） */
  tune(p){
    if(!p) return { ...this.opts };
    if(p.theme && THEMES[p.theme]){ this.opts.theme=p.theme; this._applyTheme(); }
    if(typeof p.tempoScale==='number'){ this.tempoScale=clamp(p.tempoScale,0.5,2); }
    if(typeof p.density==='number'){
      this.opts.density=clamp(p.density,0,100);
      const {g}=this._gridParams();
      if(g!==this.gridSize){ this._rebuildTerrain(); }
    }
    if(typeof p.meteors==='boolean'){ this.opts.meteors=p.meteors; this._toggle('meteorMesh',p.meteors); }
    if(typeof p.ripples==='boolean'){
      this.opts.ripples=p.ripples;
      if(!p.ripples) this.uniforms.uRipples.value.forEach(r=>{r.isActive=0;r.strength=0});
    }
    if(typeof p.floating==='boolean'){ this.opts.floating=p.floating; this._toggle('floatMesh',p.floating); }
    return { ...this.opts };
  }
  _toggle(name,on){ if(this[name]) this[name].visible=!!on; }
  _rebuildTerrain(){
    // 密度变了：销毁旧 mesh 重建（保留 renderer/scene/uniform）
    const {g,spacing,boxWidth}=this._gridParams();
    this.scene.remove(this.mesh);
    this.mesh.geometry.dispose();
    const geo=new THREE.BoxGeometry(boxWidth,1,boxWidth);
    this.mesh=new THREE.InstancedMesh(geo,this.mesh.material,g*g);
    this.mesh.frustumCulled=false;
    const dummy=new THREE.Object3D(),offset=(g*spacing)/2;let i=0;
    for(let x=0;x<g;x++)for(let z=0;z<g;z++){
      dummy.position.set(x*spacing-offset,-0.5,z*spacing-offset);
      dummy.updateMatrix();this.mesh.setMatrixAt(i++,dummy.matrix);
    }
    this.mesh.instanceMatrix.needsUpdate=true;this.scene.add(this.mesh);
    this.gridSize=g;
    this.uniforms.uDensity.value=this.opts.density/100;
  }

  resize(){
    const c=this.canvas,w=c.clientWidth||1280,h=c.clientHeight||720;
    this.renderer.setSize(w,h,false);
    this.camera.aspect=w/h;this.camera.updateProjectionMatrix();
  }

  start(){
    if(this.running)return;
    this.running=true;
    if(this.canvas)this.canvas.classList.add('on');
    this.resize();
    const tick=()=>{
      if(!this.running)return;
      const now=performance.now(),dt=Math.min(0.1,(now-(this._last||now))/1000);this._last=now;
      const t=(now-this.t0)/1000;
      const axes=(window.__mood&&window.__mood.axes)||null;
      const e=axes?moodToEnergy(axes):{
        subBass:0.25+0.15*Math.sin(now/2600),bass:0.25,lowMid:0.3,mid:0.25,highMid:0.2,
        presence:0.2,brilliance:0.2,air:0.15,warmth:0.5,brightness:0.5,sharpness:0.5,
        glow:0.5,energy:0.25,tempo:1.0,pulse:0.0};
      const k=1-Math.exp(-2.0*dt);
      for(const key of Object.keys(this.sm))this.sm[key]=lerp(this.sm[key],e[key],k);
      const u=this.uniforms;
      u.uTime.value=t;
      u.uTempo.value=this.sm.tempo*(this.tempoScale||1);
      u.uPulse.value=clamp01(this.sm.energy*0.5+this.sm.pulse*0.5);
      u.uSubBass.value=this.sm.subBass;u.uBass.value=this.sm.bass;
      u.uLowMid.value=this.sm.lowMid;u.uMid.value=this.sm.mid;
      u.uHighMid.value=this.sm.highMid;u.uPresence.value=this.sm.presence;
      u.uBrilliance.value=this.sm.brilliance;u.uAir.value=this.sm.air;
      u.uWarmth.value=this.sm.warmth;u.uBrightness.value=this.sm.brightness;
      u.uSharpness.value=this.sm.sharpness;u.uGlowIntensity.value=this.sm.glow;
      u.uEnergy.value=this.sm.energy;

      /* ★ 情绪差分触发涟漪：激烈度上升沿 → 一圈扩散波；越激烈间隔越短 */
      if(this.opts.ripples && axes){
        const up = this._prevE ? Math.max(0, e.pulse - this._prevE) : 0;
        const interval = lerp(2.4, 0.45, this.sm.energy);   // 能量高→间隔短
        if((up>0.035 || this.sm.pulse>0.6) && t-(this._lastRipple||0)>interval){
          this._addRipple(t, 0.35+0.65*this.sm.pulse);
          this._lastRipple=t;
        }
        this._prevE=e.pulse;
      }
      /* 流星：能量高→更频繁（原作无情绪版，这里情绪化） */
      if(this.meteorMesh && this.meteorData){
        const interval=lerp(9,1.6,this.sm.energy);
        if(t-(this._lastMeteor||0)>interval){
          const m=this.meteorData[this._meteorIdx];
          m.active=true;m.x=(Math.random()-0.5)*120;m.y=32+Math.random()*12;m.z=(Math.random()-0.5)*120;
          m.vx=(Math.random()-0.5)*6;m.vz=2+Math.random()*4;m.life=0;
          this._meteorIdx=(this._meteorIdx+1)%20;this._lastMeteor=t;
        }
        const dm=new THREE.Object3D();
        this.meteorData.forEach((m,i)=>{
          if(m.active){
            m.life+=dt;m.y-=28*dt;m.x+=m.vx*dt;m.z+=m.vz*dt;
            dm.position.set(m.x,m.y,m.z);dm.scale.setScalar(1);
            if(m.y<-2||m.life>3)m.active=false;
          } else dm.position.set(0,-1000,0);
          dm.updateMatrix();this.meteorMesh.setMatrixAt(i,dm.matrix);
        });
        this.meteorMesh.instanceMatrix.needsUpdate=true;
      }
      /* 浮空块：能量高→跳更高更快（情绪版） */
      if(this.floatMesh && this.floatData){
        const dm=new THREE.Object3D();
        const speed=lerp(0.5,3,this.sm.energy),sizeK=lerp(0.5,2.2,this.sm.energy);
        this.floatData.forEach((f,i)=>{
          f.y+=f.vy*dt*speed;if(f.y>26)f.y=0;
          dm.position.set(f.x,f.y,f.z);dm.scale.setScalar(f.size*sizeK);
          dm.updateMatrix();this.floatMesh.setMatrixAt(i,dm.matrix);
        });
        this.floatMesh.instanceMatrix.needsUpdate=true;
      }
      this.renderer.render(this.scene,this.camera);
      this.raf=requestAnimationFrame(tick);
    };
    this.raf=requestAnimationFrame(tick);
  }

  stop(){
    this.running=false;cancelAnimationFrame(this.raf);
    if(this.canvas)this.canvas.classList.remove('on');
    try{this.renderer.dispose();}catch(_e){}
  }
  state(){return {running:this.running,gridSize:this.gridSize,opts:{...this.opts},sm:{...this.sm},
                  ripples:this.uniforms.uRipples.value.filter(r=>r.isActive).length};}
}

export const _diag = { moodToEnergy, THEME_KEYS, clamp01 };
export default SonicRenderer;
