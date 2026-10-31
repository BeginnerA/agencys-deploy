// jar_hosts.js 纯渲染回归：字段展示、空态、快照提示与 XSS 转义
'use strict';
const { jarHostTableHTML } = require('../../webapp/static/js/jar_hosts.js');

const failures = [];
function check(name, cond, detail) {
  if (!cond) failures.push(name + (detail ? ': ' + detail : ''));
}

const html = jarHostTableHTML([{
  host: '10.0.0.1', status: 'online', version: '1.0.1',
  previous_version: '1.0.0', pid: 42, uptime_hours: 2.5, restarts: 1,
  cpu: 3.2, rss_mb: 128, live: true,
}]);
check('status label', html.includes('在线'), html);
check('current version', html.includes('1.0.1'));
check('previous version', html.includes('1.0.0'));
check('pid', html.includes('42'));
check('uptime', html.includes('2.5小时'));
check('cpu', html.includes('3.2%'));
check('memory', html.includes('128 MB'));
check('live tag', html.includes('实时'));

const xss = jarHostTableHTML([{
  host: '<img src=x onerror=alert(1)>', status: 'online', version: '<b>x</b>',
  live: true,
}]);
check('no injected tag', !/<img|<b>/i.test(xss), xss);
check('host escaped', xss.includes('&lt;img'), xss);

const empty = jarHostTableHTML([]);
check('empty state', empty.includes('暂无主机版本数据'), empty);

const snapshot = jarHostTableHTML([{
  host: 'h1', status: 'unknown', version: '2.0.0', live: false,
}]);
check('snapshot tag', snapshot.includes('版本快照'), snapshot);
check('snapshot hint', snapshot.includes('开启实时监控'), snapshot);
check('null cpu placeholder', snapshot.includes('—'));

// 旧模式行不得出现 WAR 专属列（列头与单元格双向）
for (const h of [html, snapshot]) {
  check('non-war rows keep old columns', !['进程', '应用', '恢复', '配置', '漂移', '维护', '操作']
    .some(label => h.includes('<th>' + label + '</th>')), h);
}

// WAR 行：进程/应用并存、恢复、漂移、操作阶段细分
const unhealthy = jarHostTableHTML([{
  host: '10.0.0.9', status: 'unhealthy', version: '1.0.0', live: true,
  config_status: 'managed', operation_state: 'in_progress',
  operation_phase: 'health', instance_id: 'i1', maintenance: false,
  process_status: 'online', app_health: 'failed',
  recovery_status: 'succeeded', version_drift: true,
}]);
check('war columns header', ['进程', '应用', '恢复', '配置', '漂移', '维护', '操作']
  .every(label => unhealthy.includes('<th>' + label + '</th>')), unhealthy);
check('unhealthy status label', unhealthy.includes('不健康'), unhealthy);
check('process online coexists with app failed',
      unhealthy.includes('在线') && unhealthy.includes('不健康'), unhealthy);
check('recovery succeeded label', unhealthy.includes('成功'), unhealthy);
check('version drift yes', unhealthy.includes('是'), unhealthy);
check('operation phase label', unhealthy.includes('健康检查'), unhealthy);
check('generic in_progress label not shown', !unhealthy.includes('操作中'), unhealthy);

const unknownRecovery = jarHostTableHTML([{
  host: '10.0.0.8', status: 'online', live: true, config_status: 'managed',
  operation_state: 'idle', recovery_status: 'unknown',
}]);
check('recovery unknown not succeeded', !unknownRecovery.includes('成功'),
      unknownRecovery);

const unmanagedRow = jarHostTableHTML([{
  host: '10.0.0.7', status: 'unmanaged', live: true, config_status: 'unmanaged',
}]);
check('unmanaged status label', unmanagedRow.includes('未托管'), unmanagedRow);

if (failures.length) {
  console.error('FAIL\n' + failures.join('\n'));
  process.exit(1);
}
console.log('jar hosts render tests passed');
