// config-home.js — 配置工作台首页（统计卡片/快速开始/最近项目/空态）+ 用户管理视图（#/config/users）
(function(){
  'use strict';
  const { touchRecent, getRecent } = window.cfgHelpers;

  // 工作台：统计卡片 + 快速开始 + 最近项目 + 空态引导
  async function renderHome(root){
    root.innerHTML = '<div class="cfg-home">' + skeletonHTML(5) + '</div>';
    try{
      const [projects, servers, users] = await Promise.all([
        api('/api/projects'), api('/api/servers'), api('/api/users'),
      ]);
      const total = {
        services: projects.reduce((a, p) => a + (p.services || 0), 0),
        environments: projects.reduce((a, p) => a + (p.environments || 0), 0),
        pipelines: projects.reduce((a, p) => a + (p.pipelines || 0), 0),
      };
      const okCnt = servers.filter(s => s.conn && s.conn.ok).length;
      const errCnt = servers.filter(s => s.conn && !s.conn.ok).length;
      const recent = getRecent().filter(r => projects.some(p => p.id === r.pid));
      const firstPid = projects.length ? projects[0].id : null;
      const goProject = pid => { location.hash = '#/config/p/' + pid; };

      const statCards = [
        { ico:'🗂️', num: projects.length, label:'项目', tip: projects.length > 1 ? '点击选择要进入的项目' : '点击进入项目仪表盘', go: () => { if (firstPid) goProject(firstPid); } },
        { ico:'🧩', num: total.services, label:'服务', tip:'全部项目服务总数', go: () => { if (firstPid) location.hash = '#/config/p/' + firstPid + '/services'; } },
        { ico:'🌐', num: total.environments, label:'环境', tip:'全部项目环境总数', go: () => { if (firstPid) location.hash = '#/config/p/' + firstPid + '/environments'; } },
        { ico:'🔁', num: total.pipelines, label:'流水线', tip:'全部项目流水线总数', go: () => { if (firstPid) location.hash = '#/config/p/' + firstPid + '/pipelines'; } },
        { ico:'🖥️', num: servers.length, label:'服务器', sub: `SSH 正常 ${okCnt}${errCnt ? ' · 异常 ' + errCnt : ''}`, tip:'平台级资源，环境中选择绑定', go: () => { location.hash = '#/config/servers'; } },
        { ico:'👤', num: users.length, label:'用户', tip:'账号与角色管理', go: () => { location.hash = '#/config/users'; } },
        { ico:'📡', num: '-', label:'通知渠道', sub:'6 种渠道', tip:'告警通知发送配置', go: () => { location.hash = '#/config/channels'; } },
      ];

      // 快速开始跳转：需项目上下文；无项目时引导新建
      const gotoAuto = key => {
        if (!firstPid){
          toastError('请先创建项目，再创建服务/环境/流水线');
          openProjectFormSafe(root);
          return;
        }
        sessionStorage.setItem('cfgAutoOpen', key);
        const subMap = { service:'services', env:'environments', pipeline:'pipelines' };
        location.hash = '#/config/p/' + firstPid + '/' + (subMap[key] || 'services');
      };
      const quickCards = [
        { ico:'🧩', title:'新建服务', desc:'注册代码仓库与构建方式，供流水线编排', time:'约 2 分钟', go: () => gotoAuto('service') },
        { ico:'🌐', title:'新建环境', desc:'模板一键填充，只需改服务器地址等差异项', time:'约 3 分钟', go: () => gotoAuto('env') },
        { ico:'🔁', title:'编排流水线', desc:'拖拽服务到阶段，自动生成发布命令', time:'约 2 分钟', go: () => gotoAuto('pipeline') },
        { ico:'🖥️', title:'添加服务器', desc:'填写 IP 与登录方式，保存时自动测试连接', time:'约 1 分钟', go: () => { location.hash = '#/config/servers'; sessionStorage.setItem('cfgAutoOpen', 'server'); } },
      ];

      root.innerHTML = `<div class="cfg-home">
        <div style="display:flex;align-items:center;gap:12px">
          <div class="page-title">配置工作台</div>
          <button class="btn sm primary" id="cfgNewProjTop">＋ 新建项目</button>
        </div>
        <div class="page-sub">可视化运维创作：先建项目，再按「服务器 → 服务 → 环境 → 流水线」顺序配置</div>
        ${projects.length === 0 ? `
        <div class="onboard-card">
          <div style="font-size:15px;font-weight:700;color:var(--text-1)">🚀 还没有项目，推荐按这个顺序开始</div>
          <div class="onboard-steps">
            <span>① 新建项目</span><span class="os-arrow">→</span>
            <span>② 添加服务器</span><span class="os-arrow">→</span>
            <span>③ 新建服务</span><span class="os-arrow">→</span>
            <span>④ 新建环境并绑定服务器</span><span class="os-arrow">→</span>
            <span>⑤ 编排流水线</span>
          </div>
          <button class="btn primary" id="cfgNewProj">＋ 新建项目</button>
        </div>` : ''}
        <div>
          <div class="section-title">平台概览 <span class="tag">共 ${projects.length} 个项目</span></div>
          <div class="stat-grid">${statCards.map(c => `
            <div class="stat-card" data-stat>
              <div class="st-ico">${c.ico}</div>
              <div class="st-num">${c.num}${c.sub ? `<span class="sub">${esc(c.sub)}</span>` : ''}</div>
              <div class="st-label">${esc(c.label)}</div>
              <div class="st-tip">${esc(c.tip || '')}</div>
            </div>`).join('')}
          </div>
        </div>
        <div>
          <div class="section-title">快速开始 <span class="tag">新手从这里下手</span></div>
          <div class="quick-grid">${quickCards.map(c => `
            <div class="quick-card" data-quick>
              <div class="qc-head"><span class="qc-ico">${c.ico}</span><span class="qc-title">${esc(c.title)}</span></div>
              <div class="qc-desc">${esc(c.desc)}</div>
              <div class="qc-time">⏱ ${esc(c.time)}</div>
            </div>`).join('')}
          </div>
        </div>
        ${recent.length ? `
        <div>
          <div class="section-title">最近使用的项目</div>
          <div class="recent-grid">${recent.map(r => `
            <div class="recent-item" data-recent="${r.pid}">
              <span class="ri-ico">🗂️</span>
              <div><div class="ri-name">${esc(r.name)}</div><div class="ri-meta">最近访问 ${fmtTime(Math.floor(r.ts / 1000))}</div></div>
            </div>`).join('')}
          </div>
        </div>` : ''}
      </div>`;

      // 项目统计卡：多项目时点击弹出项目选择浮层，单项目直接进入
      function pickProjectPop(cardEl){
        const r = cardEl.getBoundingClientRect();
        const pop = document.createElement('div');
        pop.className = 'drop-panel';
        pop.style.top = (r.bottom + 6) + 'px';
        pop.style.left = r.left + 'px';
        pop.style.minWidth = '240px';
        pop.style.display = 'block';
        pop.innerHTML = projects.map(p => `
          <div class="drop-item">🗂️ <b>${esc(p.name)}</b><span class="k">${esc(p.code || '')} · ${p.services || 0} 个服务</span></div>`).join('');
        [...pop.children].forEach((el, i) => el.onclick = () => { pop.remove(); goProject(projects[i].id); });
        document.body.appendChild(pop);
        // 延迟注册外层关闭，避免本次点击冒泡立即误关
        setTimeout(() => {
          const close = e => { if (!pop.contains(e.target)){ pop.remove(); document.removeEventListener('click', close, true); } };
          document.addEventListener('click', close, true);
        }, 0);
      }
      root.querySelectorAll('[data-stat]').forEach((el, i) => {
        el.onclick = i === 0 && projects.length > 1 ? () => pickProjectPop(el) : () => statCards[i].go();
      });
      root.querySelectorAll('[data-quick]').forEach((el, i) => el.onclick = () => quickCards[i].go());
      root.querySelectorAll('[data-recent]').forEach(el => {
        el.onclick = () => { const p = projects.find(x => x.id === Number(el.dataset.recent)); if (p){ touchRecent(p.id, p.name); goProject(p.id); } };
      });
      const np = document.getElementById('cfgNewProj');
      const npTop = document.getElementById('cfgNewProjTop');
      if (np) np.onclick = () => openProjectFormSafe(root);
      if (npTop) npTop.onclick = () => openProjectFormSafe(root);
    }catch(e){
      root.innerHTML = errorState('配置工作台加载失败：' + esc(e.message), 'navRetry');
    }
  }
  // 新建项目入口：cfgProject 由 Task 5 模块提供，未就绪时给出明确提示
  function openProjectFormSafe(root){
    if (!window.cfgProject || !window.cfgProject.openProjectForm){
      toastError('项目模块未就绪，请稍后重试');
      return;
    }
    window.cfgProject.openProjectForm(root, null, () => location.hash = '#/config');
  }

  // ---- 用户管理视图（#/config/users） ----
  const ROLE_CN = { admin: '管理员', operator: '操作员', viewer: '只读' };
  async function renderUsers(root){
    root.innerHTML = '<div class="cfg-home">' + skeletonHTML(3) + '</div>';
    try{
      const us = await api('/api/users');
      const me = currentUser().username;
      root.innerHTML = `<div class="cfg-home">
        <div style="display:flex;align-items:center;gap:12px">
          <div class="page-title">用户管理</div>
          <button class="btn sm ghost" onclick="location.hash='#/config'">← 返回工作台</button>
        </div>
        <div class="deploy-toolbar"><span style="flex:1"></span>
          <button class="btn sm primary" id="addUser">+ 新建用户</button>
        </div>
        <div class="panel" style="padding:0">${us.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr>
          <th>用户名</th><th>角色</th><th>启用</th><th>强制改密</th><th>操作</th></tr></thead><tbody>
          ${us.map(u => `<tr>
            <td>${esc(u.username)}${u.username === me ? ' <span class="weak">(我)</span>' : ''}</td>
            <td>${ROLE_CN[u.role] || esc(u.role)}</td>
            <td>${u.enabled ? '<span class="ok">启用</span>' : '<span class="weak">停用</span>'}</td>
            <td>${u.must_change_password ? '<span class="badge warning">是</span>' : '-'}</td>
            <td><button class="btn sm ghost" data-edit="${u.id}">编辑</button>
                <button class="btn sm danger" data-del="${u.id}">删除</button></td>
          </tr>`).join('')}</tbody></table></div>`
        : `<div class="empty-state"><div class="e-ico">👤</div><div>暂无用户</div></div>`}</div>
      </div>`;
      document.getElementById('addUser').onclick = () => openUserForm(root, null);
      root.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => openUserForm(root, us.find(x => x.id === Number(b.dataset.edit))));
      root.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
        const u = us.find(x => x.id === Number(b.dataset.del));
        if (!await confirmDialog(`确认删除用户「${u.username}」？`)) return;
        try{ await api('/api/users/' + u.id, { method: 'DELETE' }); toastSuccess('用户已删除'); await renderUsers(root); }
        catch(err){ toastError(err.message); }
      });
    }catch(e){
      root.innerHTML = errorState('用户列表加载失败：' + esc(e.message), 'navRetry');
    }
  }
  function openUserForm(root, u){
    openDrawer({ title: u ? '编辑用户：' + u.username : '新建用户', body: `
      <div class="form-grid">
        <div class="field"><label>用户名 *</label><input id="uName" value="${esc(u ? u.username : '')}" ${u ? 'disabled' : ''}></div>
        <div class="field"><label>${u ? '重置密码（留空不修改）' : '初始密码 *（至少 6 位）'}</label><input id="uPwd" type="password"></div>
        <div class="field"><label>角色</label><select id="uRole">
          ${Object.entries(ROLE_CN).map(([k, v]) => `<option value="${k}" ${(u && u.role === k) || (!u && k === 'viewer') ? 'selected' : ''}>${v}</option>`).join('')}
        </select></div>
        <div class="field check" style="align-self:end"><label><input type="checkbox" id="uEnabled" ${!u || u.enabled ? 'checked' : ''}> 启用</label></div>
      </div><div style="display:flex;gap:10px;margin-top:20px"><button class="btn primary flex-1" id="cfSubmit">${u ? '保存' : '创建'}</button></div>` });
    document.getElementById('cfSubmit').onclick = async () => {
      try{
        const name = val('uName').trim();
        const pwd = val('uPwd');
        if (!u){
          if (!name || pwd.length < 6) throw new Error('用户名必填，密码至少 6 位');
          await api('/api/users', { method: 'POST', body: JSON.stringify({ username: name, password: pwd, role: val('uRole') }) });
          toastSuccess('用户已创建（首登强制改密）');
        }else{
          const payload = { role: val('uRole'), enabled: chk('uEnabled') };
          if (pwd) payload.password = pwd;
          await api('/api/users/' + u.id, { method: 'PUT', body: JSON.stringify(payload) });
          toastSuccess('用户已更新');
        }
        const dr = document.getElementById('drawerRoot');
        dr.classList.add('hidden'); dr.innerHTML = '';
        await renderUsers(root);
      }catch(err){ toastError(err.message); }
    };
  }
  // ---- 通知渠道配置视图（#/config/channels） ----
  const CHANNEL_META = {
    email:      { ico:'📧', name:'邮件', desc:'SMTP 邮件通知' },
    wecom:      { ico:'💬', name:'企业微信', desc:'企业微信机器人' },
    webhook:    { ico:'🔗', name:'通用 Webhook', desc:'HTTP POST 回调' },
    sms_aliyun: { ico:'📱', name:'阿里云短信', desc:'阿里云 SMS 服务' },
    feishu:     { ico:'🐦', name:'飞书', desc:'飞书机器人' },
    dingtalk:   { ico:'🔔', name:'钉钉', desc:'钉钉机器人' },
  };
  // 各渠道表单字段定义：{key, label, type, placeholder, required}
  const CHANNEL_FIELDS = {
    email: [
      { key:'host', label:'SMTP 服务器', placeholder:'smtp.example.com' },
      { key:'port', label:'端口', placeholder:'465', type:'number' },
      { key:'user', label:'用户名', placeholder:'user@example.com' },
      { key:'password', label:'密码', type:'password', masked:true },
      { key:'from_addr', label:'发件人', placeholder:'noreply@example.com' },
      { key:'to_addrs', label:'收件人', placeholder:'a@example.com,b@example.com' },
    ],
    wecom: [
      { key:'webhook', label:'Webhook URL', placeholder:'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...' },
    ],
    webhook: [
      { key:'url', label:'回调 URL', placeholder:'https://your-server/webhook' },
    ],
    sms_aliyun: [
      { key:'access_key', label:'AccessKey ID', type:'password', masked:true },
      { key:'secret', label:'AccessKey Secret', type:'password', masked:true },
      { key:'sign', label:'签名', placeholder:'短信签名' },
      { key:'template', label:'模板 CODE', placeholder:'SMS_123456' },
      { key:'phones', label:'手机号', placeholder:'13800001111,13900002222' },
      { key:'region', label:'地域', placeholder:'cn-hangzhou' },
    ],
    feishu: [
      { key:'webhook', label:'Webhook URL', placeholder:'https://open.feishu.cn/open-apis/bot/v2/hook/...' },
    ],
    dingtalk: [
      { key:'webhook', label:'Webhook URL', placeholder:'https://oapi.dingtalk.com/robot/send?access_token=...' },
      { key:'secret', label:'加签密钥', type:'password', masked:true },
    ],
  };

  async function renderChannels(root){
    root.innerHTML = '<div class="cfg-home">' + skeletonHTML(4) + '</div>';
    try{
      const cfgs = await api('/api/alerts/channels');
      root.innerHTML = `<div class="cfg-home">
        <div style="display:flex;align-items:center;gap:12px">
          <div class="page-title">通知渠道配置</div>
          <button class="btn sm ghost" onclick="location.hash='#/config'">← 返回工作台</button>
        </div>
        <div class="page-sub">配置告警通知发送渠道，敏感字段（密码/密钥）打码显示，编辑时留空则保留原值</div>
        <div class="ch-grid" id="chGrid"></div>
        <div style="display:flex;justify-content:flex-end;margin-top:var(--sp-4)">
          <button class="btn primary" id="chSaveAll">💾 保存全部渠道</button>
        </div>
      </div>`;
      const grid = document.getElementById('chGrid');
      for (const [ch, meta] of Object.entries(CHANNEL_META)){
        const cfg = cfgs[ch] || {};
        const fields = CHANNEL_FIELDS[ch] || [];
        const hasData = Object.values(cfg).some(v => v && v !== '******');
        const card = document.createElement('div');
        card.className = 'ch-card' + (hasData ? ' configured' : '');
        card.dataset.channel = ch;
        card.innerHTML = `
          <div class="ch-head">
            <span class="ch-ico">${meta.ico}</span>
            <div><div class="ch-name">${meta.name}</div><div class="ch-desc">${meta.desc}</div></div>
            ${hasData ? '<span class="badge success">已配置</span>' : '<span class="badge">未配置</span>'}
            ${hasData ? `<button class="btn sm ghost ch-test-btn" data-ch="${ch}" title="发送测试消息验证配置">🧪 测试</button>` : ''}
          </div>
          <div class="form-grid">${fields.map(f => {
            const v = cfg[f.key] || '';
            const isMasked = f.masked && v === '******';
            const displayVal = isMasked ? '' : esc(v);
            const ph = isMasked ? '已配置，留空保留原值' : esc(f.placeholder || '');
            return `<div class="field">
              <label>${esc(f.label)}${f.masked ? ' <span class="weak">（留空保留原值）</span>' : ''}</label>
              <input data-ch="${ch}" data-key="${f.key}" type="${f.type === 'password' ? 'password' : f.type === 'number' ? 'number' : 'text'}" placeholder="${ph}" value="${displayVal}">
            </div>`;
          }).join('')}</div>`;
        grid.appendChild(card);
      }
      // 渠道测试按钮
      grid.querySelectorAll('.ch-test-btn').forEach(btn => {
        btn.onclick = async () => {
          const ch = btn.dataset.ch;
          const origText = btn.innerHTML;
          btn.disabled = true;
          btn.innerHTML = '⏳ 测试中...';
          try {
            const res = await api('/api/alerts/channels/' + ch + '/test', { method: 'POST' });
            if (res.ok) {
              toastSuccess('渠道测试成功，请检查接收端');
            } else {
              toastError('测试失败: ' + (res.detail || '未知错误'));
            }
          } catch (e) {
            toastError('测试失败: ' + e.message);
          } finally {
            btn.disabled = false;
            btn.innerHTML = origText;
          }
        };
      });
      document.getElementById('chSaveAll').onclick = async () => {
        const payload = {};
        grid.querySelectorAll('input[data-ch]').forEach(inp => {
          const ch = inp.dataset.ch, key = inp.dataset.key;
          if (!payload[ch]) payload[ch] = {};
          payload[ch][key] = inp.value;
        });
        try{
          await api('/api/alerts/channels', { method:'PUT', body: JSON.stringify(payload) });
          toastSuccess('渠道配置已保存');
          await renderChannels(root);
        }catch(e){ toastError(e.message); }
      };
    }catch(e){
      root.innerHTML = errorState('渠道配置加载失败：' + esc(e.message), 'navRetry');
    }
  }

  // 局部工具（与旧 config.js 同款，避免依赖被删文件）
  function val(id){ return document.getElementById(id).value; }
  function chk(id){ return document.getElementById(id).checked; }

  window.cfgHome = {
    render: async function(root, view){
      if (view === 'users'){ await renderUsers(root); return []; }
      if (view === 'channels'){ await renderChannels(root); return []; }
      await renderHome(root);
      return []; 
    },
  };
})();
