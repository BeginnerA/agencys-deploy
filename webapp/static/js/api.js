// API 封装：JWT 注入 / 401 跳登录 / 统一 JSON 解析
function token(){ return localStorage.getItem('token') || ''; }
function currentUser(){ try{ return JSON.parse(localStorage.getItem('user')||'{}'); }catch(e){ return {}; } }

async function api(path, opts={}){
  opts.headers = Object.assign({'Authorization':'Bearer '+token(), 'Content-Type':'application/json'}, opts.headers||{});
  const resp = await fetch(path, opts);
  if(resp.status===401){ localStorage.removeItem('token'); localStorage.removeItem('user'); location.href='/'; throw new Error('未登录'); }
  const data = await resp.json().catch(()=>({}));
  if(!resp.ok) throw new Error(data.error || ('HTTP '+resp.status));
  return data;
}
function logout(){ localStorage.removeItem('token'); localStorage.removeItem('user'); location.href='/'; }

// 追加：轮询封装（错误静默，避免轮询失败刷屏 toast）
async function poll(fn, intervalMs){
  const tick = async () => { try{ await fn(); }catch(e){ /* 轮询失败静默 */ } };
  await tick();
  return setInterval(tick, intervalMs);
}
// 追加：当前用户（含 role 快捷判断）
function isAdmin(){ return currentUser().role === 'admin'; }
function canDeploy(){ return ['admin','operator'].includes(currentUser().role); }
