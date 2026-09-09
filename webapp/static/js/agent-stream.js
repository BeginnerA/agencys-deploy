// agent-stream.js — 统一 SSE 对话客户端（智能助手页 / 全局悬浮助手共用）
// 封装 /api/agent/chat/stream 的 fetch + 逐行解析；事件协议与后端一致：
//   text(正文增量) / tool(工具开始) / tool_result / done(收尾) / error(异常)
// 用法：
//   const cancel = streamAgentChat({
//     message, sessionId,          // 必填：正文与会话
//     onEvent(ev),                 // 每个解析成功的事件
//     on401(),                     // 未登录（调用方负责清理跳转）
//     onFatal(msg, hint),          // HTTP 非 200 / 网络异常（流内 error 事件走 onEvent）
//     onClose()                    // 正常结束/出错/中止均恰好回调一次（UI 收尾）
//   });
// 返回 cancel()：调用后 AbortController.abort()，onClose 仍会触发。
function streamAgentChat(opts){
  const ctrl = new AbortController();
  let closed = false;
  const closeOnce = () => {
    if (closed) return;
    closed = true;
    if (typeof opts.onClose === 'function') opts.onClose();
  };
  (async () => {
    try{
      const resp = await fetch('/api/agent/chat/stream', {
        method: 'POST',
        headers: {'Authorization': 'Bearer ' + token(),
                  'Content-Type': 'application/json'},
        body: JSON.stringify({message: opts.message,
                              session_id: opts.sessionId}),
        signal: ctrl.signal
      });
      if (resp.status === 401){
        if (typeof opts.on401 === 'function') opts.on401();
        return;
      }
      if (!resp.ok){
        const d = await resp.json().catch(() => ({}));
        if (typeof opts.onFatal === 'function'){
          opts.onFatal(d.error || ('HTTP ' + resp.status), d.hint || '');
        }
        return;
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      const dispatch = (line) => {
        if (!line.startsWith('data:')) return;
        const raw = line.slice(5).trim();
        if (!raw) return;
        let ev = null;
        try{ ev = JSON.parse(raw); }catch(e){ return; } // 单条坏事件跳过
        if (ev && typeof ev.type === 'string' &&
            typeof opts.onEvent === 'function'){
          opts.onEvent(ev);
        }
      };
      for(;;){
        const chunk = await reader.read();
        if (chunk.done) break;
        buf += decoder.decode(chunk.value, {stream: true});
        let i;
        while ((i = buf.indexOf('\n\n')) >= 0){
          const part = buf.slice(0, i);
          buf = buf.slice(i + 2);
          part.split('\n').forEach(dispatch);
        }
      }
      if (buf.trim()) buf.split('\n').forEach(dispatch); // 尾部无空行分隔的残余
    }catch(e){
      if (e && e.name !== 'AbortError' && typeof opts.onFatal === 'function'){
        opts.onFatal(e.message || '请求失败', '');
      }
    }finally{
      closeOnce();
    }
  })();
  return function cancel(){
    try{ ctrl.abort(); }catch(e){}
  };
}
