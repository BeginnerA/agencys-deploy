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

if (failures.length) {
  console.error('FAIL\n' + failures.join('\n'));
  process.exit(1);
}
console.log('jar hosts render tests passed');
