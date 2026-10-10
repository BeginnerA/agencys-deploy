// live.js — 全局实时通道客户端（消费后端 GET /api/stream SSE，帧协议见 webapp/routes/stream_routes.py）
//
// 解决的问题：原先 7 处各自独立的 setInterval 盲轮询（最密 3s / 次密 5s），
// 事件发生与界面刷新之间最多差一整轮，而且绝大多数轮询都在空转打后端。
// 本模块把「轮询驱动」翻转为「事件驱动 + 降级轮询」两态：
//   · 通道在线：收到 topic 事件（同绑定 400ms 合并）立即调用刷新函数，并关掉兜底定时器
//   · 通道离线 / 服务端限流 / 页面隐藏：按调用方给的 fallbackMs 退回轮询
//   任何时刻都至少在驱动刷新一次，不存在「既不推也不轮」的死角。
//
// 为什么用 fetch + ReadableStream 而不是 EventSource：需要携带 Authorization 头
// （EventSource 不能自定义请求头，与本平台 JWT 认证不兼容），与 agent-stream.js 同一范式。
(function(){
  const TOPICS = ['monitor', 'alerts', 'tasks', 'agent', 'logs'];
  const COALESCE_MS = 400;          // 同绑定短时间多事件合并窗口
  const BACKOFF_MAX_MS = 30000;     // 重连退避上限
  const CAP_BACKOFF_MS = 30000;     // 服务端拒接（订阅已满）时的退避下限，防形成重连风暴
  const FALLBACK_WHEN_HIDDEN_MS = 60000;  // 页面隐藏时兜底轮询降频（省请求，回前台立即重连）

  let state = 'init';               // init | connecting | online | degraded
  let ctrl = null, retryTimer = null, attempt = 0, started = false;
  let capped = false;               // 上一轮被服务端拒接（收不到 hello）
  let lastEventTs = 0;
  const binds = {};                 // id -> {topic, fn, fallbackMs, timer, lastRun, trail, dead}
  const watchers = [];              // 状态变化订阅（顶栏指示灯用）
  let bindSeq = 0;

  function setState(next){
    if (state === next) return;
    state = next;
    syncFallback();
    notify();
  }

  // 订阅者（顶栏指示灯）重算：状态变化与事件到达都要通知，
  // 否则指示灯 title 里的「最近事件时间」会停在首次渲染的文案上
  function notify(){
    watchers.forEach(w => { try{ w(state); }catch(e){ /* 指示灯异常不影响通道 */ } });
  }

  // ---- 绑定调度：在线靠事件，离线靠各自节奏的轮询 ----
  function clearTimer(b){ if (b.timer){ clearInterval(b.timer); b.timer = null; } }

  function syncFallback(){
    Object.keys(binds).forEach(k => {
      const b = binds[k];
      if (!b || b.dead) return;
      if (state === 'online'){ clearTimer(b); return; }
      const ms = document.hidden ? Math.max(b.fallbackMs, FALLBACK_WHEN_HIDDEN_MS) : b.fallbackMs;
      if (b.timer) clearInterval(b.timer);
      b.timer = setInterval(() => run(b), ms);
    });
  }

  function fire(b){
    if (b.dead) return;
    b.lastRun = Date.now();
    let r;
    try{ r = b.fn(); }catch(e){ /* 刷新失败静默：与既有 poll() 口径一致 */ }
    // 约定沿用 poll()：回调返回 false 表示不再需要该绑定（如跑马灯被关闭）；
    // 异步回调拿到的是 Promise，必须解包后再判 false
    if (r && typeof r.then === 'function'){
      r.then(v => { if (v === false) unbind(b); }).catch(() => {});
    } else if (r === false){ unbind(b); }
  }

  function unbind(b){
    if (!b) return;
    b.dead = true; clearTimer(b); if (b.trail) clearTimeout(b.trail);
    delete binds[b.id];
  }

  function run(b){
    if (b.dead) return;
    const wait = COALESCE_MS - (Date.now() - b.lastRun);
    if (wait <= 0){ fire(b); return; }
    if (b.trail) return;            // 已有尾随合并，不重复排队
    b.trail = setTimeout(() => { b.trail = null; fire(b); }, wait);
  }

  function dispatch(topic){
    lastEventTs = Date.now();
    notify();
    Object.keys(binds).forEach(k => {
      const b = binds[k];
      if (!b || b.dead) return;
      if (topic === 'resync' || b.topics.indexOf(topic) >= 0) run(b);
    });
  }

  // ---- 帧解析（与 agent-stream.js 同款：按空行切帧、忽略注释行、坏帧跳过）----
  function handleLine(line){
    if (!line.startsWith('data:')) return;   // ": ping" 心跳注释帧走这里被忽略
    const raw = line.slice(5).trim();
    if (!raw) return;
    let frame = null;
    try{ frame = JSON.parse(raw); }catch(e){ return; }
    if (!frame || typeof frame.type !== 'string') return;
    // 只有真正收到 hello 才算通道打开（复位退避）：仅凭 HTTP 200 复位会让
    // 「服务端拒接但返回 200 + error 帧」变成 1s 一次的紧循环重连（实测风暴）
    if (frame.type === 'hello'){ capped = false; attempt = 0; return; }
    if (frame.type === 'error'){ capped = true; throw new Error('stream_capped'); }
    if (frame.type === 'event') dispatch(frame.topic);
    else if (frame.type === 'bye') throw new Error('stream_closed');  // 交给外层重连
  }

  function scheduleReconnect(){
    if (retryTimer || started === false) return;
    if (document.hidden) return;             // 隐藏时不重连，回前台由 visibilitychange 触发
    let delay = Math.min(BACKOFF_MAX_MS, 1000 * Math.pow(2, attempt));
    if (capped) delay = Math.max(delay, CAP_BACKOFF_MS);   // 服务端满：慢下来而不是每秒戳一次
    attempt += 1;
    retryTimer = setTimeout(() => { retryTimer = null; connect(); }, delay);
  }

  async function connect(){
    if (!token()) return;                    // 未登录不建流
    if (retryTimer){ clearTimeout(retryTimer); retryTimer = null; }
    if (ctrl){ try{ ctrl.abort(); }catch(e){} }
    ctrl = new AbortController();
    setState('connecting');
    try{
      const resp = await fetch('/api/stream?topics=' + TOPICS.join(','), {
        headers: {'Authorization': 'Bearer ' + token()},
        signal: ctrl.signal
      });
      if (!resp.ok || !resp.body){          // 503 订阅已满 / 网关异常：退回轮询
        setState('degraded'); scheduleReconnect(); return;
      }
      setState('online');
      const reader = resp.body.getReader(), dec = new TextDecoder();
      let buf = '';
      for(;;){
        const chunk = await reader.read();
        if (chunk.done) break;
        buf += dec.decode(chunk.value, {stream: true});
        let i;
        while ((i = buf.indexOf('\n\n')) >= 0){
          const part = buf.slice(0, i); buf = buf.slice(i + 2);
          part.split('\n').forEach(handleLine);
        }
      }
      if (buf.trim()) buf.split('\n').forEach(handleLine);
      setState('degraded'); scheduleReconnect();
    }catch(e){
      if (e && e.name === 'AbortError') return;   // 主动中止（退出登录/页面隐藏）不重连
      setState('degraded'); scheduleReconnect();
    }
  }

  function connectWithAbort(){
    if (document.hidden){                   // 隐藏期间不占用服务端流，退回降频轮询
      if (ctrl){ try{ ctrl.abort(); }catch(err){} ctrl = null; }
      setState('degraded');
      return;
    }
    connect();
  }

  document.addEventListener('visibilitychange', () => {
    if (!started) return;
    // 已在线且前台可见时不重建流（避免可见性抖动把旧流 abort 掉造成重复订阅）
    if (!document.hidden && state === 'online') return;
    connectWithAbort();
  });

  // ---- 对外 API ----
  // liveBind(topic | topics[], fn, fallbackMs) → off()；fn 返回 false 视为自动解绑
  // 多主题台共用一个刷新函数（如设备页靠 monitor+tasks 两个事件源）时绑一次即可，
  // 避免绑多条而重复重取（合并窗口只在单绑定内生效）
  window.liveBind = function(topic, fn, fallbackMs){
    const id = ++bindSeq;
    const topics = (Array.isArray(topic) ? topic : [topic]).filter(Boolean);
    binds[id] = {id, topics, fn, fallbackMs: fallbackMs || 30000,
                 timer: null, lastRun: 0, trail: null, dead: false};
    if (!started) window.liveStart();
    else syncFallback();
    return function off(){ unbind(binds[id]); };
  };
  window.liveStart = function(){
    if (started) return;
    started = true;
    connectWithAbort();
  };
  window.liveStop = function(){
    started = false;
    if (retryTimer){ clearTimeout(retryTimer); retryTimer = null; }
    if (ctrl){ try{ ctrl.abort(); }catch(e){} ctrl = null; }
    setState('degraded');
  };
  window.liveState = function(){ return state; };
  window.liveLastEvent = function(){ return lastEventTs; };
  // 顶栏详情面板展示订阅主题用（返回副本，防外部改到内部数组）
  window.liveTopics = function(){ return TOPICS.slice(); };
  window.liveOnState = function(w){
    if (typeof w === 'function'){ watchers.push(w); try{ w(state); }catch(e){} }
    return () => { const i = watchers.indexOf(w); if (i >= 0) watchers.splice(i, 1); };
  };
})();
