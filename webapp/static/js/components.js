// components.js — 通用组件：toast / confirm / modal / drawer / 空态 / 骨架 / 表格工具
//
// 本文件同时承载「交互无障碍」的集中收口：全站大量 <span class="chip">/<div> 用
// el.onclick= 绑定交互，这类元素不可 Tab 聚焦也不响应 Enter，键盘用户完全无法操作；
// 逐页改模板为 <button> 会牵动 16+ 处结构与样式，故与自绘下拉同一范式用 MutationObserver
// 自动补 tabindex/role 并代理键盘事件，页面零改动、后续新增元素同样生效。
const TOAST_MAX = 4;            // 同屏上限：轮询/批量操作失败会连发十多条，堆叠会盖住整屏内容
const TOAST_SHOW_MS = 3000;     // 最后一次合并后的停留时长（合并到达时重置计时）
const _toasts = new Map();      // key(type|文案) -> {el, count, timer, dead}

function toast(msg, type){
  const root = document.getElementById('toastRoot');
  if (!root) return;
  const key = (type || '') + '|' + msg;
  let item = _toasts.get(key);
  // 淡出中的那一条不复用：否则新文案会被它的移除定时器连带删掉
  if (item && item.dead) item = null;
  if (!item){
    const el = document.createElement('div');
    el.className = 'toast ' + (type || '');
    item = {el, count: 0, timer: null, dead: false};
    _toasts.set(key, item);
    root.appendChild(el);
  }
  item.count += 1;
  item.el.textContent = item.count > 1 ? msg + ' ×' + item.count : msg;
  clearTimeout(item.timer);
  item.timer = setTimeout(() => {
    item.dead = true;
    item.el.style.opacity = '0';
    item.el.style.transition = 'opacity .3s';
    setTimeout(() => { item.el.remove(); if (_toasts.get(key) === item) _toasts.delete(key); }, 400);
  }, TOAST_SHOW_MS);
  // 超上限先淘汰最早的一条（Map 保持插入序，合并不会把它排到末尾）
  while (_toasts.size > TOAST_MAX){
    const oldestKey = _toasts.keys().next().value;
    const oldest = _toasts.get(oldestKey);
    clearTimeout(oldest.timer);
    oldest.el.remove();
    _toasts.delete(oldestKey);
  }
}
function toastError(msg){ toast(msg, 'error'); }
function toastSuccess(msg){ toast(msg, 'success'); }

// ---- 覆盖层基础设施：Esc 响应层栈 + 焦点接管 ----
// 抽屉上再弹确认框时，两层各自监听 Esc 会一键双关（注册在同一目标上的监听器
// 无法靠 stopPropagation 互拒），故统一入栈/出栈、只有栈顶消费 Esc；
// 同时监听器挂在 document 捕获阶段，否则自绘下拉的面板 Esc 会被外层抽屉抢走。
const _escLayers = [];
function pushEscLayer(handler){
  const layer = {handler,
    release(){ const i = _escLayers.indexOf(layer); if (i >= 0) _escLayers.splice(i, 1); }};
  _escLayers.push(layer);
  return layer;
}
if (!window.__escStackBound){
  window.__escStackBound = true;
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    const top = _escLayers[_escLayers.length - 1];
    if (!top) return;                       // 无覆盖层时不拦截（顶栏搜索框等自有局部处理）
    e.preventDefault(); e.stopPropagation();
    top.handler();
  }, true);
}

// 弹窗焦点接管：打开时记住当前焦点、关闭后归还（否则焦点掉回 body，键盘用户
// 得整页重 Tab 一遍）；初始焦点由调用方指定（确认框给「取消」，Enter 误按不会执行危险操作）。
// 返回的关闭函数接受一个 after 回调：调用方用它落地 resolve(值)——必须真正调用，
// 否则 await confirmDialog() 的按钮处理逻辑会永久挂起（实测踩过：弹窗能关、后续代码再也不执行）。
// opts.focusBack：触发元素弹层开着时已被隐藏（菜单项→弹窗：菜单已收）时，
// 焦点无法归还到看不见的元素上，改落调用方指定的兜底元素（实测：不做兜底就掉回 body）
function focusTrap(card, onClose, opts){
  const prev = document.activeElement;
  const fallback = opts && opts.focusBack;
  let done = false;
  return (after) => {
    if (done) return;
    done = true;
    onClose();
    if (typeof after === 'function'){ try{ after(); }catch(e){} }
    // 用 getClientRects() 判可见：offsetParent 对 fixed 定位的元素恒为 null，会误判成已隐藏
    const stillThere = prev && prev.isConnected && prev.getClientRects().length > 0;
    const target = stillThere ? prev : fallback;
    if (target && typeof target.focus === 'function') target.focus();
  };
}

// 确认弹窗：resolve(true/false)；Esc/遮罩=取消，初始焦点在「取消」
function confirmDialog(text, title){
  return new Promise(resolve => {
    const root = document.getElementById('modalRoot');
    root.classList.remove('hidden');
    root.innerHTML = `
      <div class="modal-mask" id="cfMask">
        <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="cfTitle">
          <h3 id="cfTitle">${esc(title || '操作确认')}</h3>
          <p style="margin-bottom:24px;color:var(--text-2)">${esc(text)}</p>
          <div style="display:flex;gap:10px;justify-content:flex-end">
            <button class="btn" id="cfNo">取消</button>
            <button class="btn danger" id="cfYes">确认</button>
          </div>
        </div>
      </div>`;
    const layer = pushEscLayer(() => close(false));
    const end = focusTrap(root, () => { root.classList.add('hidden'); root.innerHTML = ''; layer.release(); });
    const close = (v) => end(() => resolve(v));
    document.getElementById('cfNo').onclick = () => close(false);
    document.getElementById('cfYes').onclick = () => close(true);
    document.getElementById('cfMask').onclick = (e) => { if(e.target.id === 'cfMask') close(false); };
    document.getElementById('cfNo').focus();
  });
}

// 输入弹窗：resolve(输入串/ null)；Enter 提交，Esc 取消
function promptDialog(title, value, placeholder){
  return new Promise(resolve => {
    const root = document.getElementById('modalRoot');
    root.classList.remove('hidden');
    root.innerHTML = `
      <div class="modal-mask" id="pdMask">
        <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="pdTitle">
          <h3 id="pdTitle">${esc(title || '输入')}</h3>
          <input type="text" id="pdInput" value="${esc(value || '')}"
                 placeholder="${esc(placeholder || '')}" maxlength="60"
                 style="width:100%;margin-bottom:20px">
          <div style="display:flex;gap:10px;justify-content:flex-end">
            <button class="btn" id="pdNo">取消</button>
            <button class="btn primary" id="pdYes">确定</button>
          </div>
        </div>
      </div>`;
    const input = document.getElementById('pdInput');
    const layer = pushEscLayer(() => close(null));
    const end = focusTrap(root, () => { root.classList.add('hidden'); root.innerHTML = ''; layer.release(); });
    const close = (v) => end(() => resolve(v));
    const submit = () => close((input.value || '').trim());
    document.getElementById('pdNo').onclick = () => close(null);
    document.getElementById('pdYes').onclick = submit;
    input.onkeydown = (e) => { if (e.key === 'Enter'){ e.preventDefault(); submit(); } };
    document.getElementById('pdMask').onclick = (e) => { if(e.target.id === 'pdMask') close(null); };
    setTimeout(() => { input.focus(); input.select(); }, 40);
  });
}

// 表单抽屉：{title, body, wide, onClose} → 返回容器元素；onClose 返回 Promise 时 await 后再销毁
let drawerPrevClose = null; // 当前抽屉的 onClose（被新 openDrawer 覆盖时先触发，如停止日志流）
let drawerEscLayer = null;  // 当前抽屉的 Esc 层（被新抽屉直接覆盖时也必须出栈，残留层会吞掉 Esc）
function openDrawer(opts){
  if (drawerPrevClose){ try{ drawerPrevClose(); }catch(e){} } // 覆盖旧抽屉前先触发其 onClose
  if (drawerEscLayer){ drawerEscLayer.release(); drawerEscLayer = null; }
  drawerPrevClose = opts.onClose || null;
  const prevFocus = document.activeElement;   // 关闭后焦点归还触发元素
  const root = document.getElementById('drawerRoot');
  root.classList.remove('hidden');
  root.innerHTML = `
    <div class="drawer-mask" id="dwMask"></div>
    <div class="drawer ${opts.wide ? 'wide' : ''}" role="dialog" aria-modal="true" aria-labelledby="dwTitle">
      <div class="drawer-head"><b id="dwTitle">${esc(opts.title)}</b>
        <div class="dh-right"><button class="btn sm ghost" id="dwClose">关闭</button></div>
      </div>
      <div class="drawer-body" id="dwBody">${opts.body}</div>
      ${opts.footer ? `<div class="drawer-foot" id="dwFoot">${opts.footer}</div>` : ''}
    </div>`;
  const close = async () => {
    if (opts.onClose) { await opts.onClose(); }
    if (drawerPrevClose === opts.onClose) drawerPrevClose = null; // 仍为当前抽屉的 onClose 才清除
    if (drawerEscLayer === layer){ drawerEscLayer.release(); drawerEscLayer = null; }
    root.classList.add('hidden'); root.innerHTML = '';
    if (prevFocus && typeof prevFocus.focus === 'function') prevFocus.focus();
  };
  const layer = pushEscLayer(close);
  drawerEscLayer = layer;
  document.getElementById('dwClose').onclick = close;
  document.getElementById('dwMask').onclick = close;
  document.getElementById('dwClose').focus();   // 抽屉内无表单时 Tab 从关闭按钮起步
  return document.getElementById('dwBody');
}

// 空态；btnFn 为全局函数名字符串（与 errorState 的 retryFn 同约定）
function emptyState(icon, text, btnText, btnFn){
  return `<div class="empty-state"><div class="e-ico">${icon}</div>
    <div>${esc(text)}</div>${btnText ? `<button class="btn sm primary e-btn" onclick="${btnFn}()">${btnText}</button>` : ''}</div>`;
}
// 错误态（面板内局部错误 + 重试）；retryFn 为全局函数名字符串
function errorState(text, retryFn){
  return `<div class="error-state"><div class="e-ico">⚠️</div>
    <div class="muted">${esc(text || '数据加载失败')}</div>
    <button class="btn sm e-btn" onclick="${retryFn}()">重试</button></div>`;
}
// 骨架屏：count 个面板块
function skeletonHTML(count){
  return Array.from({length: count || 4}, () =>
    `<div class="skeleton" style="height:${Math.round(90 + Math.random()*40)}px;margin-bottom:14px"></div>`).join('');
}

// 状态徽标：task.status → badge（未知状态回退原值并转义，纵深防御）
function statusBadge(status){
  const map = {success:'success', failed:'failed', running:'running', pending:'pending'};
  const label = {success:'成功', failed:'失败', running:'运行中', pending:'等待中'}[status] || esc(status);
  return `<span class="badge ${map[status] || 'pending'}">${label}</span>`;
}
// 时间格式化：ts(秒) → "MM-DD HH:mm:ss"
function fmtTime(ts){
  if (!ts) return '-';
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
// HTML 转义（所有插值用户/后端数据处使用）
function esc(s){
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// ---- 自绘下拉：原生 select 换肤 ----
// 原生 select 的弹出面板由浏览器/系统渲染，不受 CSS 背景与 color-scheme 完全控制（旧版 Chromium/部分浏览器仍为白底）；
// 这里把 select 隐藏保留（.value/onchange 逻辑不变），外层自绘 按钮面 + 深色选项面板。
// 选项面板每次展开时从 select.options 重建，兼容动态填充的 option。
function enhanceSelect(sel){
  if (!sel || sel.dataset.enh || sel.multiple) return;
  sel.dataset.enh = '1';
  const wrap = document.createElement('div');
  wrap.className = 'sel-wrap' + (sel.classList.contains('filter-sel') ? '' : ' w-full');
  sel.parentNode.insertBefore(wrap, sel);
  wrap.appendChild(sel);
  sel.style.display = 'none';

  const face = document.createElement('div');
  face.className = 'sel-face';
  face.setAttribute('tabindex', '0');
  face.setAttribute('role', 'combobox');
  face.setAttribute('aria-expanded', 'false');
  const val = document.createElement('span'); val.className = 'sel-val';
  const arrow = document.createElement('span'); arrow.className = 'sel-arrow'; arrow.textContent = '▾';
  face.appendChild(val); face.appendChild(arrow);
  wrap.appendChild(face);

  const pop = document.createElement('div');
  pop.className = 'sel-pop hidden';
  pop.dataset.faceId = 'sf' + Math.random().toString(36).slice(2, 8);
  face.id = pop.dataset.faceId;
  wrap.appendChild(pop);

  // 无障碍属性随原生控件走：换肤后 select 被隐藏、face 才是 Tab 焦点，名字/描述/校验态
  // 不镜像过去就等于全丢了。用属性观察者而非只在初始化时拷一次——label 关联由
  // enhanceForms 在本函数之后才补写（同一个任务里的先后不固定），镜像必须能持续跟上。
  // title 也要镜像：部分页面只给 select 写了 title 当名字（如会话下拉），不镜像则 face 无名字
  const MIRROR_ATTRS = ['aria-label', 'aria-labelledby', 'aria-describedby', 'aria-required', 'aria-invalid', 'title'];
  const mirrorA11y = () => MIRROR_ATTRS.forEach(a => {
    const v = sel.getAttribute(a);
    if (v) face.setAttribute(a, v); else face.removeAttribute(a);
  });
  new MutationObserver(mirrorA11y).observe(sel, { attributes: true, attributeFilter: MIRROR_ATTRS });
  mirrorA11y();

  const syncFace = () => {
    face.classList.toggle('disabled', sel.disabled);
    face.setAttribute('tabindex', sel.disabled ? '-1' : '0');
    val.textContent = sel.selectedOptions.length ? sel.selectedOptions[0].text : '';
  };
  let popEscLayer = null;               // 面板展开期间的 Esc 层（多层覆盖时只关最顶）
  const closePop = () => {
    pop.classList.add('hidden');
    face.classList.remove('open');
    face.setAttribute('aria-expanded', 'false');
    if (popEscLayer){ popEscLayer.release(); popEscLayer = null; }   // 面板关闭即退出 Esc 层
    /* 归位：避免祖先 overflow 裁剪残留影响 */
    if (pop.parentElement !== wrap){
      pop.style.top = '';
      pop.style.left = '';
      pop.style.position = '';
      pop.style.width = '';
      pop.style.minWidth = '';
      wrap.appendChild(pop);
    }
  };
  const openPop = () => {
    if (sel.disabled) return;
    // 同一时刻只开一个下拉
    document.querySelectorAll('.sel-pop:not(.hidden)').forEach(p => {
      p.classList.add('hidden');
      const f = p.dataset.faceId ? document.getElementById(p.dataset.faceId) : p.previousElementSibling;
      if (f) f.classList.remove('open');
    });
    pop.innerHTML = '';
    let searchEl = null;
    [...sel.options].forEach(o => {
      const d = document.createElement('div');
      d.className = 'sel-opt' + (o.selected ? ' on' : '');
      d.textContent = o.text;
      // 检索键：显示文本 + option.title（会话下拉里存的是末条消息摘要），供 data-search 客户端过滤
      d.dataset.k = ((o.text || '') + ' ' + (o.title || '')).toLowerCase();
      d.onclick = () => {
        sel.value = o.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        syncFace();   // 立即同步面板文本（勿依赖异步的 MutationObserver）
        closePop();
      };
      pop.appendChild(d);
    });
    // 可搜索模式：面板顶部挂筛选框，即时隐藏不匹配项（无需再放独立搜索输入框）
    if (sel.dataset.search){
      const fi = document.createElement('input');
      fi.type = 'text'; fi.className = 'sel-search'; fi.placeholder = '输入关键词筛选…';
      fi.setAttribute('aria-label', '在本下拉列表的选项中筛选');   // 面板内控件不继承字段 label，需自带名字
      const empty = document.createElement('div');
      empty.className = 'sel-opt sel-empty hidden'; empty.textContent = '无匹配项';
      const optEls = [...pop.querySelectorAll('.sel-opt:not(.sel-empty)')];
      fi.oninput = () => {
        const kw = fi.value.trim().toLowerCase();
        let shown = 0;
        optEls.forEach(d => {
          const hit = !kw || (d.dataset.k || '').includes(kw);
          d.classList.toggle('hidden', !hit);
          if (hit) shown++;
        });
        empty.classList.toggle('hidden', shown !== 0);
      };
      // 拦截冒泡：点/键筛选框不应触发 face 的开合切换或外部关闭
      fi.onclick = e => e.stopPropagation();
      fi.onkeydown = e => { if (e.key === 'Escape'){ e.stopPropagation(); closePop(); } };
      pop.insertBefore(fi, pop.firstChild);
      pop.appendChild(empty);
      searchEl = fi;
    }
    syncFace();
    pop.classList.remove('hidden');
    face.classList.add('open');
    face.setAttribute('aria-expanded', 'true');
    /* 挂到 body 用 fixed 定位，避免祖先 overflow:hidden/auto 裁剪下拉面板 */
    if (pop.parentElement !== document.body){
      const r = face.getBoundingClientRect();
      document.body.appendChild(pop);
      pop.style.position = 'fixed';
      pop.style.top = (r.bottom + 4) + 'px';
      /* 宽度自适应内容（不锁定为 face 宽度），让长文本选项完整显示 */
      pop.style.width = '';
      pop.style.minWidth = r.width + 'px';
      pop.style.left = r.left + 'px';
      /* 边界碰撞：右/下超出视口时回弹 */
      const pw = pop.offsetWidth, ph = pop.offsetHeight;
      const vw = document.documentElement.clientWidth;
      const vh = document.documentElement.clientHeight;
      if (r.left + pw > vw) pop.style.left = Math.max(0, vw - pw) + 'px';
      if (r.bottom + 4 + ph > vh){
        pop.style.top = Math.max(0, r.top - ph - 4) + 'px';
      }
    }
    if (searchEl) searchEl.focus(); // 可搜索下拉：展开即聚焦筛选框
    // 面板展开时抢注 Esc 层（栈顶优先）：否则抽屉里的下拉按 Esc 会连抽屉一起关掉
    if (popEscLayer) popEscLayer.release();
    popEscLayer = pushEscLayer(closePop);
  };
  face.onclick = e => { e.stopPropagation(); pop.classList.contains('hidden') ? openPop() : closePop(); };
  face.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown'){ e.preventDefault(); openPop(); }
    else if (e.key === 'Escape') closePop();
  });
  // 代码动态重建 option（如发布抽屉 dpType 联动）时同步面板文本
  new MutationObserver(syncFace).observe(sel, { childList: true, subtree: true });
  syncFace();
}
// 点击其它区域时关闭所有自绘下拉
function bindSelectOutsideClose(){
  document.addEventListener('click', e => {
    document.querySelectorAll('.sel-pop:not(.hidden)').forEach(p => {
      const faceEl = p.dataset.faceId ? document.getElementById(p.dataset.faceId) : p.previousElementSibling;
      if (faceEl && !faceEl.contains(e.target) && !p.contains(e.target)){
        p.classList.add('hidden');
        faceEl.classList.remove('open');
      }
    });
  }, true);
}
// ---- 键盘可达性增强：可点击的非原生元素 ----
// 判定「是不是控件」不硬编类名（页面会不断新增），而看是否被赋了 onclick：
// 项目统一用 el.onclick = fn 写法；原生可交互标签本身就能键盘操作，已自带 role 的
// 自定义控件视为作者已自行处理无障碍，两者一律不介入（避免与既有键盘逻辑双触发）。
const KB_NATIVE = /^(A|AREA|BUTTON|INPUT|SELECT|TEXTAREA|LABEL|SUMMARY|OPTION|DIALOG)$/;
function markKeyboardClickable(el){
  if (!el || el.nodeType !== 1) return;
  if (KB_NATIVE.test(el.tagName)) return;
  if (el.getAttribute('role')) return;                          // 已自行声明语义：不抢作者的键盘契约
  if (el.closest && el.closest('.sel-pop')) return;             // 下拉选项由面板自己管键盘，不该逐个进 Tab 序列
  if (typeof el.onclick !== 'function') return;                 // 未绑定（或晚一个任务才绑）不猜
  el.dataset.kbEnh = '1';
  el.tabIndex = 0;
  el.setAttribute('role', 'button');
  // 短文本才当无障碍名（长文本让读屏直接读内容更准）；模板里换行+缩进会把无障碍名带成多行
  const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
  if (!el.getAttribute('aria-label') && text && text.length <= 40) el.setAttribute('aria-label', text);
}
// ---- 表单无障碍增强：label 关联 / 说明与错误提示关联 ----
// 字段 markup 均由各页模板手写，历史上三种写法并存：<label> 不写 for（占多数）、
// 配置页族的 .f-label 是 div、<label> 直接把控件包在里面（隐式关联，本就合规）。
// 实测全站 130 个 .field、111 个裸 label，带 for 的只有 2 个，aria-describedby/invalid/required 为 0。
// 逐页返工不现实、新页面还会继续写错，故与键盘可达性共用一个 DOM 收口：控件一进文档就补规范关联。
// 只加属性不改标签名：div.f-label 换成 <label> 会被 components.css 的 .field label{}（特异性 0,1,1，
// 高于 .f-label 的 0,1,0）覆写，实测 margin/字号/display 全变，是视觉回归。
const A11Y_CTRL = 'input:not([type="hidden"]),select,textarea';
let a11ySeq = 0;
function a11yId(el, prefix){
  if (!el.id) el.id = prefix + '_' + (++a11ySeq);
  return el.id;
}
function labelById(id){
  if (!id) return null;
  try { return document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]'); }
  catch (e) { return null; }              // id 含引号等畸形字符：当作没有 label
}
// 追加（不覆盖）aria-labelledby / aria-describedby 的引用，多次调用可累积
function a11yRefs(el, attr, ids){
  const add = (ids || []).filter(Boolean);
  if (!el || !add.length) return;
  const cur = (el.getAttribute(attr) || '').split(/\s+/).filter(Boolean);
  const rest = add.filter(v => !cur.includes(v));
  if (rest.length) el.setAttribute(attr, cur.concat(rest).join(' '));
}
// 无主 label：没写 for、也没把控件包在内部（多选组那种「一个勾选框一个 label」的模板 label 被自然排除）
// 先取同域直接子级：「作用域」这类写法是一个字段标题 + 内嵌一块子选项（子块里另有一个标题），
// 全后代扫会得出两个无主 label 而无法判定归属（实测 alerts 规则表单、发布窗口选择器都是这种）
function looseLabels(scope, ctrl){
  const ok = l => l.tagName === 'LABEL' && !l.getAttribute('for') && !l.contains(ctrl)
    && !l.querySelector(A11Y_CTRL) && !l.closest('.sel-pop');
  const direct = [...scope.children].filter(ok);
  return direct.length ? direct : [...scope.querySelectorAll('label:not([for])')].filter(ok);
}
// .f-label 是 div，名字里不该带星标与问号记号，故按子节点剔除后取纯文本
function fLabelText(fl){
  return [...fl.childNodes]
    .filter(n => !(n.nodeType === 1 && n.classList && (n.classList.contains('req') || n.classList.contains('q'))))
    .map(n => n.textContent || '').join(' ').replace(/\s+/g, ' ').trim();
}
// 归属本字段的说明/错误槽：只在同一个 .field（无则同父元素）内取，避免串到邻字段
function a11ySlots(scope){
  return [...scope.querySelectorAll('.f-hint,.f-err,.hint,.field-hint')]
    .filter(el => !el.closest('.sel-pop'))
    .map(el => a11yId(el, 'a11y'));
}
// 控件是否已从任一渠道拿到名字（自身声明 / 被 label 包住 / 已有 label[for]）
function hasAnyName(ctrl){
  return !!(ctrl.getAttribute('aria-label') || ctrl.getAttribute('aria-labelledby')
    || ctrl.closest('label') || labelById(ctrl.id));
}
function enhanceControl(ctrl){
  if (!ctrl || ctrl.nodeType !== 1) return;
  if (ctrl.dataset.a11yForm === '1') return;
  ctrl.dataset.a11yForm = '1';            // 每条只处理一次，整页补扫靠此早退
  if (ctrl.closest('.sel-pop')) return;    // 下拉面板内的筛选框自成一体，不继承字段 label
  const field = ctrl.closest('.field');
  const scope = field || ctrl.parentElement;
  if (!scope) return;
  // 1) 无障碍名：自身已声明、被 label 包住、已有 label[for] 的，一律不介入（不抢作者契约）
  if (!hasAnyName(ctrl)){
    const fl = field && field.querySelector('.f-label');
    if (fl){
      const name = fLabelText(fl);
      if (name) ctrl.setAttribute('aria-label', name);
    } else {
      const owners = looseLabels(scope, ctrl);
      if (owners.length === 1){
        const lb = owners[0];
        // 同域内无主控件只有一个时用原生 for（label 文本被页面改写时名字自动跟随）；
        // 多个控件共用一个 label（如日期+时间拆栏）或下拉换肤（for 指向的是被隐藏的
        // select，face 拿不到名字）时改用 aria-labelledby
        const peers = [...scope.querySelectorAll(A11Y_CTRL)].filter(c => !c.closest('label') && !c.closest('.sel-pop'));
        if (peers.length === 1 && !ctrl.closest('.sel-wrap')) lb.htmlFor = a11yId(ctrl, 'ctl');
        else a11yRefs(ctrl, 'aria-labelledby', [a11yId(lb, 'lbl')]);
      }
      // owners 多于一个（如分组标题 + 字段标题同域）时无法判定归属，猜错比不猜更糟：
      // 留给页面自行处理，已登记在 docs/agent-evolution.md 的挂账里
    }
  }
  // 1b) 筛选条写法：「<span>项目</span> <select>」这类同域既无 label 也无 .f-label 的控件，
  // 只当紧邻的前兄弟是纯文本、不含控件、不是按钮/链接、且不超 20 字时才拿它当名字（全站 8 个筛选下拉皆此模式）
  // 下拉换肤后 select 已不在原位置，要看 .sel-wrap 这个代理者的前兄弟
  if (!hasAnyName(ctrl)){
    const anchor = ctrl.closest('.sel-wrap') || ctrl;
    const pv = anchor.previousElementSibling;
    if (pv && !pv.matches('button,a,.btn,label') && !pv.querySelector(A11Y_CTRL)){
      const t = (pv.textContent || '').replace(/\s+/g, ' ').trim();
      if (t && t.length <= 20) a11yRefs(ctrl, 'aria-labelledby', [a11yId(pv, 'lbl')]);
    }
  }
  // 2) 必填：星标 * 纯视觉，改由 aria-required 表达（label 文本里已剔除星标，不会重读）
  if (field && field.querySelector('.req') && !ctrl.hasAttribute('aria-required')) ctrl.setAttribute('aria-required', 'true');
  // 3) 说明与错误：hint/err 进 aria-describedby，读屏聚焦时连提示一起播
  a11yRefs(ctrl, 'aria-describedby', a11ySlots(scope));
}
// 整组勾选框（通知渠道 / 周期 / 作用域目标）：字段标题属于整组而非单个控件，
// 而每个控件又已被自己的 label 包住，走不到上面的控件路径，故单独给容器上 role=group
function enhanceFieldGroup(field){
  if (field.dataset.a11yGrp === '1') return;
  field.dataset.a11yGrp = '1';
  if (field.getAttribute('role')) return;                       // 作者已自行声明语义
  const loose = [...field.querySelectorAll(A11Y_CTRL)].filter(c => !c.closest('label') && !c.closest('.sel-pop'));
  if (loose.length) return;                                     // 有独立控件的走控件路径
  if (!field.querySelector(A11Y_CTRL)) return;                  // 域内无控件（纯展示块）
  const owners = looseLabels(field, field);
  if (owners.length === 1){
    field.setAttribute('role', 'group');
    field.setAttribute('aria-labelledby', a11yId(owners[0], 'lbl'));
  } else if (!owners.length){
    const fl = field.querySelector('.f-label');
    const name = fl && fLabelText(fl);
    if (name){ field.setAttribute('role', 'group'); field.setAttribute('aria-label', name); }
  }
}
function enhanceForms(root){
  if (!root || root.nodeType !== 1) return;
  if (root.matches && root.matches(A11Y_CTRL)) enhanceControl(root);
  if (root.matches && root.matches('.field')) enhanceFieldGroup(root);
  if (!root.querySelectorAll) return;
  root.querySelectorAll(A11Y_CTRL).forEach(enhanceControl);
  root.querySelectorAll('.field').forEach(enhanceFieldGroup);
}
// 表格语义（WCAG 1.3.1）：数据表列头一律补 scope=col（本项目 thead 全为列头、无行头），
// 并就近取标题作表名。集中收口不逐页改模板，与前两批同一策略。
// 表名四级回退：同 panel 的标题 → 祖先前序的标题 → 活动页签 → 路由页名；全落空才留空（不编造）。
const A11Y_TITLE_SEL = '.panel-title,.section-title,.page-title';
function titleTextOf(el){ return el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : ''; }
// 页签文字常带 emoji 前缀（📏 规则管理），只留中英文与数字作表名
function activeTabName(){
  const t = document.querySelector('.tab-btn.active') || document.querySelector('.sub-tabs .chip.active');
  return t ? (t.textContent || '').replace(/[^\u4e00-\u9fa5A-Za-z0-9]+/g, ' ').trim() : '';
}
function findTableName(table){
  const panel = table.closest('.panel');
  if (panel){ const t = panel.querySelector(A11Y_TITLE_SEL); if (t) return t; }
  // 标题在 panel 外层前兄弟（「列表」面板紧跟一个 .page-title/.section-title）：逐层向上找最近的前序标题
  let node = panel || table.parentElement;
  while (node && node !== document.body){
    for (let prev = node.previousElementSibling; prev; prev = prev.previousElementSibling){
      const t = (prev.matches && prev.matches(A11Y_TITLE_SEL)) ? prev
        : (prev.querySelector ? prev.querySelector(A11Y_TITLE_SEL) : null);
      if (t) return t;
    }
    node = node.parentElement;
  }
  return null;
}
function enhanceTable(table){
  if (!table || table.nodeType !== 1) return;
  if (table.dataset.a11yTbl === '1') return;
  table.dataset.a11yTbl = '1';
  table.querySelectorAll('thead th').forEach(th => {         // 只补未声明的，行头/作者已写的不动
    if (!th.hasAttribute('scope')) th.setAttribute('scope', 'col');
  });
  // 表头裸复选框＝整列「全选」开关（如 monCheckAll，无 label 包裹、无前兄弟，走不到控件路径）：补名让读屏知道这格作用
  table.querySelectorAll('thead th input[type="checkbox"]').forEach(cb => {
    if (cb.closest('label') || cb.getAttribute('aria-label') || cb.getAttribute('aria-labelledby')) return;
    cb.setAttribute('aria-label', '全选');
  });
  if (table.querySelector('caption') || table.getAttribute('aria-label')
    || table.getAttribute('aria-labelledby')) return;         // 作者已自行命名，不抢契约
  const titleEl = findTableName(table);
  if (titleEl && titleTextOf(titleEl)) a11yRefs(table, 'aria-labelledby', [a11yId(titleEl, 'tbl')]);
  else {
    const nm = activeTabName()
      || (typeof currentRoute !== 'undefined' && currentRoute && currentRoute.name) || '';
    if (nm) table.setAttribute('aria-label', nm);
  }
}
function enhanceTables(root){
  if (!root || root.nodeType !== 1) return;
  if (root.matches && root.matches('table')) enhanceTable(root);
  if (root.querySelectorAll) root.querySelectorAll('table').forEach(enhanceTable);
}
function scanClickable(root){
  if (!root || root.nodeType !== 1) return;
  markKeyboardClickable(root);
  if (root.querySelectorAll) root.querySelectorAll('*').forEach(markKeyboardClickable);
  // 键盘可达、表单关联、表格语义共用这一个入口：三次遍历成本实测在亚毫秒级（见下方 scheduleDeepScan 注释）
  enhanceForms(root);
  enhanceTables(root);
}
// 补扫：部分页面在 innerHTML 之后还要 await 一个请求才绑 onclick，单靠插入时的
// 那一次扫描会漏；每次变更突发后 1s 整页重扫一次（已处过的在 role 判定处早退）。
// 实测成本：184 节点页面 0.2ms，800 行可点击表格的 2585 节点压力用例 0.6ms（首扫），
// 重扫无增量（早退），且防抖后每秒最多一次，不影响滚动/输入卡顿。
let deepScanTimer = null;
function scheduleDeepScan(){
  if (deepScanTimer) return;
  deepScanTimer = setTimeout(() => {
    deepScanTimer = null;
    scanClickable(document.body);
  }, 1000);
}
// Enter/Space 代理：聚焦在增强过的元素上按键即触发其 onclick（Space 需阻止页面滚动）。
// 用委托而非逐元素挂监听：onclick 常在 DOM 插入后的下一个任务里才赋值，委托不受先后影响。
document.addEventListener('keydown', e => {
  if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
  const el = e.target;
  if (!el || !el.dataset || el.dataset.kbEnh !== '1') return;
  if (typeof el.onclick !== 'function') return;
  e.preventDefault();          // 否则 Space 会滚动整页
  el.click();
});

// 全局自动增强：页面/抽屉/动态行内新增的 select 一律换肤
function startSelectEnhancer(){
  bindSelectOutsideClose();
  new MutationObserver(muts => {
    for (const m of muts){
      if (m.type !== 'childList') continue;
      for (const n of m.addedNodes){
        if (n.nodeType !== 1) continue;
        if (n.tagName === 'SELECT') enhanceSelect(n);
        else if (n.querySelectorAll) n.querySelectorAll('select').forEach(enhanceSelect);
        // 页面渲染多为「先 innerHTML 再同步赋 onclick」，观察者回调是同一任务后的
        // 微任务，因而能读到已赋好的处理函数；异步绑定的由补扫兜底
        scanClickable(n);
        scheduleDeepScan();
      }
    }
  }).observe(document.body, { childList: true, subtree: true });
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', startSelectEnhancer);
else startSelectEnhancer();
