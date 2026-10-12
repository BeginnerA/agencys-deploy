// API 封装：JWT 注入 / 401 跳登录 / 统一 JSON 解析 / 同 URL 在途 GET 合流
function token(){ return localStorage.getItem('token') || ''; }
function currentUser(){ try{ return JSON.parse(localStorage.getItem('user')||'{}'); }catch(e){ return {}; } }

async function _apiCall(path, opts){
  opts.headers = Object.assign({'Authorization':'Bearer '+token(), 'Content-Type':'application/json'}, opts.headers||{});
  const resp = await fetch(path, opts);
  if(resp.status===401){ localStorage.removeItem('token'); localStorage.removeItem('user'); location.href='/'; throw new Error('未登录'); }
  const data = await resp.json().catch(()=>({}));
  if(!resp.ok){ const err = new Error(data.error || ('HTTP '+resp.status)); err.data = data; throw err; }
  return data;
}

// 在途 GET 合流：一个事件到达时常常多个绑定（页面列表 + 全局角标）同时重取同一个
// URL，发出去就是重复请求。同一 URL 的 GET 在途时共用一个 Promise（失败也共享，
// 各自 catch）；任何变更类请求一律清空合流表，避免变更后误读到变更前发出的旧快照。
const _inflight = new Map();
async function api(path, opts={}){
  const method = String(opts.method || 'GET').toUpperCase();
  if (method !== 'GET'){ _inflight.clear(); return _apiCall(path, opts); }
  if (_inflight.has(path)) return _inflight.get(path);
  const p = _apiCall(path, opts).finally(() => { if (_inflight.get(path) === p) _inflight.delete(path); });
  _inflight.set(path, p);
  return p;
}
function logout(){ localStorage.removeItem('token'); localStorage.removeItem('user'); location.href='/'; }

// 注：原先这里的 poll() 盲轮询封装已删除——全站取数改走 live.js 的 liveBind（事件驱动 + 降级兜底），
// 保留它只会诱导新页面退回定时轮询。子进程日志文件的增量追尾仍需要定时器的两处（发布抽屉、日志中心）
// 直接用自己的 setInterval，并在注释里写明「为何必须保留」。
// 追加：当前用户（含 role 快捷判断）
function isAdmin(){ return currentUser().role === 'admin'; }
function canDeploy(){ return ['admin','operator'].includes(currentUser().role); }
