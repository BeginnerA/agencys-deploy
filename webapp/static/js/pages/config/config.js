// config.js — 配置管理入口：按 hash 子路由分发到工作台 / 项目仪表盘 / 用户管理
(function(){
  'use strict';
  async function render(root){
    // split('?')[0] 归一化 query：带参 hash 不应影响子路由解析
    const parts = (location.hash || '#/config').split('?')[0].replace(/^#\/?/, '').split('/').filter(Boolean);
    if (parts[1] === 'p'){
      // 缺 pid 与非法 pid 行为统一：链接损坏直接报错，不静默回落工作台
      const pid = Number(parts[2]);
      if (!Number.isInteger(pid) || pid <= 0) throw new Error('项目 ID 无效');
      return await window.cfgProject.render(root, pid, parts[3] || 'overview');
    }
    if (parts[1] === 'users') return await window.cfgHome.render(root, 'users');
    if (parts[1] === 'channels') return await window.cfgHome.render(root, 'channels');
    if (parts[1] === 'servers'){
      // 平台级服务器入口（工作台统计卡）：跳转到第一个项目的服务器子页
      const ps = await api('/api/projects');
      if (ps.length){ location.hash = `#/config/p/${ps[0].id}/servers`; return []; }
      return await window.cfgHome.render(root, 'home');
    }
    return await window.cfgHome.render(root, 'home');
  }
  window.config = { render, destroy: function(){ /* 无定时器/图表 */ } };
})();
