// 形象工作室：上传照片 -> 取景 -> 生成形象（AI 或本地滤镜）-> 选身体 -> 存成伙伴

import { STYLE_PRESETS, fileToBitmap, stylize, canvasToDataURL } from './cartoonify.js';
import { Character, SPECIES, PALETTES } from './character.js';
import { fetchAiStatus, generateCharacter } from './ai.js';
import { store } from './store.js';

const PREVIEW_SIZE = 288;   // 调参时的预览分辨率（求快）
const EXPORT_SIZE = 384;    // 保存时的分辨率（求清晰）
const UPLOAD_SIZE = 768;    // 发给 AI 的照片分辨率（够清楚又不会太大）

export class Studio {
  constructor(dialog, onSaved) {
    this.dialog = dialog;
    this.onSaved = onSaved;
    this.source = null;      // 原始照片
    this.aiImage = null;     // AI 生成的形象
    this.mode = 'ai';        // ai | local
    this.editingId = null;
    this.params = defaultParams();
    this.build();
    this.loadAiStatus();
  }

  build() {
    this.dialog.innerHTML = `
      <form method="dialog" class="studio">
        <header class="studio-head">
          <h2>形象工作室</h2>
          <button class="icon-btn" value="cancel" aria-label="关闭">✕</button>
        </header>

        <div class="studio-body">
          <section class="studio-col">
            <div class="drop-zone" data-drop>
              <input type="file" accept="image/*" hidden data-file>
              <div class="drop-inner">
                <div class="drop-emoji">🐱🐶🧑</div>
                <p>把猫 / 狗 / 人的照片拖进来</p>
                <div class="row">
                  <button type="button" class="btn primary" data-pick>选择照片</button>
                  <button type="button" class="btn ghost" data-sample>没有照片？试试示例</button>
                </div>
                <p class="hint" data-privacy>照片只在你的浏览器里处理，不会上传</p>
              </div>
            </div>

            <div class="framer" data-framer hidden>
              <canvas data-preview width="${PREVIEW_SIZE}" height="${PREVIEW_SIZE}"></canvas>
              <div class="framer-busy" data-busy hidden><span class="spinner"></span>正在画…</div>
              <div class="framer-hint">在圆里拖动移图，滚轮缩放</div>
            </div>

            <div class="mode-tabs" data-modes hidden>
              <button type="button" class="tab" data-mode="ai">AI 生成</button>
              <button type="button" class="tab" data-mode="local">本地滤镜</button>
            </div>

            <!-- AI 模式 -->
            <div class="ai-panel" data-ai hidden>
              <div class="picker">
                <span class="picker-label">画风</span>
                <div class="chips" data-ai-styles></div>
              </div>
              <label class="name-field">补充<input type="text" data-extra maxlength="60" placeholder="比如：戴一顶小黄帽（可留空）"></label>
              <div class="row">
                <button type="button" class="btn primary" data-generate>✨ 生成形象</button>
                <button type="button" class="btn ghost small" data-use-photo hidden>用回原照片</button>
              </div>
              <p class="hint" data-ai-status></p>
            </div>

            <!-- 本地滤镜模式 -->
            <div class="controls-grid" data-controls hidden>
              <label>色阶<input type="range" data-p="levels" min="2" max="10" step="1"></label>
              <label>描边<input type="range" data-p="edge" min="0" max="1.4" step="0.05"></label>
              <label>饱和<input type="range" data-p="saturation" min="0.4" max="2.2" step="0.05"></label>
              <label>亮度<input type="range" data-p="brightness" min="0.7" max="1.4" step="0.02"></label>
              <div class="chips" data-styles></div>
              <label class="check"><input type="checkbox" data-p="cutout"> 去掉背景（纯色背景效果最好）</label>
            </div>

            <div class="controls-grid" data-frame-controls hidden>
              <label>缩放<input type="range" data-p="zoom" min="0.6" max="3" step="0.02"></label>
              <label>旋转<input type="range" data-p="rotation" min="-45" max="45" step="1"></label>
              <label>柔边<input type="range" data-p="feather" min="0" max="1" step="0.05"></label>
              <button type="button" class="btn ghost small" data-reset>恢复默认参数</button>
            </div>
          </section>

          <section class="studio-col">
            <div class="pet-preview" data-stage></div>

            <div class="picker">
              <span class="picker-label">身体</span>
              <div class="chips" data-species></div>
            </div>
            <div class="picker">
              <span class="picker-label">毛色</span>
              <div class="chips swatches" data-palettes></div>
            </div>
            <label class="name-field">名字<input type="text" data-name maxlength="12" placeholder="给它取个名字"></label>

            <div class="studio-actions">
              <button type="button" class="btn primary big" data-save disabled>保存这个伙伴</button>
            </div>
          </section>
        </div>
      </form>`;

    const $ = (sel) => this.dialog.querySelector(sel);
    this.els = {
      drop: $('[data-drop]'),
      file: $('[data-file]'),
      privacy: $('[data-privacy]'),
      framer: $('[data-framer]'),
      preview: $('[data-preview]'),
      busy: $('[data-busy]'),
      modes: $('[data-modes]'),
      ai: $('[data-ai]'),
      aiStyles: $('[data-ai-styles]'),
      aiStatus: $('[data-ai-status]'),
      extra: $('[data-extra]'),
      generate: $('[data-generate]'),
      usePhoto: $('[data-use-photo]'),
      controls: $('[data-controls]'),
      frameControls: $('[data-frame-controls]'),
      stage: $('[data-stage]'),
      styles: $('[data-styles]'),
      species: $('[data-species]'),
      palettes: $('[data-palettes]'),
      name: $('[data-name]'),
      save: $('[data-save]'),
    };

    this.character = new Character(this.els.stage, { species: 'cat', palette: 'orange' });

    this.renderChips();
    this.bind();
    this.syncControls();
  }

  /** 问后端 AI 配好了没；没配好就默认走本地滤镜 */
  async loadAiStatus() {
    this.aiStatus = await fetchAiStatus();
    if (this.aiStatus.styles?.length) {
      this.els.aiStyles.innerHTML = this.aiStatus.styles
        .map((s) => `<button type="button" class="chip" data-ai-style="${s.id}">${s.label}</button>`)
        .join('');
      if (!this.params.aiStyle) this.params.aiStyle = this.aiStatus.styles[0].id;
    }
    if (!this.aiStatus.ready) this.mode = 'local';
    this.renderAiStatus();
    this.syncControls();
    this.applyMode();
  }

  renderAiStatus(message = null, kind = '') {
    const el = this.els.aiStatus;
    el.className = `hint ${kind}`;
    if (message) {
      el.textContent = message;
      return;
    }
    const s = this.aiStatus;
    if (!s) el.textContent = '正在检查 AI 服务…';
    else if (s.offline) el.textContent = '没连上后端，AI 生成不可用。用 node server/server.mjs 启动即可。';
    else if (!s.ready) el.textContent = '后端还没配 AI（当前是回环模式）。设置 GEMINI_API_KEY 或 OPENAI_API_KEY 后重启即可。';
    else el.textContent = `照片会发给 ${s.label} 生成形象，可能要等十几秒。`;
    this.els.generate.disabled = !s || s.offline;
  }

  renderChips() {
    this.els.styles.innerHTML = Object.entries(STYLE_PRESETS)
      .map(([key, s]) => `<button type="button" class="chip" data-style="${key}">${s.label}</button>`)
      .join('');
    this.els.species.innerHTML = Object.entries(SPECIES)
      .map(([key, s]) => `<button type="button" class="chip" data-species-key="${key}">${s.label}</button>`)
      .join('');
    this.els.palettes.innerHTML = PALETTES.map(
      (p) => `<button type="button" class="chip swatch" data-palette="${p.id}" title="${p.label}"
              style="--sw:${p.main};--sw-dark:${p.dark}"></button>`
    ).join('');
  }

  bind() {
    const { drop, file } = this.els;

    this.dialog.querySelector('[data-pick]').addEventListener('click', () => file.click());
    this.dialog.querySelector('[data-sample]').addEventListener('click', () => this.loadSample());
    file.addEventListener('change', () => {
      if (file.files?.[0]) this.loadFile(file.files[0]);
    });

    ['dragenter', 'dragover'].forEach((e) =>
      drop.addEventListener(e, (ev) => {
        ev.preventDefault();
        drop.classList.add('over');
      })
    );
    ['dragleave', 'drop'].forEach((e) =>
      drop.addEventListener(e, (ev) => {
        ev.preventDefault();
        drop.classList.remove('over');
      })
    );
    drop.addEventListener('drop', (ev) => {
      const f = ev.dataTransfer?.files?.[0];
      if (f) this.loadFile(f);
    });

    // 参数滑块
    for (const input of this.dialog.querySelectorAll('[data-p]')) {
      input.addEventListener('input', () => {
        const key = input.dataset.p;
        this.params[key] = input.type === 'checkbox' ? input.checked : Number(input.value);
        this.refresh();
      });
    }
    this.dialog.querySelector('[data-reset]').addEventListener('click', () => {
      const { style, aiStyle } = this.params;
      this.params = { ...defaultParams(), style, aiStyle };
      this.applyStylePreset(style);
      this.syncControls();
      this.refresh();
    });

    // 圆内拖动取景
    const canvas = this.els.preview;
    let dragging = false;
    let last = null;
    canvas.addEventListener('pointerdown', (e) => {
      dragging = true;
      last = { x: e.clientX, y: e.clientY };
      canvas.setPointerCapture(e.pointerId);
    });
    canvas.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      const rect = canvas.getBoundingClientRect();
      this.params.offsetX += (e.clientX - last.x) / rect.width;
      this.params.offsetY += (e.clientY - last.y) / rect.height;
      last = { x: e.clientX, y: e.clientY };
      this.refresh();
    });
    const stopDrag = () => (dragging = false);
    canvas.addEventListener('pointerup', stopDrag);
    canvas.addEventListener('pointercancel', stopDrag);
    canvas.addEventListener(
      'wheel',
      (e) => {
        e.preventDefault();
        this.params.zoom = clamp(this.params.zoom * (e.deltaY > 0 ? 0.94 : 1.06), 0.6, 3);
        this.syncControls();
        this.refresh();
      },
      { passive: false }
    );

    this.els.modes.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-mode]');
      if (!btn) return;
      this.mode = btn.dataset.mode;
      this.applyMode();
      this.refresh();
    });

    this.els.aiStyles.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-ai-style]');
      if (!btn) return;
      this.params.aiStyle = btn.dataset.aiStyle;
      this.syncControls();
    });
    this.els.generate.addEventListener('click', () => this.generate());
    this.els.usePhoto.addEventListener('click', () => {
      this.aiImage = null;
      this.els.usePhoto.hidden = true;
      this.renderAiStatus();
      this.refresh();
    });

    this.els.styles.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-style]');
      if (!btn) return;
      this.params.style = btn.dataset.style;
      this.applyStylePreset(this.params.style);
      this.syncControls();
      this.refresh();
    });
    this.els.species.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-species-key]');
      if (!btn) return;
      this.params.species = btn.dataset.speciesKey;
      this.character.setSpecies(this.params.species);
      this.syncControls();
    });
    this.els.palettes.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-palette]');
      if (!btn) return;
      this.params.palette = btn.dataset.palette;
      this.character.setPalette(this.params.palette);
      this.character.setHead(this.headUrl);
      this.syncControls();
    });

    this.els.save.addEventListener('click', () => this.save());
  }

  applyStylePreset(style) {
    const p = STYLE_PRESETS[style];
    if (!p) return;
    this.params.levels = p.levels;
    this.params.edge = p.edge;
    this.params.saturation = p.saturation;
  }

  /** 切换 AI / 本地滤镜的面板显隐 */
  applyMode() {
    const hasPhoto = !!this.source;
    this.els.modes.hidden = !hasPhoto;
    this.els.ai.hidden = !hasPhoto || this.mode !== 'ai';
    this.els.controls.hidden = !hasPhoto || this.mode !== 'local';
    this.els.frameControls.hidden = !hasPhoto;
    this.els.privacy.textContent =
      this.mode === 'ai'
        ? 'AI 模式会把照片发到你自己配置的模型服务；本地滤镜模式则完全不出浏览器。'
        : '照片只在你的浏览器里处理，不会上传';
    for (const btn of this.els.modes.querySelectorAll('[data-mode]')) {
      btn.classList.toggle('on', btn.dataset.mode === this.mode);
    }
  }

  syncControls() {
    for (const input of this.dialog.querySelectorAll('[data-p]')) {
      const key = input.dataset.p;
      if (input.type === 'checkbox') input.checked = !!this.params[key];
      else input.value = this.params[key];
    }
    for (const btn of this.dialog.querySelectorAll('[data-style]'))
      btn.classList.toggle('on', btn.dataset.style === this.params.style);
    for (const btn of this.dialog.querySelectorAll('[data-ai-style]'))
      btn.classList.toggle('on', btn.dataset.aiStyle === this.params.aiStyle);
    for (const btn of this.dialog.querySelectorAll('[data-species-key]'))
      btn.classList.toggle('on', btn.dataset.speciesKey === this.params.species);
    for (const btn of this.dialog.querySelectorAll('[data-palette]'))
      btn.classList.toggle('on', btn.dataset.palette === this.params.palette);
  }

  async loadFile(file) {
    if (!file.type.startsWith('image/')) {
      alert('请选择图片文件');
      return;
    }
    try {
      this.source = await fileToBitmap(file);
      this.afterSourceLoaded();
    } catch (err) {
      console.error(err);
      alert('这张图片读不了，换一张试试');
    }
  }

  loadSample() {
    this.source = drawSamplePhoto();
    this.afterSourceLoaded();
  }

  afterSourceLoaded() {
    this.aiImage = null;
    this.els.drop.hidden = true;
    this.els.framer.hidden = false;
    this.els.usePhoto.hidden = true;
    this.els.save.disabled = false;
    this.params.offsetX = 0;
    this.params.offsetY = 0;
    this.params.zoom = 1.1;
    this.applyMode();
    this.renderAiStatus();
    this.refresh();
  }

  /** 当前用来做头像的图：生成过就用 AI 的结果，否则用原照片 */
  activeSource() {
    return this.mode === 'ai' && this.aiImage ? this.aiImage : this.source;
  }

  /** AI 模式下不套滤镜：生成前预览的就是要发出去的原图，生成后是模型的成品 */
  isRaw() {
    return this.mode === 'ai';
  }

  stylizeOptions(size) {
    return {
      size,
      frame: this.params,
      raw: this.isRaw(),
      style: this.params.style,
      levels: this.params.levels,
      edge: this.params.edge,
      saturation: this.params.saturation,
      brightness: this.params.brightness,
      cutout: !this.isRaw() && this.params.cutout,
      feather: this.params.feather,
    };
  }

  /** 参数变了就重算头像（合并到一帧里，拖动时不会卡） */
  refresh() {
    const src = this.activeSource();
    if (!src || this._pending) return;
    this._pending = true;
    requestAnimationFrame(() => {
      this._pending = false;
      const canvas = stylize(src, this.stylizeOptions(PREVIEW_SIZE));
      const ctx = this.els.preview.getContext('2d');
      ctx.clearRect(0, 0, PREVIEW_SIZE, PREVIEW_SIZE);
      ctx.drawImage(canvas, 0, 0);
      this.headUrl = canvasToDataURL(canvas);
      this.character.setHead(this.headUrl);
    });
  }

  /** 把取好景的照片发给后端生成 */
  async generate() {
    if (!this.source || this.generating) return;
    this.setBusy(true);
    this.renderAiStatus('正在生成，通常十几秒…', '');

    try {
      // 发原照片的取景结果（不加蒙版、不做风格化），让模型看到干净的方图
      const cropped = stylize(this.source, {
        size: UPLOAD_SIZE,
        frame: this.aiImage ? { zoom: 1.1, offsetX: 0, offsetY: 0, rotation: 0 } : this.params,
        raw: true,
        mask: false,
      });
      const image = cropped.toDataURL('image/jpeg', 0.92);

      const result = await generateCharacter({
        image,
        style: this.params.aiStyle,
        species: this.params.species,
        extra: this.els.extra.value.trim(),
      });

      this.aiImage = await loadImage(result);
      // 生成结果是完整方图，取景参数重置，让滑块作用在新图上
      this.params.zoom = 1;
      this.params.offsetX = 0;
      this.params.offsetY = 0;
      this.params.rotation = 0;
      this.els.usePhoto.hidden = false;
      this.syncControls();
      this.refresh();
      this.renderAiStatus('生成好了！可以拖动取景，或者换个画风再生成一次。', 'ok');
      this.character.burst('heart', 3);
    } catch (err) {
      console.error(err);
      this.renderAiStatus(`生成失败：${err.message}。可以再试一次，或者切到本地滤镜。`, 'bad');
    } finally {
      this.setBusy(false);
    }
  }

  setBusy(busy) {
    this.generating = busy;
    this.els.busy.hidden = !busy;
    this.els.generate.disabled = busy;
    this.els.generate.textContent = busy ? '生成中…' : '✨ 生成形象';
  }

  /** 打开工作室；传入已有伙伴则进入编辑模式 */
  open(character = null) {
    this.editingId = character?.id || null;
    if (character) {
      this.params = { ...defaultParams(), ...(character.source || {}), species: character.species, palette: character.palette };
      this.headUrl = character.head;
      this.source = null;
      this.aiImage = null;
      this.els.name.value = character.name || '';
      this.character.setSpecies(character.species);
      this.character.setPalette(character.palette);
      this.character.setHead(character.head);
      // 原图没留存，只能在已有头像上继续调身体 / 名字
      this.els.drop.hidden = true;
      this.els.framer.hidden = true;
      this.els.save.disabled = false;
    } else {
      const aiStyle = this.params.aiStyle;
      this.params = { ...defaultParams(), aiStyle };
      this.source = null;
      this.aiImage = null;
      this.headUrl = null;
      this.els.name.value = '';
      this.els.extra.value = '';
      this.els.drop.hidden = false;
      this.els.framer.hidden = true;
      this.els.save.disabled = true;
      this.character.setHead(null);
      this.character.setSpecies(this.params.species);
      this.character.setPalette(this.params.palette);
    }
    this.applyMode();
    this.syncControls();
    this.character.setState('idle');
    if (!this.dialog.open) this.dialog.showModal();
  }

  save() {
    // 导出用更高分辨率重跑一次
    let head = this.headUrl;
    const src = this.activeSource();
    if (src) head = canvasToDataURL(stylize(src, this.stylizeOptions(EXPORT_SIZE)));
    if (!head) return;

    const name = this.els.name.value.trim() || defaultName(this.params.species);
    const payload = {
      name,
      species: this.params.species,
      palette: this.params.palette,
      head,
      madeBy: this.mode === 'ai' && this.aiImage ? 'ai' : 'local',
      source: { ...this.params },
    };

    const record = this.editingId
      ? store.updateCharacter(this.editingId, payload)
      : store.addCharacter(payload);

    this.character.setState('cheer');
    setTimeout(() => {
      this.dialog.close();
      this.onSaved?.(record);
    }, 700);
  }
}

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('生成的图片读不了'));
    img.src = src;
  });
}

function defaultParams() {
  return {
    zoom: 1.1,
    rotation: 0,
    offsetX: 0,
    offsetY: 0,
    style: 'cartoon',
    aiStyle: 'cozy',
    levels: STYLE_PRESETS.cartoon.levels,
    edge: STYLE_PRESETS.cartoon.edge,
    saturation: STYLE_PRESETS.cartoon.saturation,
    brightness: 1,
    feather: 0.45,
    cutout: false,
    species: 'cat',
    palette: 'orange',
  };
}

function defaultName(species) {
  return { cat: '小猫', dog: '小狗', human: '小人' }[species] || '小伙伴';
}

const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);

/** 没照片时的示例「照片」：程序画一只橘猫脸，方便直接体验流程 */
function drawSamplePhoto() {
  const size = 420;
  const c = document.createElement('canvas');
  c.width = c.height = size;
  const ctx = c.getContext('2d');

  const bg = ctx.createLinearGradient(0, 0, size, size);
  bg.addColorStop(0, '#cfe6f5');
  bg.addColorStop(1, '#a8cbe4');
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, size, size);

  ctx.fillStyle = '#e79a4e';
  ctx.beginPath();
  ctx.moveTo(120, 150); ctx.lineTo(96, 70); ctx.lineTo(176, 118); ctx.closePath(); ctx.fill();
  ctx.beginPath();
  ctx.moveTo(300, 150); ctx.lineTo(324, 70); ctx.lineTo(244, 118); ctx.closePath(); ctx.fill();

  ctx.beginPath();
  ctx.ellipse(210, 220, 118, 106, 0, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = '#f6d7ae';
  ctx.beginPath();
  ctx.ellipse(210, 250, 74, 62, 0, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = '#d98436';
  for (let i = 0; i < 4; i++) {
    ctx.fillRect(170 + i * 22, 118, 9, 34);
  }

  ctx.fillStyle = '#2f2a33';
  ctx.beginPath(); ctx.ellipse(172, 214, 17, 21, 0, 0, Math.PI * 2); ctx.fill();
  ctx.beginPath(); ctx.ellipse(248, 214, 17, 21, 0, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = '#fff';
  ctx.beginPath(); ctx.ellipse(178, 207, 6, 7, 0, 0, Math.PI * 2); ctx.fill();
  ctx.beginPath(); ctx.ellipse(254, 207, 6, 7, 0, 0, Math.PI * 2); ctx.fill();

  ctx.fillStyle = '#e0757f';
  ctx.beginPath();
  ctx.moveTo(210, 252); ctx.lineTo(198, 240); ctx.lineTo(222, 240); ctx.closePath(); ctx.fill();

  ctx.strokeStyle = '#3a3038';
  ctx.lineWidth = 4;
  ctx.beginPath();
  ctx.moveTo(210, 254); ctx.quadraticCurveTo(196, 272, 182, 258);
  ctx.moveTo(210, 254); ctx.quadraticCurveTo(224, 272, 238, 258);
  ctx.stroke();

  ctx.lineWidth = 3;
  ctx.strokeStyle = 'rgba(60,50,55,.6)';
  for (const dir of [-1, 1]) {
    for (let i = 0; i < 3; i++) {
      ctx.beginPath();
      ctx.moveTo(210 + dir * 40, 250 + i * 10);
      ctx.lineTo(210 + dir * 140, 232 + i * 22);
      ctx.stroke();
    }
  }
  return c;
}
