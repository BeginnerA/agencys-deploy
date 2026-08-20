from webapp.monitor import get_monitor

mon = get_monitor()
if mon:
    # 检查缓存中的数据
    print('=== 探测缓存 ===')
    for env_id, data in mon._cache.items():
        print(f"env_id={env_id}:")
        for s in data.get('servers', []):
            print(f"  host={s['host']}, remark={s.get('remark', 'N/A')}")
    
    # 检查 get_overview 返回的数据
    print('\n=== get_overview 返回 ===')
    overview = mon.get_overview(1, 'prod')
    for s in overview.get('servers', []):
        print(f"  host={s['host']}, remark={s.get('remark', 'N/A')}")
else:
    print('monitor 未初始化')
