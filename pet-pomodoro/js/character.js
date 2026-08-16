// 角色骨架 + 程序化动画
//
// 头 = 用户照片风格化后的圆形贴图；身体 = SVG 画的猫 / 狗 / 人。
// 动画不是逐帧图，而是每帧算出各部件的 transform：
//   目标姿势用弹簧插值（换状态时平滑过渡），叠加正弦波做呼吸 / 摇尾 / 摆手，
//   再随机穿插抖耳朵、张望这类小动作，看起来就「活」了。

const NS = 'http://www.w3.org/2000/svg';

// 骨架几何（viewBox 320×320）：Q 版比例——头大身子小
const HEAD_CY = 128;      // 头心
const HEAD_R = 70;        // 头半径（照片贴图的圆）
const BODY_BASE = 250;    // 身体缩放的支点（呼吸时脚不动）
const ARM_L = { x: 112, y: 214 };
const ARM_R = { x: 208, y: 214 };

export const SPECIES = {
  cat: { label: '猫', ears: 'cat', tail: 'cat' },
  dog: { label: '狗', ears: 'dog', tail: 'dog' },
  human: { label: '人', ears: 'none', tail: 'none' },
};

// 配色整体偏暖、偏柔，主打「看着就想抱一下」
export const PALETTES = [
  { id: 'orange', label: '橘', main: '#f2a25c', dark: '#d9803a', light: '#ffd9b0' },
  { id: 'cream', label: '奶油', main: '#f0dcc4', dark: '#d3b493', light: '#fff3e4' },
  { id: 'milktea', label: '奶茶', main: '#c8a184', dark: '#a37c60', light: '#ecd7c3' },
  { id: 'mint', label: '薄荷', main: '#8fd0bd', dark: '#5fae98', light: '#cdeee4' },
  { id: 'pink', label: '粉', main: '#f4a6bb', dark: '#d9758f', light: '#ffd6e1' },
  { id: 'ink', label: '墨', main: '#6b6478', dark: '#4a4557', light: '#a49dae' },
];

export function paletteById(id) {
  return PALETTES.find((p) => p.id === id) || PALETTES[0];
}

// 各状态下的目标姿势（单位：度 / 像素）
const POSES = {
  // armL 为正 = 左爪向外抬起，为负 = 收到身前（趴桌子）
  idle: {
    headRot: 0, headY: 0, headX: 0, bodyY: 0, squash: 0,
    armL: 8, armR: -8, deskOpacity: 0,
    bob: { amp: 2.6, freq: 0.55 }, tail: { amp: 15, freq: 0.6 }, ear: 1,
  },
  focus: {
    headRot: 7, headY: 5, headX: -2, bodyY: 6, squash: 0.04,
    armL: -24, armR: 24, deskOpacity: 1,
    bob: { amp: 1.3, freq: 1.05 }, tail: { amp: 7, freq: 1.5 }, ear: 0.5,
  },
  break: {
    headRot: -6, headY: -4, headX: 0, bodyY: -4, squash: -0.03,
    armL: 54, armR: -54, deskOpacity: 0,
    bob: { amp: 5.5, freq: 1.15 }, tail: { amp: 26, freq: 1.2 }, ear: 1.4,
  },
  sleep: {
    headRot: 16, headY: 8, headX: 4, bodyY: 8, squash: 0.07,
    armL: 4, armR: -4, deskOpacity: 0,
    bob: { amp: 2.2, freq: 0.22 }, tail: { amp: 5, freq: 0.25 }, ear: 0.2,
  },
  cheer: {
    headRot: 0, headY: -8, headX: 0, bodyY: -10, squash: -0.06,
    armL: 84, armR: -84, deskOpacity: 0,
    bob: { amp: 9, freq: 2.1 }, tail: { amp: 34, freq: 2.4 }, ear: 1.6,
  },
};

class Spring {
  constructor(value, stiffness = 0.14, damping = 0.72) {
    this.value = value;
    this.target = value;
    this.velocity = 0;
    this.stiffness = stiffness;
    this.damping = damping;
  }
  step() {
    this.velocity += (this.target - this.value) * this.stiffness;
    this.velocity *= this.damping;
    this.value += this.velocity;
    return this.value;
  }
}

const SPRUNG_KEYS = ['headRot', 'headY', 'headX', 'bodyY', 'squash', 'armL', 'armR', 'deskOpacity', 'ear'];
const WAVE_KEYS = ['bobAmp', 'bobFreq', 'tailAmp', 'tailFreq'];

export class Character {
  /**
   * @param {HTMLElement} host 挂载容器
   * @param {{species?:string, palette?:string, head?:string|null}} config
   */
  constructor(host, config = {}) {
    this.host = host;
    this.config = { species: 'cat', palette: 'orange', head: null, ...config };
    this.state = 'idle';
    this.springs = {};
    for (const k of SPRUNG_KEYS) this.springs[k] = new Spring(POSES.idle[k]);
    for (const k of WAVE_KEYS) this.springs[k] = new Spring(waveValue(POSES.idle, k));
    this.phase = 0;          // 波形相位，单独累积，改频率时不会跳变
    this.tailPhase = 0;
    this.nextTwitch = 2 + Math.random() * 4;
    this.nextGlance = 4 + Math.random() * 6;
    this.glance = 0;
    this.clock = 0;
    this.lastFrame = 0;
    this.running = false;
    this.fxTimer = 0;
    this.render();
  }

  /** 重新生成 SVG（换物种 / 换配色时调用） */
  render() {
    const palette = paletteById(this.config.palette);
    const species = SPECIES[this.config.species] || SPECIES.cat;
    this.host.innerHTML = svgMarkup(species, palette, this.uid());
    this.svg = this.host.querySelector('svg');
    this.parts = {};
    for (const el of this.svg.querySelectorAll('[data-part]')) {
      this.parts[el.dataset.part] = el;
    }
    // 摸摸头：点一下就开心地弹一下，冒爱心
    if (!this._petBound) {
      this.host.addEventListener('pointerdown', () => this.pet());
      this._petBound = true;
    }
    this.setHead(this.config.head);
    if (!this.running) this.start();
  }

  uid() {
    this._uid = this._uid || `c${Math.random().toString(36).slice(2, 8)}`;
    return this._uid;
  }

  setHead(dataUrl) {
    this.config.head = dataUrl || null;
    const img = this.parts.headImage;
    const placeholder = this.parts.placeholder;
    if (!img) return;
    if (dataUrl) {
      img.setAttribute('href', dataUrl);
      img.style.display = '';
      if (placeholder) placeholder.style.display = 'none';
    } else {
      img.removeAttribute('href');
      img.style.display = 'none';
      if (placeholder) placeholder.style.display = '';
    }
  }

  setSpecies(species) {
    if (this.config.species === species) return;
    this.config.species = species;
    this.render();
  }

  setPalette(palette) {
    if (this.config.palette === palette) return;
    this.config.palette = palette;
    this.render();
  }

  /** 切换动画状态：idle / focus / break / sleep / cheer */
  setState(name) {
    if (!POSES[name] || this.state === name) return;
    this.state = name;
    const pose = POSES[name];
    for (const k of SPRUNG_KEYS) this.springs[k].target = pose[k];
    for (const k of WAVE_KEYS) this.springs[k].target = waveValue(pose, k);
    if (name === 'cheer') this.burst('confetti', 18);
    if (name === 'break') this.burst('heart', 3);
  }

  /** 被摸了：给弹簧一个向上的初速度，看起来就像被顺毛顺得一激灵 */
  pet() {
    this.springs.bodyY.velocity -= 4;
    this.springs.headY.velocity -= 3;
    this.springs.squash.velocity -= 0.05;
    this.glanceTarget = (Math.random() - 0.5) * 10;
    this.twitch = 1;
    this.burst('heart', 2);
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.lastFrame = performance.now();
    const loop = (now) => {
      if (!this.running) return;
      const dt = Math.min(0.05, (now - this.lastFrame) / 1000);
      this.lastFrame = now;
      this.tick(dt);
      this._raf = requestAnimationFrame(loop);
    };
    this._raf = requestAnimationFrame(loop);
  }

  stop() {
    this.running = false;
    if (this._raf) cancelAnimationFrame(this._raf);
  }

  tick(dt) {
    this.clock += dt;
    const s = {};
    for (const k of SPRUNG_KEYS) s[k] = this.springs[k].step();
    const bobAmp = this.springs.bobAmp.step();
    const bobFreq = this.springs.bobFreq.step();
    const tailAmp = this.springs.tailAmp.step();
    const tailFreq = this.springs.tailFreq.step();

    this.phase += dt * bobFreq * Math.PI * 2;
    this.tailPhase += dt * tailFreq * Math.PI * 2;

    // 随机小动作：抖耳朵、左右张望
    this.nextTwitch -= dt;
    if (this.nextTwitch <= 0) {
      this.nextTwitch = 3 + Math.random() * 6;
      this.twitch = 1;
    }
    this.twitch = Math.max(0, (this.twitch || 0) - dt * 4);

    this.nextGlance -= dt;
    if (this.nextGlance <= 0) {
      this.nextGlance = 4 + Math.random() * 7;
      this.glanceTarget = (Math.random() - 0.5) * (this.state === 'focus' ? 5 : 12);
    }
    this.glance += ((this.glanceTarget || 0) - this.glance) * 0.05;

    const bob = Math.sin(this.phase) * bobAmp;
    const breathe = 1 + Math.sin(this.phase * 0.5) * 0.018;
    const squash = 1 + s.squash + Math.sin(this.phase) * 0.012;

    const p = this.parts;
    setTransform(p.root, `translate(0 ${bob.toFixed(2)})`);
    setTransform(p.body, `translate(160 ${BODY_BASE}) scale(${(breathe / squash).toFixed(4)} ${squash.toFixed(4)}) translate(-160 -${BODY_BASE})`);
    setTransform(p.bodyGroup, `translate(0 ${s.bodyY.toFixed(2)})`);
    setTransform(
      p.head,
      `translate(${(160 + s.headX).toFixed(2)} ${(HEAD_CY + s.headY).toFixed(2)}) rotate(${(s.headRot + this.glance).toFixed(2)}) translate(-160 -${HEAD_CY})`
    );
    setTransform(p.armL, `rotate(${(s.armL + Math.sin(this.phase * 2) * (this.state === 'focus' ? 9 : 2)).toFixed(2)} ${ARM_L.x} ${ARM_L.y})`);
    setTransform(p.armR, `rotate(${(s.armR - Math.sin(this.phase * 2 + 0.8) * (this.state === 'focus' ? 9 : 2)).toFixed(2)} ${ARM_R.x} ${ARM_R.y})`);

    const earSwing = Math.sin(this.phase * 1.4) * 3 * s.ear + this.twitch * 14;
    setTransform(p.earL, `rotate(${(-earSwing).toFixed(2)} 126 96)`);
    setTransform(p.earR, `rotate(${earSwing.toFixed(2)} 194 96)`);

    if (p.tail) {
      const sway = Math.sin(this.tailPhase) * tailAmp;
      p.tail.setAttribute('d', tailPath(this.config.species, sway));
    }

    if (p.desk) p.desk.setAttribute('opacity', clamp01(s.deskOpacity).toFixed(3));
    if (p.shadow) {
      const spread = 1 + bob * 0.012;
      setTransform(p.shadow, `translate(160 302) scale(${(1 / spread).toFixed(3)} 1) translate(-160 -302)`);
    }

    // 环境特效：专注冒星星、休息冒爱心、睡觉冒 Z
    this.fxTimer -= dt;
    if (this.fxTimer <= 0) {
      const rate = { focus: 2.6, break: 1.8, sleep: 2.4, cheer: 0.5, idle: 5 }[this.state] || 5;
      this.fxTimer = rate * (0.6 + Math.random() * 0.8);
      const kind = { focus: 'spark', break: 'heart', sleep: 'zzz', cheer: 'confetti', idle: 'spark' }[this.state];
      if (kind && (this.state !== 'idle' || Math.random() < 0.5)) this.burst(kind, this.state === 'cheer' ? 8 : 1);
    }
  }

  /** 生成漂浮特效元素，播完自动移除 */
  burst(kind, count = 1) {
    const layer = this.parts.fx;
    if (!layer) return;
    for (let i = 0; i < count; i++) {
      const el = document.createElementNS(NS, kind === 'confetti' ? 'rect' : 'path');
      const x = 160 + (Math.random() - 0.5) * (kind === 'confetti' ? 170 : 110);
      const y = HEAD_CY + (Math.random() - 0.5) * 40;
      if (kind === 'confetti') {
        el.setAttribute('width', 7);
        el.setAttribute('height', 10);
        el.setAttribute('rx', 2);
        el.setAttribute('x', x);
        el.setAttribute('y', y);
        el.setAttribute('fill', ['#f6a54c', '#ef6f6c', '#63c7b2', '#7f9cf5', '#f7d154'][i % 5]);
      } else {
        el.setAttribute('d', FX_PATHS[kind]);
        el.setAttribute('fill', FX_FILL[kind]);
        setTransform(el, `translate(${x} ${y}) scale(${(0.8 + Math.random() * 0.5).toFixed(2)})`);
      }
      layer.appendChild(el);

      const drift = (Math.random() - 0.5) * 60;
      const rise = kind === 'confetti' ? 120 : -80 - Math.random() * 40;
      const anim = el.animate(
        [
          { opacity: 0, transform: 'translate(0px, 0px) scale(0.6)' },
          { opacity: 1, offset: 0.2 },
          { opacity: 0, transform: `translate(${drift}px, ${rise}px) scale(1.1) rotate(${(Math.random() - 0.5) * 200}deg)` },
        ],
        { duration: kind === 'confetti' ? 1500 : 2200, easing: 'cubic-bezier(.2,.7,.4,1)' }
      );
      anim.onfinish = () => el.remove();
    }
  }

  destroy() {
    this.stop();
    this.host.innerHTML = '';
  }
}

const FX_PATHS = {
  heart: 'M0 4 C -6 -3 -12 2 -6 8 L 0 14 L 6 8 C 12 2 6 -3 0 4 Z',
  spark: 'M0 -10 L2.6 -2.6 L10 0 L2.6 2.6 L0 10 L-2.6 2.6 L-10 0 L-2.6 -2.6 Z',
  zzz: 'M-8 -8 H8 L-8 8 H8',
};
const FX_FILL = { heart: '#f4849b', spark: '#f7c948', zzz: 'none' };

function waveValue(pose, key) {
  if (key === 'bobAmp') return pose.bob.amp;
  if (key === 'bobFreq') return pose.bob.freq;
  if (key === 'tailAmp') return pose.tail.amp;
  return pose.tail.freq;
}

function setTransform(el, value) {
  if (el) el.setAttribute('transform', value);
}

const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);

/** 尾巴：从屁股出发的二次曲线，控制点随摆动角度移动 */
function tailPath(species, sway) {
  if (species === 'human') return '';
  if (species === 'dog') {
    return `M 206 246 Q ${230 + sway * 0.7} ${232 - Math.abs(sway) * 0.25} ${234 + sway} ${210 - Math.abs(sway) * 0.2}`;
  }
  return `M 204 252 Q ${252 + sway} 246 ${244 + sway * 1.5} ${198 - Math.abs(sway) * 0.3}`;
}

/** 耳朵（或帽子）：起点都贴着头的圆周，跟着头一起转 */
function earMarkup(kind, palette) {
  if (kind === 'cat') {
    return `
      <g data-part="earL">
        <path d="M112 84 L96 24 L156 60 Z" fill="${palette.main}" stroke="${palette.dark}" stroke-width="5" stroke-linejoin="round"/>
        <path d="M116 76 L106 42 L142 62 Z" fill="${palette.light}"/>
      </g>
      <g data-part="earR">
        <path d="M208 84 L224 24 L164 60 Z" fill="${palette.main}" stroke="${palette.dark}" stroke-width="5" stroke-linejoin="round"/>
        <path d="M204 76 L214 42 L178 62 Z" fill="${palette.light}"/>
      </g>`;
  }
  if (kind === 'dog') {
    return `
      <g data-part="earL">
        <path d="M108 78 C 72 66 60 116 74 150 C 88 184 118 166 120 132 Z" fill="${palette.dark}"/>
        <path d="M106 92 C 86 88 80 122 90 146 C 100 168 112 154 113 130 Z" fill="${palette.main}" opacity=".55"/>
      </g>
      <g data-part="earR">
        <path d="M212 78 C 248 66 260 116 246 150 C 232 184 202 166 200 132 Z" fill="${palette.dark}"/>
        <path d="M214 92 C 234 88 240 122 230 146 C 220 168 208 154 207 130 Z" fill="${palette.main}" opacity=".55"/>
      </g>`;
  }
  // 人：没有耳朵，改成一顶小毛线帽，同样挂在 earL / earR 上做轻微摆动
  return `
    <g data-part="earL">
      <path d="M92 118 A 68 68 0 0 1 228 118 Z" fill="${palette.dark}"/>
      <rect x="86" y="106" width="148" height="20" rx="10" fill="${palette.main}" stroke="${palette.dark}" stroke-width="3"/>
    </g>
    <g data-part="earR"><circle cx="160" cy="48" r="11" fill="${palette.light}" stroke="${palette.dark}" stroke-width="3"/></g>`;
}

function svgMarkup(species, palette, uid) {
  const clipId = `${uid}-head-clip`;
  return `
<svg viewBox="0 0 320 320" class="character" xmlns="${NS}" aria-hidden="true">
  <defs>
    <clipPath id="${clipId}"><circle cx="160" cy="${HEAD_CY}" r="${HEAD_R}"/></clipPath>
    <radialGradient id="${uid}-shadow">
      <stop offset="0%" stop-color="rgba(0,0,0,.22)"/><stop offset="100%" stop-color="rgba(0,0,0,0)"/>
    </radialGradient>
  </defs>

  <ellipse data-part="shadow" cx="160" cy="302" rx="74" ry="13" fill="url(#${uid}-shadow)"/>

  <g data-part="root">
    <g data-part="bodyGroup">
      ${species.tail === 'none' ? '' : `<path data-part="tail" d="" fill="none" stroke="${palette.main}" stroke-width="13" stroke-linecap="round"/>`}

      <g data-part="body">
        <!-- 后脚 -->
        <ellipse cx="132" cy="290" rx="20" ry="12" fill="${palette.dark}"/>
        <ellipse cx="188" cy="290" rx="20" ry="12" fill="${palette.dark}"/>
        <!-- 身体 -->
        <path d="M160 186 C 200 186 214 218 214 248 C 214 278 190 292 160 292 C 130 292 106 278 106 248 C 106 218 120 186 160 186 Z"
              fill="${palette.main}" stroke="${palette.dark}" stroke-width="4"/>
        <path d="M160 208 C 182 208 194 228 194 250 C 194 270 178 280 160 280 C 142 280 126 270 126 250 C 126 228 138 208 160 208 Z"
              fill="${palette.light}" opacity=".85"/>
        <!-- 手臂（绕肩膀旋转） -->
        <g data-part="armL">
          <rect x="${ARM_L.x - 11}" y="${ARM_L.y - 6}" width="22" height="58" rx="11" fill="${palette.main}" stroke="${palette.dark}" stroke-width="4"/>
          <circle cx="${ARM_L.x}" cy="${ARM_L.y + 50}" r="12" fill="${palette.light}" stroke="${palette.dark}" stroke-width="3"/>
        </g>
        <g data-part="armR">
          <rect x="${ARM_R.x - 11}" y="${ARM_R.y - 6}" width="22" height="58" rx="11" fill="${palette.main}" stroke="${palette.dark}" stroke-width="4"/>
          <circle cx="${ARM_R.x}" cy="${ARM_R.y + 50}" r="12" fill="${palette.light}" stroke="${palette.dark}" stroke-width="3"/>
        </g>
      </g>

      <!-- 头（照片贴图 + 耳朵） -->
      <g data-part="head">
        ${earMarkup(species.ears, palette)}
        <circle cx="160" cy="${HEAD_CY}" r="${HEAD_R + 3}" fill="${palette.light}" stroke="${palette.dark}" stroke-width="4"/>
        <!-- 腮红画在照片下面，只从羽化的边缘透出来 -->
        <circle cx="${160 - HEAD_R + 16}" cy="${HEAD_CY + 30}" r="13" fill="#f2879f" opacity=".5"/>
        <circle cx="${160 + HEAD_R - 16}" cy="${HEAD_CY + 30}" r="13" fill="#f2879f" opacity=".5"/>
        <g clip-path="url(#${clipId})">
          <image data-part="headImage" x="${160 - HEAD_R}" y="${HEAD_CY - HEAD_R}" width="${HEAD_R * 2}" height="${HEAD_R * 2}"
                 preserveAspectRatio="xMidYMid slice" style="display:none"/>
          <g data-part="placeholder">
            <circle cx="160" cy="${HEAD_CY}" r="${HEAD_R}" fill="${palette.main}"/>
            <ellipse cx="136" cy="${HEAD_CY - 6}" rx="8" ry="11" fill="#2f2a33"/>
            <ellipse cx="184" cy="${HEAD_CY - 6}" rx="8" ry="11" fill="#2f2a33"/>
            <path d="M146 ${HEAD_CY + 24} q14 12 28 0" stroke="#2f2a33" stroke-width="5" fill="none" stroke-linecap="round"/>
          </g>
        </g>
        <circle cx="160" cy="${HEAD_CY}" r="${HEAD_R}" fill="none" stroke="${palette.dark}" stroke-width="5" opacity=".5"/>
      </g>
    </g>

    <!-- 专注时出现的小书桌 -->
    <g data-part="desk" opacity="0">
      <rect x="66" y="266" width="188" height="14" rx="7" fill="#c79a6b"/>
      <rect x="80" y="280" width="12" height="28" rx="4" fill="#a97f55"/>
      <rect x="228" y="280" width="12" height="28" rx="4" fill="#a97f55"/>
      <rect x="186" y="238" width="38" height="28" rx="4" fill="#6d7b91"/>
      <rect x="190" y="242" width="30" height="20" rx="2" fill="#cfe3f2"/>
      <path d="M98 266 l10 -28 l9 3 l-9 25 z" fill="#e8e2d6"/>
    </g>

    <g data-part="fx"></g>
  </g>
</svg>`;
}
