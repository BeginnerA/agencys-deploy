'use strict';
const { jarHostTableHTML } = require('../../webapp/static/js/jar_hosts.js');

const war = jarHostTableHTML([
  {host: '10.0.0.1', status: 'online', version: '1.0.0',
   config_status: 'maintenance', operation_state: 'in_progress',
   maintenance: true, instance_id: 'i1', live: true},
]);
if (!war.includes('<th>配置</th>') || !war.includes('维护中')
    || !war.includes('操作中')) {
  console.error('FAIL war host columns'); process.exit(1);
}
const bad = jarHostTableHTML([
  {host: '10.0.0.2', status: 'unhealthy', config_status: 'managed',
   operation_state: 'idle', maintenance: false, live: true},
]);
if (!bad.includes('不健康')) {
  console.error('FAIL unhealthy status label'); process.exit(1);
}
const unmanaged = jarHostTableHTML([
  {host: '10.0.0.3', status: 'unmanaged', config_status: 'unmanaged',
   operation_state: 'idle', maintenance: false, live: true},
]);
if (!unmanaged.includes('未托管')) {
  console.error('FAIL unmanaged status label'); process.exit(1);
}
const jar = jarHostTableHTML([{host: 'h', status: 'online', live: true}]);
if (jar.includes('<th>配置</th>')) {
  console.error('FAIL jar host columns must stay unchanged'); process.exit(1);
}
console.log('PASS war_hosts_test');
