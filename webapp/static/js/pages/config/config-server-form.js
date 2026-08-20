// config-server-form.js — 服务器新增/编辑：凭据继承开关 + 就地校验 + 保存并测试 SSH
(function(){
  'use strict';
  const H = window.cfgHelpers;

  function open(container, srv, onSaved){
    const st = {
      container, onSaved, srv,
      // 覆盖形态含 ssh_port 单独覆盖与密码禁用（''）：缺失判据会误判继承并在保存时静默清空
      inherit: !(srv && (srv.ssh_user || srv.ssh_key_file ||
        (srv.ssh_password ?? null) !== null || (srv.ssh_port ?? null) !== null)),
      authMode: (srv && srv.ssh_password && !srv.ssh_key_file) ? 'pwd' : 'key',
      others: [],
      testing: false, testOk: null, testMsg: null,
    };
    openDrawer({ title: srv ? '编辑服务器：' + srv.host : '添加服务器', wide: true,
      body: '<div class="cfg-home">' + skeletonHTML(3) + '</div>', onClose: null });
    const bodyEl = document.getElementById('dwBody');
    (async () => {
      try{
        st.others = (await api('/api/servers')).filter(x => !srv || x.id !== srv.id);
        render();
      }catch(e){ bodyEl.innerHTML = errorState('加载失败：' + esc(e.message), 'navRetry'); }
    })();

    function render(){
      const pwdMasked = srv && srv.ssh_password === '***';
      const hasCovered = srv && (srv.ssh_user || srv.ssh_key_file ||
        (srv.ssh_password ?? null) !== null || (srv.ssh_port ?? null) !== null);
      const sshBody = st.inherit
        ? `<div class="f-hint">本台服务器将使用环境级 SSH 凭据（root@22 · 密钥 ~/.ssh/id_rsa_deploy）；未绑定环境时回退默认 root/22。大部分服务器共用一套登录方式，无需逐台填写。
            ${hasCovered ? '<div style="color:var(--warning);margin-top:6px">⚠ 本台存在端口/密码覆盖配置，保存后将被清空（如需保留请取消继承）</div>' : ''}
          </div>`
        : `<div class="form-grid">
            ${H.field({ label:'登录用户', input:`<input id="sUser" value="${esc(srv ? srv.ssh_user || '' : '')}" placeholder="如 root">`, hint:'留空 = 使用环境级用户' })}
            ${H.field({ label:'SSH 端口', input:`<input id="sPort" type="number" value="${esc(srv && srv.ssh_port ? srv.ssh_port : '')}" placeholder="22">`, hint:'留空 = 使用环境级端口' })}
            <div class="field full"><label>连接方式</label>
              <select id="sAuth"><option value="key" ${st.authMode === 'key' ? 'selected' : ''}>密钥（推荐）</option><option value="pwd" ${st.authMode === 'pwd' ? 'selected' : ''}>密码</option></select>
            </div>
            ${st.authMode === 'key'
              ? H.field({ label:'私钥路径', tip:'未配置密码时使用', input:`<input id="sKey" value="${esc(srv ? srv.ssh_key_file || '' : '')}" placeholder="如 ~/.ssh/id_rsa_deploy">`, hint:'留空 = 使用环境级密钥' })
              : H.field({ label:'密码', tip:'密码优先、失败自动回退密钥', input:`<input id="sPwd" type="password" value="" placeholder="${pwdMasked ? '已遮蔽，留空不修改' : '敏感密码建议环境变量 SSH_PASSWORD'}">`, hint: pwdMasked ? '原密码已遮蔽：留空不修改，保存后保留原密码' : '留空 = 不配置密码（仅用密钥）；敏感密码建议环境变量 SSH_PASSWORD' })}
          </div>`;
      bodyEl.innerHTML = `
        <div class="form-grid">
          ${H.field({ label:'服务器 IP', required:true, tip:'SSH 连接用地址，全平台唯一', input:`<input id="sHost" value="${esc(srv ? srv.host : '')}" placeholder="如 10.0.0.11">` })}
          ${H.field({ label:'Nacos 注册 IP（nacos_ip）', tip:'NAT 环境下与外网 IP 不同才填', input:`<input id="sNacosIp" value="${esc(srv ? srv.nacos_ip || '' : '')}" placeholder="留空 = 使用服务器 IP">`, hint:'NAT 环境下服务在 Nacos 中注册的内网 IP，与上方 IP 相同则留空' })}
        </div>
        <div class="cfg-section"><div class="cs-head"><span class="cs-ico">🔑</span><span class="cs-title">SSH 连接凭据</span><span class="cs-desc">大部分服务器共用环境级凭据</span></div>
          <div class="cs-body">
            <label class="mode-switch" style="margin-bottom:10px"><input type="checkbox" id="sInherit" ${st.inherit ? 'checked' : ''}> 使用环境级凭据（不单独配置）</label>
            ${sshBody}
          </div>
        </div>
        <div class="field full"><label>备注</label><textarea id="sRemark" rows="2" placeholder="仅用于识别服务器用途，不影响发布逻辑">${esc(srv ? srv.remark || '' : '')}</textarea></div>
        <div id="testPanel"></div>
        <div style="display:flex;gap:10px;margin-top:16px">
          <button class="btn primary flex-1" id="srvSave">保存并测试连接</button>
          <button class="btn ghost" id="srvSaveOnly" style="display:none">仍要保存</button>
          <button class="btn ghost" id="srvRetest" style="display:none">重新测试</button>
        </div>`;

      document.getElementById('sInherit').onchange = e => { st.inherit = e.target.checked; render(); };
      const auth = document.getElementById('sAuth');
      if (auth) auth.onchange = e => { st.authMode = e.target.value; render(); };
      document.getElementById('srvSave').onclick = async () => {
        if (!validate()) return;
        const btn = document.getElementById('srvSave');
        btn.disabled = true; btn.textContent = '⏳ 测试中…';
        await runTest();
        btn.disabled = false; btn.textContent = '保存并测试连接';
        if (st.testOk) await save();
      };
      document.getElementById('srvRetest').onclick = () => { if (validate()) runTest(); };
      document.getElementById('srvSaveOnly').onclick = () => { if (validate()) save(); };
      renderTest();
    }

    // ---- 就地校验（不弹窗、光标留在原处） ----
    function validate(){
      let ok = true;
      const host = val('sHost').trim();
      if (!host){ H.fieldErr('sHost', '服务器 IP 或域名必填'); ok = false; }
      else if (!H.ipValid(host)){ H.fieldErr('sHost', 'IP 格式错误（IPv4 各段 0~255）或域名无效'); ok = false; }
      else if (st.others.some(x => x.host === host)){ H.fieldErr('sHost', '该 IP 已存在（全平台唯一）'); ok = false; }
      else H.fieldErr('sHost', '');
      const port = val('sPort').trim();
      if (!st.inherit && port && (!Number.isInteger(Number(port)) || Number(port) < 1 || Number(port) > 65535)){
        H.fieldErr('sPort', '端口范围 1~65535'); ok = false;
      }else H.fieldErr('sPort', '');
      return ok;
    }

    // ---- 收集提交载荷（对齐后端三态：不传/NULL = 继承环境级） ----
    function collectPayload(){
      const p = { host: val('sHost').trim(), nacos_ip: val('sNacosIp').trim(), remark: val('sRemark').trim() };
      if (st.inherit){
        // 从覆盖切回继承：显式置 null 清空覆盖（后端为字段级更新，不提交不会清除）
        p.ssh_user = null; p.ssh_port = null; p.ssh_password = null; p.ssh_key_file = null;
      }else{
        p.ssh_user = val('sUser').trim() || null;
        p.ssh_port = val('sPort').trim() ? Number(val('sPort').trim()) : null;
        if (st.authMode === 'key'){
          const kf = val('sKey').trim();
          if (kf) p.ssh_key_file = kf;
          else if (!(srv && srv.ssh_key_file)) p.ssh_key_file = null;   // 编辑态已有密钥且留空 = 保留（不提交）
          // 遮蔽密码不触碰（保留原密码）；无遮蔽时空串 = 禁用密码认证、仅用密钥
          if (!(srv && srv.ssh_password === '***')) p.ssh_password = '';
        }else{
          const pw = val('sPwd');
          if (pw) p.ssh_password = pw;
          else if (srv && srv.ssh_password === '***'){ /* 留空 = 保留原密码，不提交 */ }
          else p.ssh_password = '';   // 空串 = 该台禁用密码认证、仅用密钥
          // 切到密码模式：已有密钥保留（密码失败自动回退密钥），不提交
          if (!(srv && srv.ssh_key_file)) p.ssh_key_file = null;
        }
      }
      return p;
    }

    // ---- 连接测试（POST /servers/test，单接口总体结果） ----
    async function runTest(){
      const t = { host: val('sHost').trim() };
      if (!st.inherit){
        const user = val('sUser').trim(), port = val('sPort').trim();
        if (user) t.ssh = Object.assign(t.ssh || {}, { user });
        if (st.authMode === 'key'){ const kf = val('sKey').trim(); if (kf) t.ssh = Object.assign(t.ssh || {}, { key_file: kf }); }
        else { const pw = val('sPwd'); if (pw) t.ssh = Object.assign(t.ssh || {}, { password: pw }); }
        if (port) t.ssh_port = Number(port);
      }
      if (srv && srv.ssh_password === '***' && !st.inherit && !val('sPwd')) t.use_stored_password = true;
      st.testing = true; renderTest();
      try{
        const r = await api('/api/servers/test', { method: 'POST', body: JSON.stringify(t) });
        st.testOk = r.ok; st.testMsg = r.message;
      }catch(e){ st.testOk = false; st.testMsg = e.message; }
      st.testing = false; renderTest();
    }

    // ---- 失败建议：按 message 关键字给可执行文案 ----
    function advice(msg){
      const m = String(msg || '');
      if (/timed out|refused|unreachable|connect.*fail/i.test(m)) return '网络不可达：检查服务器是否开机、防火墙是否放行 SSH 端口（默认 22）。';
      if (/permission denied|authentication|auth|key.*denied/i.test(m)) return '认证失败：确认登录用户与凭据；推荐把 ~/.ssh/id_rsa_deploy.pub 添加到该服务器 root 的 authorized_keys 后重新测试。';
      if (/no such file|invalid.*key|parse.*key/i.test(m)) return '私钥无效：检查密钥文件路径与内容（应为 OpenSSH 格式私钥）。';
      return '连接失败：按上方错误信息排查（网络 / 端口 / 凭据），可稍后重新测试，或选择「仍要保存」稍后处理。';
    }

    function renderTest(){
      const tp = document.getElementById('testPanel');
      if (!tp) return;
      const rt = document.getElementById('srvRetest'), so = document.getElementById('srvSaveOnly');
      if (st.testing){
        tp.innerHTML = '<div class="test-panel"><div class="tp-row tp-wait">⏳ 正在测试 SSH 连接…</div></div>';
        if (rt) rt.disabled = true;
        if (so) so.disabled = true;
        return;
      }
      if (st.testMsg === null){ tp.innerHTML = ''; return; }
      tp.innerHTML = `
        <div class="test-panel">
          <div class="tp-row ${st.testOk ? 'tp-ok' : 'tp-fail'}">${st.testOk ? '✓' : '✗'} ${esc(st.testMsg)}</div>
          ${st.testOk ? '<div class="tp-advice" style="color:var(--success)">服务器连接正常，可安全保存。</div>' : `<div class="tp-advice">💡 ${esc(advice(st.testMsg))}</div>`}
        </div>`;
      rt.style.display = st.testOk ? 'none' : 'inline-block';
      so.style.display = st.testOk ? 'none' : 'inline-block';
      rt.disabled = false; so.disabled = false;
    }

    async function save(){
      const p = collectPayload();
      try{
        if (srv) await api('/api/servers/' + srv.id, { method: 'PUT', body: JSON.stringify(p) });
        else await api('/api/servers', { method: 'POST', body: JSON.stringify(p) });
        toastSuccess(srv ? '服务器已更新' : '服务器已添加');
        const dr = document.getElementById('drawerRoot');
        dr.classList.add('hidden'); dr.innerHTML = '';
        if (onSaved){ await onSaved(); showGuide(); }
        else navRetry();
      }catch(e){ toastError(e.message); }
    }

    // ---- 保存成功引导：下一步建议 + 去绑定环境 ----
    function showGuide(){
      container.insertAdjacentHTML('afterbegin', `
        <div class="guide-steps" id="srvGuide">
          <span class="gs-item done">① 添加服务器 ✓</span><span class="gs-arrow">→</span>
          <span class="gs-item">② 新建服务</span><span class="gs-arrow">→</span>
          <span class="gs-item">③ 新建环境并绑定</span><span class="gs-arrow">→</span>
          <span class="gs-item">④ 编排流水线</span>
          <button class="btn sm primary" id="goBindEnv" style="margin-left:auto">去绑定环境 →</button>
        </div>`);
      document.getElementById('goBindEnv').onclick = async () => {
        try{
          const ps = await api('/api/projects');
          const p = ps[0];
          if (p){ sessionStorage.setItem('cfgAutoOpen', 'env'); location.hash = `#/config/p/${p.id}/environments`; }
          else { toastError('还没有项目，请先创建项目'); location.hash = '#/config'; }
        }catch(e){ toastError(e.message); }
      };
    }

    function val(id){ const el = document.getElementById(id); return el ? el.value : ''; }
  }

  window.cfgServerForm = { open };
})();
