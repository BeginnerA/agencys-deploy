// components.js — 通用组件：toast / confirm / modal / drawer / 空态 / 骨架 / 表格工具
function toast(msg, type){
  const box = document.createElement('div');
  box.className = 'toast ' + (type || '');
  box.textContent = msg;
  document.getElementById('toastRoot').appendChild(box);
  setTimeout(() => { box.style.opacity = '0'; box.style.transition = 'opacity .3s'; }, 2600);
  setTimeout(() => box.remove(), 3000);
}
function toastError(msg){ toast(msg, 'error'); }
function toastSuccess(msg){ toast(msg, 'success'); }

// 确认弹窗：resolve(true/false)
function confirmDialog(text, title){
  return new Promise(resolve => {
    const root = document.getElementById('modalRoot');
    root.classList.remove('hidden');
    root.innerHTML = `
      <div class="modal-mask" id="cfMask">
        <div class="modal-card">
          <h3>${esc(title || '操作确认')}</h3>
          <p style="margin-bottom:24px;color:var(--text-2)">${esc(text)}</p>
          <div style="display:flex;gap:10px;justify-content:flex-end">
            <button class="btn" id="cfNo">取消</button>
            <button class="btn danger" id="cfYes">确认</button>
          </div>
        </div>
      </div>`;
    const close = (v) => { root.classList.add('hidden'); root.innerHTML = ''; resolve(v); };
    document.getElementById('cfNo').onclick = () => close(false);
    document.getElementById('cfYes').onclick = () => close(true);
    document.getElementById('cfMask').onclick = (e) => { if(e.target.id === 'cfMask') close(false); };
  });
}

// 输入弹窗：resolve(输入串/ null)；Enter 提交，Esc 取消
function promptDialog(title, value, placeholder){
  return new Promise(resolve => {
    const root = document.getElementById('modalRoot');
    root.classList.remove('hidden');
    root.innerHTML = `
      <div class="modal-mask" id="pdMask">
        <div class="modal-card">
          <h3>${esc(title || '输入')}</h3>
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
    const close = (v) => { root.classList.add('hidden'); root.innerHTML = ''; resolve(v); };
    const submit = () => close((input.value || '').trim());
    document.getElementById('pdNo').onclick = () => close(null);
    document.getElementById('pdYes').onclick = submit;
    input.onkeydown = (e) => {
      if (e.key === 'Enter'){ e.preventDefault(); submit(); }
      if (e.key === 'Escape') close(null);
    };
    document.getElementById('pdMask').onclick = (e) => { if(e.target.id === 'pdMask') close(null); };
    setTimeout(() => { input.focus(); input.select(); }, 40);
  });
}

// 表单抽屉：{title, body, wide, onClose} → 返回容器元素；onClose 返回 Promise 时 await 后再销毁
let drawerPrevClose = null; // 当前抽屉的 onClose（被新 openDrawer 覆盖时先触发，如停止日志流）
function openDrawer(opts){
  if (drawerPrevClose){ try{ drawerPrevClose(); }catch(e){} } // 覆盖旧抽屉前先触发其 onClose
  drawerPrevClose = opts.onClose || null;
  const root = document.getElementById('drawerRoot');
  root.classList.remove('hidden');
  root.innerHTML = `
    <div class="drawer-mask" id="dwMask"></div>
    <div class="drawer ${opts.wide ? 'wide' : ''}">
      <div class="drawer-head"><b>${esc(opts.title)}</b>
        <div class="dh-right"><button class="btn sm ghost" id="dwClose">关闭</button></div>
      </div>
      <div class="drawer-body" id="dwBody">${opts.body}</div>
      ${opts.footer ? `<div class="drawer-foot" id="dwFoot">${opts.footer}</div>` : ''}
    </div>`;
  const close = async () => {
    if (opts.onClose) { await opts.onClose(); }
    if (drawerPrevClose === opts.onClose) drawerPrevClose = null; // 仍为当前抽屉的 onClose 才清除
    root.classList.add('hidden'); root.innerHTML = '';
  };
  document.getElementById('dwClose').onclick = close;
  document.getElementById('dwMask').onclick = close;
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

  const syncFace = () => {
    face.classList.toggle('disabled', sel.disabled);
    face.setAttribute('tabindex', sel.disabled ? '-1' : '0');
    val.textContent = sel.selectedOptions.length ? sel.selectedOptions[0].text : '';
  };
  const closePop = () => {
    pop.classList.add('hidden');
    face.classList.remove('open');
    face.setAttribute('aria-expanded', 'false');
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
    [...sel.options].forEach(o => {
      const d = document.createElement('div');
      d.className = 'sel-opt' + (o.selected ? ' on' : '');
      d.textContent = o.text;
      d.onclick = () => {
        sel.value = o.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        syncFace();   // 立即同步面板文本（勿依赖异步的 MutationObserver）
        closePop();
      };
      pop.appendChild(d);
    });
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
      }
    }
  }).observe(document.body, { childList: true, subtree: true });
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', startSelectEnhancer);
else startSelectEnhancer();
