// 数据库快照视图纯逻辑回归测试（种类文案 / 字节格式 / 恢复禁用规则 / 两段式提示）
// 运行：node tests/js/config_database_view_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ctx = { console, Promise, Map, Object, JSON, String, Number, Math, Date, window: {} };
vm.createContext(ctx);
vm.runInContext(
  fs.readFileSync(path.join(__dirname, '../../webapp/static/js/pages/config/config-database.js'), 'utf8'),
  ctx,
);
const V = ctx.window.cfgDbView;

const failures = [];
function check(name, cond, detail) {
  if (!cond) failures.push(name + (detail !== undefined ? ': ' + detail : ''));
}
function eq(name, got, want) {
  check(name, got === want, `got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}

if (!V) {
  console.error('FAIL\nwindow.cfgDbView 未导出（纯逻辑必须与 DOM 分离才能无头测试）');
  process.exit(1);
}

// 1) 快照种类：三种内部 kind 都要有中文，未知 kind 原样显示（新种类不能伪装成「手工」）
eq('kind manual', V.kindLabel('manual'), '手工');
eq('kind pre-migration', V.kindLabel('pre-migration'), '迁移前自动');
eq('kind pre-restore', V.kindLabel('pre-restore'), '恢复前自动');
eq('kind unknown passthrough', V.kindLabel('pre-rollback'), 'pre-rollback');

// 2) 字节格式：空值不能显示成 "0 B"（那是真的 0 字节文件），单位按 1024 进位
eq('fmtBytes null', V.fmtBytes(null), '—');
eq('fmtBytes zero', V.fmtBytes(0), '0 B');
eq('fmtBytes bytes', V.fmtBytes(999), '999 B');
eq('fmtBytes kb', V.fmtBytes(1024), '1.0 KB');
eq('fmtBytes kb half', V.fmtBytes(1536), '1.5 KB');
eq('fmtBytes mb', V.fmtBytes(5 * 1024 * 1024), '5.0 MB');
eq('fmtBytes gb', V.fmtBytes(3 * 1024 * 1024 * 1024), '3.0 GB');

// 3) 台账丢失文件的行不能给恢复入口：点了只会收到 409，管理员不知道为什么
eq('restore disabled when file missing', V.canRestore({ name: 'a', file_missing: true }), false);
eq('restore allowed when file present', V.canRestore({ name: 'a', file_missing: false }), true);
eq('restore allowed by default', V.canRestore({ name: 'a' }), true);
eq('restore never crashes on empty row', V.canRestore(null), false);

// 4) 待恢复横幅：说清「还没换库」，别让管理员以为已经回滚完成
eq('pending banner without pending', V.pendingBannerText(null), '');
const banner = V.pendingBannerText({ name: 'platform.000007.20261120-101500.pre-restore.db.gz',
                                     staged_at: 1763606100, restart_required: true });
check('pending banner names the snapshot', banner.indexOf('platform.000007') >= 0, banner);
check('pending banner asks for a restart', banner.indexOf('重启') >= 0, banner);
check('pending banner is not past tense', !/已恢复|恢复完成|回滚成功/.test(banner), banner);

// 5) 恢复确认框：必须在点「确认」前把两段式的两个后果说全（要重启 + 当前库会另存）
const row = { name: 'platform.000003.20261118-090000.manual.db.gz', kind: 'manual', file_missing: false };
const tip = V.restoreConfirmText(row);
check('confirm names the snapshot', tip.indexOf('platform.000003') >= 0, tip);
check('confirm warns restart needed', tip.indexOf('重启') >= 0, tip);
check('confirm warns current db is kept', tip.indexOf('恢复前自动') >= 0, tip);
check('confirm says db is replaced', !/只是导出|不会改动/.test(tip), tip);

// 6) 保留策略摘要：边界来自后端，前端只回显，不自己写死一份判据
const summary = V.retentionSummaryText({ keep: 5, audit_retention_days: 90,
                                         bounds: { keep: [1, 50], audit_retention_days: [7, 3650] } });
check('summary shows keep', summary.indexOf('5 份') >= 0, summary);
check('summary shows keep bounds', summary.indexOf('1~50') >= 0, summary);
check('summary shows audit days', summary.indexOf('90 天') >= 0, summary);
check('summary shows audit bounds', summary.indexOf('7~3650') >= 0, summary);

if (failures.length) {
  console.error('FAIL\n' + failures.join('\n'));
  process.exit(1);
}
console.log('config database view tests passed');
